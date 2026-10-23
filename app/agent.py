"""The autonomous document agent.

    analyze  ->  plan (TODO list)  ->  execute each task  ->  reflect & revise  ->  render .docx

The agent decides the document type, structure, execution order and assumptions
itself. Its engineering improvement is the reflection loop (`_reflect`): a
reviewer pass scores the draft against the ORIGINAL request, deterministic checks
catch placeholders and thin sections, and only the flagged sections are rewritten
(or missing ones added) before the document is rendered.

Prevention complements that detection: the model calls deterministic tools
(calculator, calendar) while it writes, and `verify.py` recomputes every table
total and weekday after each write, fixing them in code.
"""
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from . import prompts
from .config import settings
from .docx_renderer import render_document
from .llm import LLMClient, LLMError, RateLimitedError, UsageStats
from .schemas import (
    AgentResponse, ChecksInfo, DocumentInfo, NewSection, Plan, PlannedTask, RequestAnalysis, Review,
    ReviewIssue, ReviewRound, SectionContent, TodoItem, UsageInfo,
)
from .verify import verify_section, verify_text

log = logging.getLogger("agent")

MAX_TASKS = 9
MAX_ADDED_SECTIONS = 2
# Sections about money, figures or schedules get the tools; others skip the extra round trips.
_NUMERIC_TOPIC = re.compile(r"budget|cost|price|financ|revenue|p&l|profit|margin|roi|break.?even|forecast|saving|"
                            r"capex|opex|salar|headcount|timeline|schedule|milestone|roadmap|calendar|date|week|"
                            r"month|quarter|metric|kpi|target|%|percent", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"\[(?:insert|add|tbd|placeholder|your|name|date|company|client)[^\]]*\]|\bTBD\b|\bTBC\b|lorem ipsum|\bXX+\b|\{\{[^}]*\}\}",
    re.IGNORECASE,
)


class OutOfScopeError(Exception):
    pass


@dataclass
class Run:
    """State for one request: the live TODO list plus token/latency accounting."""
    request: str
    stats: UsageStats = field(default_factory=UsageStats)
    todo: list[TodoItem] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)
    corrections: list[str] = field(default_factory=list)  # fixed by code
    first_draft_errors: int = 0  # arithmetic/date errors in sections as first written
    fact_sheet_errors: int = 0  # date errors in the analysis' fact sheet
    totals_checked: int = 0

    def add(self, id: str, title: str, action: str, before: str | None = None) -> TodoItem:
        item = TodoItem(id=id, title=title, action=action)
        idx = next((i for i, t in enumerate(self.todo) if t.id == before), len(self.todo))
        self.todo.insert(idx, item)
        return item

    def item(self, id: str) -> TodoItem:
        return next(t for t in self.todo if t.id == id)

    @contextmanager
    def track(self, item: TodoItem):
        item.status = "running"
        t0 = time.perf_counter()
        log.info("▶ [%s] %-17s %s", item.id, item.action, item.title)
        try:
            yield item
        except Exception as e:
            item.status = "failed"
            item.detail = item.detail or str(e)[:200]
            raise
        else:
            if item.status == "running":
                item.status = "done"
        finally:
            item.duration_s = round(time.perf_counter() - t0, 2)
            mark = "✔" if item.status in ("done", "revised") else "✖"
            log.info("%s [%s] %s in %.1fs%s", mark, item.id, item.status, item.duration_s,
                     f" — {item.detail}" if item.detail else "")


class DocumentAgent:
    def __init__(self, llm: LLMClient, tools: bool | None = None, review_rounds: int | None = None):
        self.llm = llm
        self.tools = settings.tools_enabled if tools is None else tools
        self.review_rounds = review_rounds or settings.review_rounds

    async def run(self, request: str) -> tuple[AgentResponse, str]:
        """Execute the full pipeline. Returns the API response and the generated filename."""
        run = Run(request)
        log.info("━━━━ New request: %s", request[:140])

        # 1. Understand the request and resolve gaps/conflicts up front.
        with run.track(run.add("S1", "Understand request: type, audience, gaps, conflicts", "analyze")) as item:
            analysis = await self.llm.json(self._sys(prompts.ANALYZE_SYSTEM, prompts.ANALYZE_TOOLS),
                                           prompts.analyze_user(request), RequestAnalysis, run.stats,
                                           prefer=settings.reasoning_model, temperature=0.2, max_tokens=2500,
                                           effort="medium", tools=self.tools)
            item.detail = (f"{analysis.document_type}; {len(analysis.assumptions)} assumptions, "
                           f"{len(analysis.conflicts)} conflicts")
        if not analysis.in_scope:
            raise OutOfScopeError(analysis.out_of_scope_reason or "This request cannot be served by a business document.")
        analysis = self._verify_facts(run, analysis)

        # 2. Plan: the agent writes its own TODO list.
        with run.track(run.add("S2", "Create execution plan (TODO list)", "plan")) as item:
            plan = await self._plan(run, analysis)
            item.detail = f"{len(plan.tasks)} tasks"
        for task in plan.tasks:
            run.add(task.id, task.title, task.action)
        run.add("S3", "Self-review against the request; revise weak sections", "reflect")
        run.add("S4", "Render polished Word document", "render")
        log.info("TODO list:\n%s", "\n".join(f"   [ ] {t.id:<3} {t.action:<17} {t.title}" for t in run.todo[2:]))

        # 3. Execute. Document order != execution order: the summary is written last, from finished sections.
        sections: dict[str, SectionContent] = {}
        for task in sorted(plan.tasks, key=lambda t: t.action == "executive_summary"):
            await self._execute(run, analysis, plan, task, sections)

        # 4. Reflect & revise (the engineering improvement).
        rounds: list[ReviewRound] = []
        try:
            with run.track(run.item("S3")) as item:
                rounds = await self._reflect(run, analysis, plan, sections)
                item.detail = "score " + " → ".join(f"{r.score}/10" for r in rounds)
        except LLMError as e:
            log.error("Reflection failed (%s); rendering the unreviewed draft", e)

        # 5. Render.
        with run.track(run.item("S4")) as item:
            filename = f"{_slug(analysis.title)}_{datetime.now():%Y%m%d_%H%M%S}.docx"
            verification = (f"{sum(run.stats.tool_calls.values())} calculator/calendar tool call(s); "
                            f"{run.totals_checked} table total(s) checked by code; "
                            f"{len(run.corrections)} fix(es) applied by code") if self.tools else ""
            render_document(analysis, plan.tasks, sections, rounds, settings.output_dir / filename, verification)
            item.detail = filename

        return self._response(run, analysis, plan, sections, rounds, filename), filename

    # ------------------------------------------------------------ planning

    async def _plan(self, run: Run, analysis: RequestAnalysis) -> Plan:
        plan = await self.llm.json(prompts.PLAN_SYSTEM, prompts.plan_user(run.request, analysis),
                                   Plan, run.stats, prefer=settings.reasoning_model, temperature=0.3,
                                   max_tokens=1800)
        # Enforce the planning contract regardless of what the model returned.
        tasks, seen = [], set()
        for t in plan.tasks:
            key = t.title.strip().lower()
            if not key or key in seen or "assumption" in key or "open question" in key:
                continue
            if "executive summary" in key:
                t.action = "executive_summary"
            seen.add(key)
            tasks.append(t)
        summaries = [t for t in tasks if t.action == "executive_summary"]
        body = [t for t in tasks if t.action != "executive_summary"][: MAX_TASKS - 1]
        summary = summaries[0] if summaries else PlannedTask(
            action="executive_summary", title="Executive Summary",
            instructions="Summarise the document for a busy executive: context, recommendation, key figures.")
        plan.tasks = [summary, *body]
        for i, t in enumerate(plan.tasks, 1):
            t.id = f"T{i}"
        return plan

    # ------------------------------------------------------------ execution

    def _sys(self, system: str, tool_rules: str, tools: bool | None = None) -> str:
        return system + "\n" + tool_rules if (self.tools if tools is None else tools) else system

    def _needs_tools(self, task: PlannedTask, issues: list[ReviewIssue] = ()) -> bool:
        if not self.tools:
            return False
        if task.action == "executive_summary" or any(i.category in ("arithmetic", "dates") for i in issues):
            return True
        return bool(_NUMERIC_TOPIC.search(" ".join([task.title, task.instructions, *task.covers])))

    def _verify(self, run: Run, task: PlannedTask, content: SectionContent, first_draft: bool) -> SectionContent:
        """Recompute table totals/shares and weekdays. With tools off this only measures, it never edits."""
        v = verify_section(content, fix=self.tools)
        run.totals_checked += v.totals_checked
        if first_draft:
            run.first_draft_errors += len(v.corrections) + len(v.issues)
        for c in v.corrections:
            log.info("    🧮 [%s] %s %s", task.id, "fixed" if self.tools else "found (not fixed, tools off)", c)
        if not self.tools:
            return content
        run.corrections += [f"[{task.id}] {c}" for c in v.corrections]
        return v.content

    def _verify_facts(self, run: Run, analysis: RequestAnalysis) -> RequestAnalysis:
        """The fact sheet is every section's source of truth, so its dates are checked before anything uses them."""
        def check(text: str) -> str:
            fixed, corrections, issues = verify_text(text, fix=self.tools)
            run.fact_sheet_errors += len(corrections) + len(issues)
            for c in corrections + issues:
                log.info("    🧮 [fact sheet] %s %s", "fixed" if self.tools and c in corrections else "found", c)
            if self.tools:
                run.corrections += [f"[fact sheet] {c}" for c in corrections]
            return fixed if self.tools else text

        analysis.key_facts = [check(f) for f in analysis.key_facts]
        analysis.assumptions = [check(a) for a in analysis.assumptions]
        for c in analysis.conflicts:
            c.resolution = check(c.resolution)
        return analysis

    async def _execute(self, run: Run, analysis: RequestAnalysis, plan: Plan, task: PlannedTask,
                       sections: dict[str, SectionContent], extra: str = "", first_draft: bool = True) -> bool:
        try:
            use_tools = self._needs_tools(task)
            with run.track(run.item(task.id)) as item:
                for attempt in range(2):
                    content = await self.llm.json(
                        self._sys(prompts.WRITE_SYSTEM.format(document_type=analysis.document_type,
                                                              audience=analysis.audience), prompts.WRITE_TOOLS, use_tools),
                        prompts.section_user(run.request, analysis, plan.tasks, task, sections, extra),
                        SectionContent, run.stats, prefer=settings.writer_model, temperature=0.6, max_tokens=1500,
                        tools=use_tools,
                    )
                    # Cheap inline checks: an empty reply, or a table section without a table, is retried now.
                    if content.word_count() < 20 and not content.table:
                        problem = "your previous reply contained no text. Write the full section"
                    elif task.action == "build_table" and not (content.table and len(content.table.rows) >= 2):
                        problem = "your previous reply had no usable table. The table object is mandatory: give columns and 4-10 complete rows"
                    elif task.action == "executive_summary" and not content.bullets:
                        problem = "your previous reply had no key points. Include 3-5 bullets, each with a concrete figure or date"
                    else:
                        break
                    if attempt:
                        break
                    log.warning("  [%s] %s — retrying once", task.id, problem.split(".")[0].replace("your previous reply", "reply"))
                    extra += f"\nIMPORTANT: {problem}."
                content = self._verify(run, task, content, first_draft)
                if (old := sections.get(task.id)) and (reason := _regression(task, old, content)):
                    log.warning("  ✗ new version of [%s] %s; keeping the previous version", task.id, reason)
                    item.detail = f"kept previous version ({reason})"
                    return False
                sections[task.id] = content
                item.detail = f"{content.word_count()} words" + (f", table {len(content.table.rows)} rows" if content.table else "")
            return True
        except RateLimitedError:
            if first_draft:  # quota is gone: abort with a clear error instead of rendering empty sections
                raise
            log.error("  Task %s hit the rate limit; keeping the current version", task.id)
            return False
        except LLMError:
            log.error("  Task %s failed; continuing with the rest of the plan", task.id)
            return False

    # ------------------------------------------------------------ reflection

    async def _reflect(self, run: Run, analysis: RequestAnalysis, plan: Plan,
                       sections: dict[str, SectionContent]) -> list[ReviewRound]:
        rounds: list[ReviewRound] = []
        best: tuple[ReviewRound, dict[str, SectionContent], list[PlannedTask]] | None = None
        added = 0
        for n in range(1, self.review_rounds + 1):
            static = _static_checks(plan.tasks, sections, dates=self.tools)
            try:
                previous = [i for i in rounds[-1].issues if i.source == "llm"] if rounds else None
                review = await self.llm.json(
                    prompts.REVIEW_SYSTEM,
                    prompts.review_user(run.request, analysis, plan.tasks, sections, previous, verified=self.tools),
                    Review, run.stats, prefer=settings.reasoning_model, temperature=0.1, max_tokens=4000,
                    effort="medium",
                )
            except LLMError:
                if not rounds:
                    raise
                log.error("  Re-review unavailable; keeping the revised draft")
                rounds[-1].verdict = "revised, not re-reviewed"
                return rounds
            issues = _attach_issues(review.issues, plan.tasks) + static
            rnd = ReviewRound(round=n, score=review.score, verdict="revise",
                              requirements_missing=review.requirements_missing, issues=issues)
            if previous:
                resolved = sum(c.resolved for c in review.previous_issues if 1 <= c.number <= len(previous))
                rnd.previous_issues_resolved = f"{resolved}/{len(previous)}"
            rounds.append(rnd)
            log.info("  ◆ review %d: score %d/10, %d issues (%d static)%s",
                     n, review.score, len(issues), len(static),
                     f", previous issues resolved {rnd.previous_issues_resolved}" if previous else "")
            for i in issues:
                log.info("      - [%s] %-6s %s", i.section_id, i.severity, i.problem[:110])
            if best is None or rnd.score > best[0].score:  # snapshot the best reviewed version
                best = (rnd, {k: v.model_copy(deep=True) for k, v in sections.items()}, list(plan.tasks))

            new_sections = review.new_sections[: MAX_ADDED_SECTIONS - added]
            serious = [i for i in issues if i.severity != "low" and i.section_id != "GLOBAL"]
            high = any(i.severity == "high" for i in issues)
            if not new_sections and not high and (
                    review.score >= 9 or (review.score >= settings.quality_threshold and not serious)):
                rnd.verdict = "pass"
                break
            if n == self.review_rounds or not (serious or new_sections):
                rnd.verdict = "accepted"  # out of budget or nothing actionable: ship the best version
                break
            rnd.revised_sections, rnd.added_sections = await self._revise(
                run, analysis, plan, sections, [i for i in issues if i.severity != "low"], new_sections)
            added += len(rnd.added_sections)

        # Never ship a regression: if a revision made things clearly worse, restore the best reviewed version.
        # Judge scores wobble by about a point, so a 1-point dip alone doesn't undo real fixes.
        final, top = rounds[-1], best[0]
        def highs(r: ReviewRound) -> int:
            return sum(i.severity == "high" for i in r.issues)

        new_high = highs(final) > highs(top)  # e.g. a revision emptied a section
        if top is not final and (top.score - final.score >= 2 or (top.score >= final.score and new_high)):
            log.warning("  ↺ round %d scored %d/10, below round %d (%d/10): shipping the round %d version",
                        rounds[-1].round, rounds[-1].score, best[0].round, best[0].score, best[0].round)
            sections.clear()
            sections.update(best[1])
            plan.tasks[:] = best[2]
            rounds[-1].verdict = f"regressed; rolled back to round {best[0].round}"
            best[0].shipped = True
        else:
            rounds[-1].shipped = True
        return rounds

    async def _revise(self, run: Run, analysis: RequestAnalysis, plan: Plan, sections: dict[str, SectionContent],
                      issues: list[ReviewIssue], new_sections: list[NewSection]) -> tuple[list[str], list[str]]:
        by_task: dict[str, list[ReviewIssue]] = {}
        for issue in issues:
            by_task.setdefault(issue.section_id, []).append(issue)
        summary = plan.tasks[0]
        revised, added = [], []

        for task in plan.tasks[1:]:
            task_issues = by_task.get(task.id)
            if not task_issues:
                continue
            if task.id not in sections:  # failed earlier: retry from scratch
                if await self._execute(run, analysis, plan, task, sections, first_draft=False):
                    revised.append(task.title)
                continue
            log.info("  ✎ revising [%s] %s (%d issues)", task.id, task.title, len(task_issues))
            try:
                use_tools = self._needs_tools(task, task_issues)
                new = await self.llm.json(
                    self._sys(prompts.REVISE_SYSTEM, prompts.WRITE_TOOLS, use_tools),
                    prompts.revise_user(analysis, task, sections[task.id], task_issues,
                                        [(t, sections[t.id]) for t in plan.tasks[1:] if t.id in sections and t is not task],
                                        verified=self.tools),
                    SectionContent, run.stats, prefer=settings.writer_model, temperature=0.4, max_tokens=1500,
                    tools=use_tools,
                )
                new = self._verify(run, task, new, first_draft=False)
                if reason := _regression(task, sections[task.id], new):
                    log.warning("  ✗ revision of [%s] %s; keeping the previous version", task.id, reason)
                    continue
                sections[task.id] = new
                run.item(task.id).status = "revised"
                revised.append(task.title)
            except LLMError as e:
                log.error("  Revision of %s failed (%s); keeping the previous version", task.id, e)

        # Re-plan: add sections for requirements the reviewer says nothing covers.
        existing = {t.title.lower() for t in plan.tasks}
        for ns in new_sections:
            if ns.title.lower() in existing:
                continue
            task = PlannedTask(id=f"T{len(plan.tasks) + 1}", action=ns.action, title=ns.title,
                               instructions=ns.instructions)
            plan.tasks.append(task)
            run.add(task.id, task.title, task.action, before="S3")
            log.info("  ＋ re-plan: added [%s] %s", task.id, task.title)
            if await self._execute(run, analysis, plan, task, sections, first_draft=False):
                added.append(task.title)

        # The summary must reflect the revised body, so regenerate it whenever anything changed.
        summary_issues = by_task.get(summary.id, []) + by_task.get("GLOBAL", [])
        if revised or added or summary_issues:
            notes = _bullets_text(summary_issues)
            if await self._execute(run, analysis, plan, summary, sections, first_draft=False,
                                   extra=f"Reviewer notes to address:\n{notes}" if notes else ""):
                run.item(summary.id).status = "revised"
                revised.append(summary.title)
        return revised, added

    # ------------------------------------------------------------ response

    def _response(self, run: Run, analysis: RequestAnalysis, plan: Plan, sections: dict[str, SectionContent],
                  rounds: list[ReviewRound], filename: str) -> AgentResponse:
        written = [t for t in plan.tasks if t.id in sections]
        words = sum(sections[t.id].word_count() for t in written)
        failed = [t for t in run.todo if t.status == "failed"]
        summary = sections.get(plan.tasks[0].id)

        msg = (f"Generated a {len(written)}-section {analysis.document_type} \"{analysis.title}\" "
               f"(~{words} words) for {analysis.audience}.")
        decisions = [f"resolved {len(analysis.conflicts)} conflicting requirement(s)" if analysis.conflicts else "",
                     f"made {len(analysis.assumptions)} documented assumption(s)" if analysis.assumptions else ""]
        if any(decisions):
            msg += " It " + " and ".join(d for d in decisions if d) + "."
        shipped = next((r for r in rounds if r.shipped), None)
        if rounds:
            revised = sum(len(r.revised_sections) + len(r.added_sections) for r in rounds)
            msg += (f" Self-review score {' → '.join(f'{r.score}/10' for r in rounds)}"
                    f"{f' after revising {revised} section(s)' if revised else ''}.")
            if shipped and shipped is not rounds[-1]:
                msg += f" A later revision scored lower, so the {shipped.score}/10 version was shipped."
        if failed:
            msg += f" {len(failed)} task(s) failed: {', '.join(t.title for t in failed)}."

        s = run.stats
        return AgentResponse(
            status="partial" if failed else "success",
            message=msg,
            document=DocumentInfo(filename=filename, download_url="", document_type=analysis.document_type,
                                  title=analysis.title, sections=[t.title for t in written], word_count=words),
            key_points=summary.bullets if summary else [],
            analysis=analysis,
            quality_score=shipped.score if shipped else None,
            plan_rationale=plan.rationale,
            todo=run.todo,
            reflection=rounds,
            checks=ChecksInfo(tools_enabled=self.tools, tool_calls=s.tool_calls, first_draft_errors=run.first_draft_errors,
                              fact_sheet_errors=run.fact_sheet_errors,
                              totals_checked=run.totals_checked, corrections=run.corrections),
            usage=UsageInfo(llm_calls=s.calls, prompt_tokens=s.prompt_tokens, completion_tokens=s.completion_tokens,
                            retries=s.retries, fallbacks=s.fallbacks, models_used=s.models_used),
            elapsed_s=round(time.perf_counter() - run.started, 1),
        )


# ---------------------------------------------------------------- helpers

def _static_checks(tasks: list[PlannedTask], sections: dict[str, SectionContent],
                   dates: bool = False) -> list[ReviewIssue]:
    """Deterministic checks the LLM reviewer tends to miss."""
    issues = []
    for t in tasks:
        c = sections.get(t.id)
        if c is None:
            issues.append(ReviewIssue(section_id=t.id, source="static", severity="high", problem="Section failed to generate.",
                                      fix="Write this section."))
            continue
        if m := _PLACEHOLDER.search(c.as_text()):
            issues.append(ReviewIssue(section_id=t.id, source="static", severity="high", problem=f"Placeholder text found: '{m.group()}'.",
                                      fix="Replace every placeholder with a concrete, realistic value."))
        if t.action == "build_table" and (not c.table or len(c.table.rows) < 2):
            issues.append(ReviewIssue(section_id=t.id, source="static", severity="high",
                                      problem="Planned as a table but no usable table was produced.",
                                      fix="Provide a table with at least 4 complete rows."))
        elif t.action != "build_table" and c.word_count() < 80:
            issues.append(ReviewIssue(section_id=t.id, source="static", severity="high",
                                      problem=f"Section is too thin ({c.word_count()} words).",
                                      fix="Expand with specific detail, figures and owners."))
        if dates:  # what code can't fix safely (impossible dates, ambiguous totals) goes to the reviser
            for category, problem in verify_section(c, fix=False).issues:
                issues.append(ReviewIssue(section_id=t.id, source="static", severity="high", category=category,
                                          problem=problem, fix="Recompute with the calculator and calendar tools."))
    return issues


def _regression(task: PlannedTask, old: SectionContent, new: SectionContent) -> str | None:
    """Reject a revision that is structurally worse than what it replaces."""
    if old.table and len(old.table.rows) >= 2 and not (new.table and len(new.table.rows) >= 2):
        return "dropped its table"
    if _PLACEHOLDER.search(new.as_text()) and not _PLACEHOLDER.search(old.as_text()):
        return "introduced placeholder text"
    if new.word_count() < 0.5 * old.word_count():
        return "lost more than half of its content"
    return None


def _attach_issues(issues: list[ReviewIssue], tasks: list[PlannedTask]) -> list[ReviewIssue]:
    """Map reviewer issues to real task ids (models sometimes cite a heading instead of an id)."""
    ids = {t.id for t in tasks}
    by_title = {t.title.lower(): t.id for t in tasks}
    out = []
    for issue in issues:
        sid = issue.section_id.strip().strip("[]")
        if sid not in ids:
            sid = by_title.get(sid.lower()) or next(
                (tid for title, tid in by_title.items() if title in issue.problem.lower()), "GLOBAL")
        out.append(issue.model_copy(update={"section_id": sid}))
    return out


def _bullets_text(issues: list[ReviewIssue]) -> str:
    return "\n".join(f"- {i.problem} -> {i.fix}" for i in issues)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "document"

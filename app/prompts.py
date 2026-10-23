"""Prompt templates for each agent module. Every module replies with one JSON object."""
import json
from datetime import date

from .schemas import PlannedTask, RequestAnalysis, ReviewIssue, SectionContent


def _today() -> str:
    return date.today().strftime("%d %B %Y")


def _bullets(items: list[str], empty: str = "(none)") -> str:
    return "\n".join(f"- {x}" for x in items) if items else empty


# ---------------------------------------------------------------- tools

_TOOL_RULES = """
TOOLS: you can call calculate, percentage, percent_change, date_add, days_between and project_calendar. \
Never do arithmetic or date maths in your head:
- every total, difference, percentage or "X% cheaper/higher" claim: use calculate, percentage or percent_change, \
passing the RAW input numbers (not subtotals you worked out yourself);
- every derived date, weekday or week range: use project_calendar (for timelines), date_add or days_between.
Request all the calls you need in ONE turn, then reply with the JSON object only."""

ANALYZE_TOOLS = _TOOL_RULES + "\nUse them so the fact sheet is exact: milestone dates from project_calendar, budget split and totals from calculate."
WRITE_TOOLS = _TOOL_RULES
VERIFIED_NOTE = ("Table totals, percentage shares and weekdays have already been checked and corrected by code, so "
                 "table arithmetic is right; if a figure in the text disagrees with a table, the text is wrong.")


# ---------------------------------------------------------------- 1. analyze

ANALYZE_SYSTEM = """You are the analysis module of an autonomous agent that produces professional business documents \
(proposals, project plans, meeting minutes, business reports, technical designs, SOPs, product specifications, briefings).
Read the request the way a senior consultant would:
- choose the single most suitable document type, a specific title, and the audience;
- extract every concrete requirement the user stated;
- identify missing information and conflicting or unrealistic requirements. Check explicitly for: length vs. scope \
constraints, budget figures that disagree, timelines that differ between stakeholders, and undecided choices;
- NEVER ask the user questions. Resolve each gap or conflict yourself with a reasonable, explicitly stated assumption so the work can proceed;
- fix the canonical facts (numbers, dates, names, scope) every section must use consistently. Invent realistic mock values where none were given.
Reply with a single JSON object only."""


def analyze_user(request: str) -> str:
    return f"""Today's date: {_today()}

USER REQUEST:
\"\"\"{request}\"\"\"

Return JSON with exactly these keys:
{{
  "in_scope": true,
  "out_of_scope_reason": "",
  "document_type": "e.g. Project Plan, Business Proposal, Board Briefing, Meeting Minutes, SOP, Technical Design, Product Specification, Business Report",
  "title": "specific document title",
  "subtitle": "one-line subtitle",
  "audience": "who will read it",
  "objective": "one sentence: what the document must achieve for the reader",
  "explicit_requirements": ["each thing the user explicitly asked for"],
  "missing_information": ["details a professional would need that were not provided"],
  "conflicts": [{{"issue": "conflicting or unrealistic requirement", "resolution": "how the document presents it and why, e.g. 'treat €50k as committed and present €200k as an option for board approval'"}}],
  "assumptions": ["concrete assumption including the value chosen"],
  "key_facts": ["canonical fact every section must reuse, e.g. 'Total budget: $120,000' or 'Go-live: 15 Jan 2027'"],
  "complexity": "standard or complex"
}}
Use empty lists where nothing applies; do not invent conflicts, and list each real conflict as its own entry. Provide 6-14 key_facts, including the start date, \
key milestone dates (as calendar dates), the budget split, and the names of the people who own key roles (e.g. \
'Project lead: Priya Sharma') where relevant, so every section uses the same schedule, numbers and names.
Set in_scope to false only if no business document can address the request, or it asks for something harmful or illegal."""


# ---------------------------------------------------------------- 2. plan

PLAN_SYSTEM = """You are the planning module of an autonomous document agent. Turn the analysis into an ordered TODO list \
of document sections that the agent will then execute one by one.
Rules:
- 5 to 8 tasks, in document order, tailored to the document type and audience (minutes need attendees, decisions and action items; \
an SOP needs scope, roles and a step-by-step procedure; a proposal needs the problem, solution, cost and the ask).
- Each task has an "action":
  "executive_summary": exactly one, first in the document. The agent executes it LAST, from the finished sections.
  "build_table": content that is best shown as rows and columns (timeline, budget, risk register, RACI, action items, comparison, requirements).
  "write_section": narrative context, analysis or recommendations.
- Every explicit requirement must be covered by at least one task; list the requirements it covers in "covers".
- Do NOT plan a section for assumptions or open questions; the agent appends one automatically.
- "instructions" must be specific: what to include, which key facts to use, what the reader needs to decide.
Reply with a single JSON object only."""


def plan_user(request: str, analysis: RequestAnalysis) -> str:
    brief = analysis.model_dump(include={
        "document_type", "title", "audience", "objective", "explicit_requirements",
        "conflicts", "assumptions", "key_facts", "complexity",
    })
    return f"""USER REQUEST:
\"\"\"{request}\"\"\"

ANALYSIS:
{json.dumps(brief, indent=1, ensure_ascii=False)}

Return JSON exactly in this shape:
{{
  "rationale": "1-2 sentences: why this structure fits the document type, audience and request",
  "tasks": [
    {{"id": "T1", "action": "executive_summary", "title": "Executive Summary", "instructions": "...", "covers": ["..."]}},
    {{"id": "T2", "action": "write_section or build_table", "title": "...", "instructions": "...", "covers": ["..."]}}
  ]
}}"""


# ---------------------------------------------------------------- 3. execute

WRITE_SYSTEM = """You are the writing module of an autonomous document agent. You write ONE section of a professional \
{document_type} for {audience}.
Writing standard:
- Specific and decision-oriented: concrete numbers, dates, owners and names. No generic filler or motivational language.
- Use the fact sheet exactly. Never contradict it or the sections already written.
- Never use placeholders such as [Company], TBD or XX. If a detail is unknown, choose a realistic value consistent with the fact sheet (mock data is acceptable).
- Plain text, no markdown headings. **Bold** may be used sparingly for key terms.
Reply with a single JSON object only."""

_SHAPES = {
    "write_section": (
        '{"paragraphs": ["2-4 paragraphs, 50-120 words each"], '
        '"bullets": ["optional: 3-6 items written as \'Label: detail\'"], "table": null}'
    ),
    "build_table": (
        '{"paragraphs": ["1-2 short paragraphs that introduce and interpret the table"], "bullets": [], '
        '"table": {"caption": "short caption", "columns": ["3-6 column headers"], '
        '"rows": [["one value per column", "..."]]}}\n'
        "Provide 4-10 rows and fill every cell."
    ),
    "executive_summary": (
        '{"paragraphs": ["2-3 paragraphs: context, what is proposed or decided, and the recommendation or ask"], '
        '"bullets": ["3-5 key points, each with a concrete figure or date"], "table": null}'
    ),
}


def _fact_sheet(a: RequestAnalysis) -> str:
    sheet = f"""FACT SHEET (use exactly):
{_bullets(a.key_facts)}
ASSUMPTIONS IN FORCE:
{_bullets(a.assumptions)}"""
    if a.conflicts:
        sheet += "\nDECISIONS ON CONFLICTING REQUIREMENTS (present them transparently; never state something the " \
                 "request left undecided as settled fact):\n" + _bullets([f"{c.issue} -> {c.resolution}" for c in a.conflicts])
    return sheet


def section_user(
    request: str,
    analysis: RequestAnalysis,
    tasks: list[PlannedTask],
    task: PlannedTask,
    written: dict[str, SectionContent],
    extra: str = "",
) -> str:
    outline = "\n".join(f"{i}. {t.title}" for i, t in enumerate(tasks, 1))
    position = tasks.index(task) + 1
    done = [(t, written[t.id]) for t in tasks if t.id in written and t.id != task.id]

    if task.action == "executive_summary":
        context = "FINISHED SECTIONS (summarise faithfully; do not introduce new facts):\n" + "\n\n".join(
            f"### {t.title}\n{c.as_text(max_words=160)}" for t, c in done
        )
    else:
        context = "ALREADY WRITTEN (do not repeat; stay consistent):\n" + _bullets(
            [f"{t.title}: {c.as_text(max_words=40)}" for t, c in done], empty="(nothing yet)"
        )

    return f"""DOCUMENT: {analysis.title} — {analysis.document_type}
AUDIENCE: {analysis.audience}
OBJECTIVE: {analysis.objective}
ORIGINAL REQUEST:
\"\"\"{request}\"\"\"

{_fact_sheet(analysis)}

DOCUMENT OUTLINE:
{outline}

{context}

YOUR TASK: write section {position}. "{task.title}" (action: {task.action})
Instructions: {task.instructions or "Cover this topic thoroughly for the audience."}
Requirements this section must satisfy: {"; ".join(task.covers) or "see instructions"}
{extra}
Return JSON exactly in this shape:
{_SHAPES[task.action]}"""


# ---------------------------------------------------------------- 4. reflect

REVIEW_SYSTEM = """You are the quality gate of an autonomous document agent: a strict senior reviewer.
Judge the draft against the user's ORIGINAL request, not just against itself. Be critical, specific and fair.
Reply with a single JSON object only."""


def review_user(request: str, analysis: RequestAnalysis, tasks: list[PlannedTask],
                sections: dict[str, SectionContent], previous: list[ReviewIssue] | None = None,
                verified: bool = False) -> str:
    draft = "\n\n".join(
        f"[{t.id}] {t.title} ({t.action})\n{sections[t.id].as_text(max_words=420)}"
        for t in tasks if t.id in sections
    )
    ids = ", ".join(t.id for t in tasks if t.id in sections)
    if previous:
        numbered = "\n".join(f"{n}. [{i.section_id}] {i.problem}" for n, i in enumerate(previous, 1))
        focus = f"""This is a RE-REVIEW. The previous review raised these issues and the agent has revised the draft:
{numbered}
First mark each previous issue resolved or not. Then list only NEW high or medium severity issues (requirement gaps, \
factual inconsistencies, contradictions, or regressions introduced by the revision). Do not raise stylistic nitpicks \
or re-litigate resolved points. Score the CURRENT draft with the same rubric."""
        prev_shape = '"previous_issues": [{"number": 1, "resolved": true}],\n  '
    else:
        focus = """Check: (1) every explicit requirement is covered substantively; (2) numbers, dates and names are consistent \
with the fact sheet and across sections; (3) no vague filler, placeholders or unsupported claims; (4) fit for the audience \
and document type; (5) conflicts and assumptions are handled transparently."""
        prev_shape = ""
    return f"""ORIGINAL REQUEST:
\"\"\"{request}\"\"\"

DOCUMENT TYPE: {analysis.document_type}    AUDIENCE: {analysis.audience}
EXPLICIT REQUIREMENTS:
{_bullets(analysis.explicit_requirements)}
{_fact_sheet(analysis)}

DRAFT:
{draft}

{focus}
Note: the rendered document also has a cover page (title, audience, date, version) and an appendix listing every \
assumption, conflict decision and open question; do not flag those as missing. The agent's documented decisions are \
legitimate: flag them only if the draft presents them misleadingly.
{VERIFIED_NOTE if verified else ""}
Severity: high = a requirement is missing or a fact is wrong/contradictory; medium = vague, incomplete or likely to \
confuse the reader; low = polish.
Score rubric (be consistent): 9-10 no high or medium issues, ready for an executive or client; 7-8 at most two medium \
issues; 5-6 a high issue or several medium issues; below 5 unusable.

Return JSON exactly in this shape:
{{
  "score": 7,
  "requirements_missing": ["explicit requirement that is not adequately covered"],
  {prev_shape}"issues": [{{"section_id": "one of: {ids}", "severity": "high, medium or low", "category": "coverage, arithmetic, dates, consistency or clarity", "problem": "what is wrong", "fix": "specific instruction to fix it"}}],
  "new_sections": [{{"title": "...", "action": "write_section or build_table", "instructions": "..."}}],
  "verdict": "pass or revise"
}}
Attach each issue to the section that must change: for a contradiction between sections, that is the one deviating \
from the fact sheet or from the section that defines the information (a team roster defines names, a budget table \
defines amounts, a timeline defines dates).
List at most 6 issues — the ones that most improve the document. Use new_sections only for an explicit requirement \
that no existing section can reasonably absorb; otherwise attach it as an issue to the closest section."""


REVISE_SYSTEM = """You are the revision module of an autonomous document agent. Rewrite ONE section so that every \
reviewer issue is fixed while keeping what was already good.
Keep the same JSON shape (keep a table if the section has one). Same standard: specific, consistent with the fact \
sheet, no placeholders, no markdown headings.
Reply with a single JSON object only."""


def revise_user(analysis: RequestAnalysis, task: PlannedTask, content: SectionContent,
                issues: list[ReviewIssue], others: list[tuple[PlannedTask, SectionContent]],
                verified: bool = False) -> str:
    context = "\n\n".join(f"### {t.title}\n{c.as_text(max_words=90)}" for t, c in others)
    return f"""DOCUMENT: {analysis.title} — {analysis.document_type} for {analysis.audience}
{_fact_sheet(analysis)}

OTHER SECTIONS (stay consistent with their names, roles, dates and figures; if an issue says they conflict, \
align this section with the fact sheet):
{context}

SECTION: "{task.title}" (action: {task.action})
Original instructions: {task.instructions}

CURRENT CONTENT (JSON):
{content.model_dump_json(indent=1)}

REVIEWER ISSUES TO FIX:
{_bullets([f"{i.problem} -> Fix: {i.fix}" for i in issues])}
{VERIFIED_NOTE if verified else ""}

Return the full rewritten section as JSON in this shape:
{_SHAPES[task.action]}"""

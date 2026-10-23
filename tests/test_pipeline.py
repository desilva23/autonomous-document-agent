"""End-to-end agent run with a scripted fake LLM: no network, no tokens.

The script plants the errors seen in real runs (a wrong weekday in the fact sheet,
a budget total that doesn't add up, a timeline row that ends before it starts) and
checks that tools ON fixes or routes each one, and that tools OFF leaves them alone.
"""
import asyncio
import dataclasses
import re

import pytest

from app import agent as agent_module
from app.agent import DocumentAgent
from app.llm import RateLimitedError
from app.schemas import Plan, PlannedTask, RequestAnalysis, Review, ReviewIssue, SectionContent

FILLER = " ".join(["The plan is specific, owned and dated."] * 14)  # ~90 words, passes the thin-section check


class FakeLLM:
    def __init__(self):
        self.reviews = 0
        self.calls: list[tuple[str, str, bool]] = []  # (schema, section title, tools offered)

    async def json(self, system, user, schema, stats, **kw):
        title = ""
        if m := re.search(r'YOUR TASK: write section \d+\. "(.+?)"|SECTION: "(.+?)"', user):
            title = m.group(1) or m.group(2)
        self.calls.append((schema.__name__, title, bool(kw.get("tools"))))

        if schema is RequestAnalysis:
            return RequestAnalysis(
                document_type="Project Plan", title="Helpdesk Migration Plan", audience="Leadership",
                objective="Agree the plan", explicit_requirements=["budget", "timeline", "scope"],
                key_facts=["Start: Monday 2 Nov 2026", "Go-live: Wednesday 21 Dec 2026", "Budget: €45,000"])
        if schema is Plan:
            return Plan(rationale="Plan fits the request", tasks=[
                PlannedTask(action="executive_summary", title="Executive Summary"),
                PlannedTask(action="build_table", title="Budget Breakdown", instructions="Costs per line item"),
                PlannedTask(action="build_table", title="Timeline & Milestones", instructions="Phases with dates"),
                PlannedTask(action="write_section", title="Scope", instructions="What is in and out of scope"),
            ])
        if schema is Review:
            self.reviews += 1
            if self.reviews == 1:
                return Review(score=6, issues=[ReviewIssue(section_id="T3", severity="high", category="dates",
                                                           problem="Pilot ends before it starts", fix="Fix the dates")])
            return Review(score=9, previous_issues=[{"number": 1, "resolved": True}])
        # SectionContent
        if title == "Budget Breakdown":
            return SectionContent(paragraphs=[FILLER], table={"caption": "Budget", "columns": ["Item", "Cost (€)"], "rows": [
                ["Licences", "€20,000"], ["Consulting", "€15,000"], ["Training", "€10,000"], ["Total", "€50,000"]]})
        if title == "Timeline & Milestones":
            revised = "REVIEWER ISSUES" in user
            pilot = ["Pilot", "30 Nov 2026", "11 Dec 2026"] if revised else ["Pilot", "30 Dec 2026", "28 Dec 2026"]
            return SectionContent(paragraphs=[FILLER], table={"columns": ["Phase", "Start date", "End date"], "rows": [
                ["Kick-off", "2 Nov 2026", "13 Nov 2026"], pilot, ["Go-live", "21 Dec 2026", "21 Dec 2026"]]})
        return SectionContent(paragraphs=[FILLER, "Go-live is on Wednesday 21 Dec 2026."], bullets=["Budget: €45,000"])


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "settings", dataclasses.replace(agent_module.settings, output_dir=tmp_path))
    return tmp_path


def run_agent(tools: bool):
    llm = FakeLLM()
    resp, filename = asyncio.run(DocumentAgent(llm, tools=tools, review_rounds=3).run("Plan our helpdesk migration"))
    return llm, resp, filename


def test_tools_on_fixes_and_routes_every_planted_error(outdir):
    llm, resp, filename = run_agent(tools=True)

    assert "Go-live: Monday 21 Dec 2026" in resp.analysis.key_facts  # fact sheet fixed before use
    fixes = "\n".join(resp.checks.corrections)
    assert "[fact sheet] weekday" in fixes and "€50,000 -> €45,000" in fixes
    assert resp.checks.first_draft_errors >= 3  # total, weekdays in prose, the impossible date range

    round1, round2 = resp.reflection
    assert any(i.source == "static" and i.category == "dates" for i in round1.issues)
    assert "Timeline & Milestones" in round1.revised_sections
    assert round2.verdict == "pass" and round2.shipped and resp.quality_score == 9

    # Tools are offered only where they help: money/dates sections, the summary, and date/arithmetic fixes.
    offered = {(schema, title): tools for schema, title, tools in llm.calls}
    assert offered[("SectionContent", "Budget Breakdown")] and offered[("SectionContent", "Executive Summary")]
    assert not offered[("SectionContent", "Scope")]
    assert offered[("RequestAnalysis", "")] and not offered[("Review", "")]
    assert (outdir / filename).is_file()


def test_tools_off_measures_but_changes_nothing(outdir):
    _, resp, _ = run_agent(tools=False)
    assert resp.checks.corrections == [] and resp.checks.first_draft_errors >= 2
    assert "Go-live: Wednesday 21 Dec 2026" in resp.analysis.key_facts
    assert not any(i.source == "static" and i.category == "dates" for i in resp.reflection[0].issues)


class QuotaGoneLLM(FakeLLM):
    async def json(self, system, user, schema, stats, **kw):
        if schema is SectionContent:
            raise RateLimitedError("All models are rate limited", retry_after=626)
        return await super().json(system, user, schema, stats, **kw)


def test_exhausted_quota_aborts_instead_of_rendering_an_empty_document(outdir):
    with pytest.raises(RateLimitedError):
        asyncio.run(DocumentAgent(QuotaGoneLLM(), tools=True).run("Plan our helpdesk migration"))
    assert list(outdir.iterdir()) == []


def test_api_returns_503_with_retry_after(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    class Agent:
        async def run(self, request):
            raise RateLimitedError("All models are rate limited", retry_after=626)

    with TestClient(app) as client:
        app.state.agent = Agent()
        r = client.post("/agent", json={"request": "Create a project plan for our helpdesk migration"})
    assert r.status_code == 503 and r.headers["retry-after"] == "627"
    assert "about 10 minute(s)" in r.json()["detail"]["reason"]


class EmptySummaryRegenLLM(FakeLLM):
    """Regression: a live run's summary regeneration came back as valid JSON with 0 words and was shipped."""

    async def json(self, system, user, schema, stats, **kw):
        if schema is SectionContent and "Reviewer notes" in user or (
                schema is SectionContent and self.reviews and 'section 1. "Executive Summary"' in user):
            self.calls.append(("SectionContent", "Executive Summary (regen)", bool(kw.get("tools"))))
            return SectionContent()
        return await super().json(system, user, schema, stats, **kw)


def test_empty_regeneration_keeps_the_previous_version(outdir):
    llm = EmptySummaryRegenLLM()
    resp, _ = asyncio.run(DocumentAgent(llm, tools=True, review_rounds=3).run("Plan our helpdesk migration"))
    regens = [c for c in llm.calls if c[1] == "Executive Summary (regen)"]
    assert len(regens) == 2  # empty reply retried once on the spot, then rejected
    assert resp.key_points == ["Budget: €45,000"]  # the original summary survived
    assert next(t for t in resp.todo if t.id == "T1").status != "revised"


class TieWithNewHighIssueLLM(FakeLLM):
    async def json(self, system, user, schema, stats, **kw):
        if schema is SectionContent and '"Timeline & Milestones"' in user:  # start clean: no planted date error
            return SectionContent(paragraphs=[FILLER], table={"columns": ["Phase", "Start date", "End date"], "rows": [
                ["Kick-off", "2 Nov 2026", "13 Nov 2026"], ["Go-live", "21 Dec 2026", "21 Dec 2026"]]})
        if schema is Review:
            self.reviews += 1
            if self.reviews == 1:
                return Review(score=7, issues=[ReviewIssue(section_id="T4", severity="medium", problem="Vague scope",
                                                           fix="Be specific")])
            return Review(score=7, previous_issues=[{"number": 1, "resolved": True}],
                          issues=[ReviewIssue(section_id="T2", severity="high", problem="Budget contradicts the scope",
                                              fix="Align")])
        return await super().json(system, user, schema, stats, **kw)


def test_tie_with_a_new_high_issue_rolls_back_to_the_earlier_version(outdir):
    resp, _ = asyncio.run(DocumentAgent(TieWithNewHighIssueLLM(), tools=True, review_rounds=2).run("Plan it"))
    first, second = resp.reflection
    assert first.shipped and not second.shipped
    assert second.verdict.startswith("regressed")

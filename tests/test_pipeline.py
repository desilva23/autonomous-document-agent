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

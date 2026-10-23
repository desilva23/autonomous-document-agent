from app.schemas import SectionContent
from app.verify import verify_section, verify_text


def table(columns, rows, **extra):
    return SectionContent.model_validate({"table": {"columns": columns, "rows": rows}, **extra})


def test_wrong_total_is_recomputed_in_the_same_format():
    c = table(["Item", "Cost (€)"], [["MVP", "€20,000"], ["Engineers", "€15,000"], ["Total", "€55,000"]])
    v = verify_section(c)
    assert v.content.table.rows[-1] == ["Total", "€35,000"]
    assert c.table.rows[-1] == ["Total", "€55,000"]  # input untouched


def test_qualified_subtotals_are_flagged_and_grand_total_fixed():
    c = table(["Item", "Cost"], [
        ["MVP", "20,000"], ["Engineers", "15,000"], ["Total Core Budget", "45,000"],
        ["Hires", "50,000"], ["Scaling", "100,000"], ["Total Optional Budget", "150,000"],
        ["Grand Total", "190,000"], ["Competitor average", "180,000"],
    ])
    v = verify_section(c)
    rows = v.content.table.rows
    assert rows[2][1] == "45,000"  # qualified: may be a subset, so flagged, never rewritten
    assert v.issues == [("arithmetic", "'Total Core Budget' is 45,000 in 'Cost', but the rows above sum to 35,000. "
                                       "Recompute it with the calculator.")]
    assert rows[5][1] == "150,000"  # already right
    assert rows[6][1] == "185,000"  # plain grand total: unambiguous, fixed
    assert rows[7][1] == "180,000"  # not part of any total


def test_subset_totals_are_not_corrupted():
    """Regression: a real run where 'Total Committed' covers only the rows marked committed."""
    c = table(["Item", "Allocated (€)", "% of €200k", "Notes"], [
        ["Planning", "20000", "10.0%", "Committed baseline"], ["Development", "100000", "50.0%", "Optional full-scale"],
        ["Compliance", "30000", "15.0%", "Optional full-scale"], ["Hiring", "30000", "15.0%", "Optional full-scale"],
        ["Marketing", "20000", "10.0%", "Optional full-scale"], ["Total Committed", "20000", "10.0%", "Baseline"],
        ["Total Optional", "200000", "100.0%", "Full rollout budget"]])
    v = verify_section(c)
    assert v.corrections == [] and v.issues == []
    c.table.rows[5][1] = "50000"  # the model's actual mistake: committed rows sum to 20,000
    v = verify_section(c)
    assert v.corrections == [] and "rows marked 'committed' sum to 20000" in v.issues[0][1]


def test_shares_are_recomputed_and_total_share_is_100():
    c = table(["Item", "Cost", "% of total"], [["A", "€30k", "50%"], ["B", "€20k", "50%"], ["Total", "€50k", "90%"]])
    rows = verify_section(c).content.table.rows
    assert [r[2] for r in rows] == ["60%", "40%", "100%"]


def test_correct_tables_and_non_money_columns_are_left_alone():
    ok = table(["Item", "Cost (€)", "Share"], [["A", "€30k", "60%"], ["B", "€20k", "40%"], ["Total", "€50k", "100%"]])
    assert verify_section(ok).corrections == []
    weeks = table(["Week", "Dates", "Milestone"], [["1", "2–8 Nov", "Kick-off"], ["8", "21–27 Dec", "Go-live"],
                                                   ["Total", "8", "weeks"]])
    assert verify_section(weeks).corrections == []


def test_weekday_is_corrected_everywhere():
    c = SectionContent(paragraphs=["Go-live is on Wednesday 21 Dec 2026."], bullets=["Kick-off: Tue, 2 November 2026"])
    v = verify_section(c)
    assert v.content.paragraphs[0] == "Go-live is on Monday 21 Dec 2026."
    assert v.content.bullets[0].startswith("Kick-off: Mon,")
    assert verify_text("Cut-over: Wednesday 21 Dec 2026")[0] == "Cut-over: Monday 21 Dec 2026"


def test_impossible_dates_are_flagged_not_guessed():
    c = table(["Phase", "Start date", "End date"], [["Pilot", "30 Dec 2026", "28 Dec 2026"]],
              paragraphs=["The pilot runs 15 Dec 2026 – 1 Dec 2026."])
    v = verify_section(c)
    assert len(v.issues) == 2
    assert v.content.table.rows[0] == ["Pilot", "30 Dec 2026", "28 Dec 2026"]


def test_detect_only_mode_changes_nothing():
    c = table(["Item", "Cost"], [["A", "$10"], ["Total", "$99"]])
    v = verify_section(c, fix=False)
    assert v.corrections and v.content.table.rows[-1] == ["Total", "$99"]

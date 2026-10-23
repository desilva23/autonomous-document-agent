from app.tools import run_tool


def test_calculate_handles_thousands_separators_and_suffixes():
    assert run_tool("calculate", {"expression": "20,000 + 15000 + 5000"})["result"] == 40000
    assert run_tool("calculate", {"expression": "round(50k / 180k * 100, 1)"})["result"] == 27.8
    assert run_tool("calculate", {"expression": "sum([1, 2, 3.5])"})["result"] == 6.5


def test_calculate_rejects_code():
    result = run_tool("calculate", {"expression": "__import__('os').system('ls')"})
    assert "error" in result


def test_percent_change_phrases_direction():
    result = run_tool("percent_change", {"from_value": 180000, "to_value": 50000})
    assert result["change_percent"] == -72.22
    assert result["summary"] == "50000 is 72.2% lower than 180000"


def test_percentage():
    assert run_tool("percentage", {"part": 12000, "whole": 120000})["percent"] == 10.0


def test_calendar_tools():
    assert run_tool("date_add", {"start_date": "2026-11-02", "weeks": 8, "days": -1})["result"] == "Sun 27 Dec 2026"
    assert run_tool("date_add", {"start_date": "2026-11-06", "business_days": 1})["result"] == "Mon 09 Nov 2026"
    between = run_tool("days_between", {"start_date": "2 Nov 2026", "end_date": "2026-12-27"})
    assert (between["calendar_days"], between["business_days"]) == (56, 40)
    cal = run_tool("project_calendar", {"start_date": "2026-11-02", "weeks": 8})
    assert cal["weeks"][7] == {"week": 8, "start": "Mon 21 Dec 2026", "end": "Sun 27 Dec 2026"}


def test_bad_input_comes_back_as_data():
    assert "error" in run_tool("date_add", {"start_date": "next tuesday"})
    assert "error" in run_tool("calculate", {"expr": "1+1"})
    assert "error" in run_tool("no_such_tool", {})

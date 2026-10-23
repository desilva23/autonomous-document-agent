"""Deterministic tools: exact arithmetic, percentages and calendar maths.

LLMs are unreliable at sums, percentages and dates (in testing, a model passed a
mentally mis-added subtotal into its own calculator call). These tools give exact
answers. The model calls them through native function calling while it writes, and
`verify.py` reuses the same number/date parsing to check every table afterwards.
"""
import ast
import math
import operator
import re
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- tool schemas (OpenAI format)

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


_DATE = {"type": "string", "description": "Date as YYYY-MM-DD"}

TOOL_SPECS = [
    _fn("calculate",
        "Evaluate an arithmetic expression exactly. Supports + - * / ** %, parentheses, round(), sum([...]), "
        "min(), max(), abs(). Always pass the RAW input numbers, never intermediate results you computed yourself.",
        {"expression": {"type": "string", "description": "e.g. '20000 + 15000 + 5000' or 'round(50000 / 180000 * 100, 1)'"}},
        ["expression"]),
    _fn("percentage", "What percentage `part` is of `whole` (e.g. a cost line as a share of the total budget).",
        {"part": {"type": "number"}, "whole": {"type": "number"}}, ["part", "whole"]),
    _fn("percent_change",
        "Percentage change from one value to another, with a plain-English summary such as '72.2% lower'. "
        "Use it for every 'X% cheaper / more / less than' claim.",
        {"from_value": {"type": "number"}, "to_value": {"type": "number"}}, ["from_value", "to_value"]),
    _fn("date_add", "Add calendar days, weeks and/or business days (Mon-Fri) to a date. Returns the date and weekday.",
        {"start_date": _DATE, "days": {"type": "integer"}, "weeks": {"type": "integer"},
         "business_days": {"type": "integer"}}, ["start_date"]),
    _fn("days_between", "Calendar days, business days and weeks between two dates (end date inclusive).",
        {"start_date": _DATE, "end_date": _DATE}, ["start_date", "end_date"]),
    _fn("project_calendar",
        "Week-by-week calendar for a project: the start and end date (with weekday) of each week, plus the "
        "final end date. Use it to build timelines so week numbers and dates always agree.",
        {"start_date": _DATE, "weeks": {"type": "integer", "description": "Number of weeks (1-104)"}},
        ["start_date", "weeks"]),
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SPECS]


def run_tool(name: str, args: dict) -> dict:
    """Execute a tool call. Errors are returned to the model as data so it can correct itself."""
    try:
        fn = _TOOLS[name]
    except KeyError:
        return {"error": f"unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}"}
    try:
        return fn(**args)
    except Exception as e:  # tool boundary: any failure goes back to the model as data, never crashes the run
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- arithmetic

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"round": round, "min": min, "max": max, "abs": abs, "sqrt": math.sqrt,
          "sum": lambda *a: sum(a[0]) if len(a) == 1 and isinstance(a[0], list) else sum(a)}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise ValueError("exponent too large")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("only numbers, + - * / ** %, parentheses and round/sum/min/max/abs/sqrt are allowed")


def _clean_expression(expr: str) -> str:
    expr = re.sub(r"[$€£₹]", "", expr).replace("×", "*").replace("÷", "/").replace("−", "-")
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr)  # 50,000 -> 50000
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*([kKmM])\b", lambda m: f"({m.group(1)}*{1000 if m.group(2) in 'kK' else 1000000})", expr)
    return expr.strip()


def _tidy(x: float) -> int | float:
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return int(x)
    return round(x, 6) if isinstance(x, float) else x


def calculate(expression: str) -> dict:
    if len(expression) > 300:
        raise ValueError("expression too long")
    value = _eval(ast.parse(_clean_expression(expression), mode="eval"))
    if isinstance(value, list):
        raise ValueError("expression must produce a single number")
    return {"expression": expression, "result": _tidy(value)}


def percentage(part: float, whole: float) -> dict:
    pct = part / whole * 100
    return {"part": _tidy(part), "whole": _tidy(whole), "percent": round(pct, 2),
            "summary": f"{_tidy(part)} is {pct:.1f}% of {_tidy(whole)}"}


def percent_change(from_value: float, to_value: float) -> dict:
    change = (to_value - from_value) / from_value * 100
    direction = "higher" if change > 0 else "lower" if change < 0 else "unchanged"
    return {"from": _tidy(from_value), "to": _tidy(to_value), "change_percent": round(change, 2),
            "summary": f"{_tidy(to_value)} is {abs(change):.1f}% {direction} than {_tidy(from_value)}"}


# ---------------------------------------------------------------- dates

_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%d/%m/%Y", "%a %d %b %Y", "%A %d %B %Y")


def parse_date(text: str) -> date:
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(text)).replace(",", " ").replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date '{text}', use YYYY-MM-DD")


def fmt_date(d: date) -> str:
    return d.strftime("%a %d %b %Y")


def _add_business_days(d: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    while n:
        d += timedelta(days=step)
        if d.weekday() < 5:
            n -= step
    return d


def date_add(start_date: str, days: int = 0, weeks: int = 0, business_days: int = 0) -> dict:
    d = parse_date(start_date) + timedelta(days=days, weeks=weeks)
    d = _add_business_days(d, business_days)
    return {"start": fmt_date(parse_date(start_date)), "result": fmt_date(d), "iso": d.isoformat(),
            "weekday": d.strftime("%A")}


def days_between(start_date: str, end_date: str) -> dict:
    a, b = parse_date(start_date), parse_date(end_date)
    if b < a:
        return {"error": f"end date {fmt_date(b)} is before start date {fmt_date(a)}"}
    total = (b - a).days + 1
    business = sum(1 for i in range(total) if (a + timedelta(days=i)).weekday() < 5)
    return {"start": fmt_date(a), "end": fmt_date(b), "calendar_days": total, "business_days": business,
            "weeks": round(total / 7, 1)}


def project_calendar(start_date: str, weeks: int) -> dict:
    if not 1 <= weeks <= 104:
        raise ValueError("weeks must be between 1 and 104")
    start = parse_date(start_date)
    rows = [{"week": n, "start": fmt_date(start + timedelta(weeks=n - 1)),
             "end": fmt_date(start + timedelta(weeks=n, days=-1))} for n in range(1, weeks + 1)]
    return {"start": fmt_date(start), "end": rows[-1]["end"], "weeks": rows}


_TOOLS = {"calculate": calculate, "percentage": percentage, "percent_change": percent_change,
          "date_add": date_add, "days_between": days_between, "project_calendar": project_calendar}

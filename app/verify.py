"""Deterministic verification of written sections: table arithmetic and dates.

Runs on every section after it is written or revised, so wrong numbers never
reach the document even when the model skipped its tools. Auto-fixes are limited
to unambiguous cases; anything ambiguous is flagged for the reviser instead:
- plain "Total"/"Subtotal"/"Grand total" rows in money/quantity columns are recomputed and fixed;
- qualified totals ("Total Committed") may cover a subset of rows, so they are only flagged when
  they match neither the rows above, the rows sharing their qualifier, nor all rows;
- "% of total" shares are recomputed from the amounts;
- corrects weekday names that don't match their date ("Monday 3 Nov 2026" -> "Tuesday");
- flags date ranges whose end is before their start (sent to the reflection loop to fix).
"""
import re
from dataclasses import dataclass, field

from .schemas import SectionContent, TableData
from .tools import parse_date

_NUM = re.compile(
    r"^\s*(?P<neg>[-−])?\s*(?P<pre>[$€£₹]|USD|EUR|GBP|INR)?\s*"
    r"(?P<int>\d{1,3}(?:(?P<sep>[,   ])\d{3})+|\d+)(?P<dec>\.\d+)?\s*"
    r"(?P<suf>[kKmM])?\s*(?P<post>USD|EUR|GBP|INR|€)?\s*(?P<pct>%)?\s*$"
)
_TOTAL = re.compile(r"\b(?:grand\s+total|sub-?total|total)\b", re.I)
_BARE_TOTAL = re.compile(r"^\s*(?:grand\s+total|overall\s+total|total)\b[^a-z]*$", re.I)
# A total whose label adds nothing but generic nouns: "Total", "Subtotal", "Total cost (EUR)".
_GENERIC_TOTAL = re.compile(r"^\s*(?:grand\s+|overall\s+)?(?:sub-?)?total(?:\s+(?:costs?|budget|investment|spend|"
                            r"amount|price|value|fees?))?\s*(?:\([^)]*\))?\s*[:.]?\s*$", re.I)
_GENERIC_WORDS = {"total", "subtotal", "sub", "grand", "overall", "cost", "costs", "budget", "investment", "spend",
                  "amount", "price", "value", "fee", "fees", "of", "the", "and", "for", "eur", "usd"}
_SUMMABLE = re.compile(r"cost|budget|amount|price|spend|fee|salar|value|total|€|\$|£|₹|usd|eur|gbp|inr|"
                       r"hours|fte|headcount|qty|quantity|units|share|%|allocation|investment|capex|opex", re.I)
_NOT_SUMMABLE = re.compile(r"week|year|month|date|day|phase|step|#|\bno\b|\bid\b|rank|score|priority|"
                           r"likelihood|impact|probability|rating|version|sprint|quarter|q[1-4]", re.I)
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_WEEKDAY = (r"\b(?P<wd>Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:r|rs|rsday)?|Fri(?:day)?|Sat(?:urday)?|"
            r"Sun(?:day)?)\.?,?\s+")
_WEEKDAY_DATE = re.compile(
    _WEEKDAY + rf"(?P<date>\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS},?\s+\d{{4}}|{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})")
_FULL_DATE = rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS},?\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}"
_RANGE = re.compile(rf"(?P<a>{_FULL_DATE})\s*(?:–|—|-|to|until)\s*(?P<b>{_FULL_DATE})")


@dataclass
class Verification:
    content: SectionContent
    corrections: list[str] = field(default_factory=list)  # fixed automatically
    issues: list[tuple[str, str]] = field(default_factory=list)  # (category, problem) for the reviser
    totals_checked: int = 0


def verify_text(text: str, fix: bool = True) -> tuple[str, list[str], list[str]]:
    """Weekday and date-range checks for a plain string (e.g. a fact-sheet entry)."""
    v = Verification(content=SectionContent())
    fixed = _fix_weekdays(text, v, fix)
    _check_ranges(fixed, v)
    return fixed, v.corrections, [problem for _, problem in v.issues]


def verify_section(content: SectionContent, fix: bool = True) -> Verification:
    """Check (and if `fix`, correct) a section. Never mutates its input."""
    c = content.model_copy(deep=True)
    v = Verification(content=c)

    if c.table:
        shares = _check_shares(c.table, v, fix)
        _check_totals(c.table, v, fix, skip=shares)
        _check_date_columns(c.table, v)

    for kind, items in (("paragraphs", c.paragraphs), ("bullets", c.bullets)):
        for i, text in enumerate(items):
            items[i] = _fix_weekdays(text, v, fix)
            _check_ranges(items[i], v)
    if c.table:
        for row in c.table.rows:
            for i, cell in enumerate(row):
                row[i] = _fix_weekdays(cell, v, fix)
                _check_ranges(row[i], v)
    v.issues = list(dict.fromkeys(v.issues))
    return v


# ---------------------------------------------------------------- numbers

@dataclass
class _Num:
    value: float
    pre: str
    post: str
    sep: str
    decimals: int
    suffix: str
    pct: bool

    @property
    def unit(self) -> float:  # value of the last displayed digit
        return _MULT.get(self.suffix.lower(), 1) * 10 ** -self.decimals


_MULT = {"k": 1_000, "m": 1_000_000}


def _parse(cell: str) -> _Num | None:
    m = _NUM.match(cell or "")
    if not m:
        return None
    sep = m.group("sep") or ""
    value = float(m.group("int").replace(sep, "") if sep else m.group("int")) + float(m.group("dec") or 0)
    suffix = m.group("suf") or ""
    value *= _MULT.get(suffix.lower(), 1)
    if m.group("neg"):
        value = -value
    return _Num(value, m.group("pre") or "", m.group("post") or "", sep, len(m.group("dec") or ".")-1,
                suffix, bool(m.group("pct")))


def _format_like(n: _Num, value: float) -> str:
    scaled = value / _MULT.get(n.suffix.lower(), 1)
    body = f"{abs(scaled):,.{n.decimals}f}"
    body = body.replace(",", n.sep) if n.sep else body.replace(",", "")
    out = f"{'-' if value < 0 else ''}{n.pre}{body}{n.suffix}"
    if n.post:
        out += ("" if n.post == "€" else " ") + n.post
    return out + ("%" if n.pct else "")


def _close(n: _Num, expected: float) -> bool:
    return abs(n.value - expected) <= max(n.unit / 2, abs(expected) * 0.005, 0.5)


def _numeric_columns(table: TableData) -> list[int]:
    cols = []
    for j, header in enumerate(table.columns):
        if j == 0 or _NOT_SUMMABLE.search(header) and not _SUMMABLE.search(header):
            continue
        cells = [r[j] for r in table.rows if not _TOTAL.search(r[0])]
        parsed = [_parse(x) for x in cells if x.strip()]
        if not parsed or sum(p is not None for p in parsed) / len(parsed) < 0.6:
            continue
        has_unit = any(p and (p.pre or p.post or p.suffix or p.pct) for p in parsed)
        if _SUMMABLE.search(header) or has_unit:
            cols.append(j)
    return cols


def _pct_column(table: TableData, j: int) -> bool:
    return all((p := _parse(r[j])) is None or p.pct for r in table.rows if r[j].strip())


def _qualifiers(label: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", label.lower()) if w not in _GENERIC_WORDS and len(w) > 2}


def _check_totals(table: TableData, v: Verification, fix: bool, skip: set[int]) -> None:
    for j in _numeric_columns(table):
        if j in skip or _pct_column(table, j):  # percent sums are ambiguous unless the share check handled them
            continue
        items_all, segment, subtotals = [], [], []
        for row in table.rows:
            n = _parse(row[j])
            if not _TOTAL.search(row[0]):
                if n:
                    items_all.append(n.value)
                    segment.append(n.value)
                continue
            if n is None:
                continue
            if not _GENERIC_TOTAL.match(row[0]):
                _check_qualified_total(table, row, j, n, segment, items_all, v)
                subtotals.append(n.value)
                segment = []
                continue
            if _BARE_TOTAL.match(row[0]) and subtotals:
                expected = sum(items_all)  # grand total over all line items
            elif segment:
                expected = sum(segment)  # (sub)total of the lines since the previous total
            elif subtotals:
                expected = sum(subtotals)  # total of the subtotals above
            else:
                continue
            v.totals_checked += 1
            if not _close(n, expected):
                fixed = _format_like(n, expected)
                v.corrections.append(f"'{row[0]}' / '{table.columns[j]}': {row[j]} -> {fixed} (sum of line items)")
                if fix:
                    row[j] = fixed
                    n = _parse(fixed)
            subtotals.append(n.value)
            segment = []


def _check_qualified_total(table: TableData, row: list[str], j: int, n: _Num, segment: list[float],
                           items_all: list[float], v: Verification) -> None:
    """'Total Committed' may sum a subset of rows, so it is never auto-fixed, only flagged if it fits nothing."""
    words = _qualifiers(row[0])
    tagged = [p.value for r in table.rows if not _TOTAL.search(r[0]) and (p := _parse(r[j]))
              and any(re.search(rf"\b{w}", " ".join(r).lower()) for w in words)]
    candidates = [sum(c) for c in (segment, tagged, items_all) if c]
    if not candidates:
        return
    v.totals_checked += 1
    if not any(_close(n, c) for c in candidates):
        basis = f"rows marked '{' '.join(sorted(words))}' sum to {_format_like(n, sum(tagged))}, " if tagged else ""
        v.issues.append(("arithmetic", f"'{row[0]}' is {row[j]} in '{table.columns[j]}', but {basis}the rows above "
                                       f"sum to {_format_like(n, sum(segment or items_all))}. Recompute it with the calculator."))


def _check_shares(table: TableData, v: Verification, fix: bool) -> set[int]:
    """'% of total' columns must equal amount / total, when the table has a single total row.

    Returns the share columns it handled, so totals checking leaves them alone.
    """
    cols = _numeric_columns(table)
    totals = [r for r in table.rows if _TOTAL.search(r[0])]
    if len(totals) != 1 or len(cols) < 2:
        return set()
    # A header naming its own base ("% of €200k") is a share of that figure, not of the table total.
    pct_cols = [j for j in cols if _pct_column(table, j) and not re.search(r"\d", table.columns[j])]
    amount_cols = [j for j in cols if j not in pct_cols]
    if not pct_cols or not amount_cols:
        return set()
    a = amount_cols[0]
    line_items = [n.value for r in table.rows if not _TOTAL.search(r[0]) and (n := _parse(r[a]))]
    total = sum(line_items)  # the true total, even if the total row itself is wrong
    if not total:
        return set()
    for j in pct_cols:
        for row in table.rows:
            n, amount = _parse(row[j]), _parse(row[a])
            if not n or not amount:
                continue
            expected = 100.0 if _TOTAL.search(row[0]) else amount.value / total * 100
            v.totals_checked += 1
            if abs(n.value - expected) > max(n.unit, 0.6):
                fixed = _format_like(n, expected)
                v.corrections.append(f"'{row[0]}' / '{table.columns[j]}': {row[j]} -> {fixed} (share of total)")
                if fix:
                    row[j] = fixed
    return set(pct_cols)


# ---------------------------------------------------------------- dates

def _fix_weekdays(text: str, v: Verification, fix: bool) -> str:
    def repl(m: re.Match) -> str:
        try:
            d = parse_date(re.sub(r"\.", "", m.group("date")))
        except ValueError:
            return m.group(0)
        given = m.group("wd")
        actual = d.strftime("%A" if len(given) > 3 else "%a")
        if given[:3].lower() == actual[:3].lower():
            return m.group(0)
        v.corrections.append(f"weekday: '{m.group(0).strip()}' -> {actual} ({d:%d %b %Y} is a {d:%A})")
        return m.group(0).replace(given, actual, 1) if fix else m.group(0)

    return _WEEKDAY_DATE.sub(repl, text)


def _check_ranges(text: str, v: Verification) -> None:
    for m in _RANGE.finditer(text):
        try:
            a, b = parse_date(re.sub(r"\.", "", m.group("a"))), parse_date(re.sub(r"\.", "", m.group("b")))
        except ValueError:
            continue
        if b < a:
            v.issues.append(("dates", f"Date range '{m.group(0)}' ends before it starts."))


def _check_date_columns(table: TableData, v: Verification) -> None:
    cols = [h.lower() for h in table.columns]
    start = next((i for i, h in enumerate(cols) if re.search(r"\bstart|\bbegin|\bfrom\b", h)), None)
    end = next((i for i, h in enumerate(cols) if re.search(r"\bend|\bfinish|\bto\b|\bdue|complet", h)), None)
    if start is None or end is None or start == end:
        return
    for row in table.rows:
        try:
            a, b = parse_date(row[start]), parse_date(row[end])
        except ValueError:
            continue
        if b < a:
            v.issues.append(("dates", f"Row '{row[0]}': end date {row[end]} is before start date {row[start]}."))

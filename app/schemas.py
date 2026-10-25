"""Pydantic models: the API contract and the JSON each agent module must return.

LLM output is messy, so the internal models are deliberately lenient: they coerce
strings to lists, numbers to strings, ragged table rows to the right width, etc.
"""
import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------- helpers ----------

def _str_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, dict)):
        v = [v]
    out = []
    for item in v:
        if isinstance(item, dict):
            item = " — ".join(str(x) for x in item.values() if x)
        item = str(item).strip()
        if item:
            out.append(item)
    return out


_LIST_MARKER = re.compile(r"^\s*(?:[-*•▪]|\d+[.)])\s+")


def _normalize_action(v) -> str:
    v = str(v or "").lower()
    if "summary" in v:
        return "executive_summary"
    if "table" in v:
        return "build_table"
    return "write_section"


# ---------- API ----------

class AgentRequest(BaseModel):
    request: str = Field(
        ...,
        min_length=10,
        max_length=4000,
        description="Natural-language description of the document you need.",
    )

    @field_validator("request")
    @classmethod
    def _sensible(cls, v: str) -> str:
        v = v.strip()
        if len(re.findall(r"[A-Za-z]{2,}", v)) < 3:
            raise ValueError("request must be a natural-language sentence describing the document")
        return v


# ---------- module outputs ----------

class Conflict(BaseModel):
    issue: str
    resolution: str = ""


class RequestAnalysis(BaseModel):
    in_scope: bool = True
    out_of_scope_reason: str = ""
    document_type: str
    title: str
    subtitle: str = ""
    audience: str = "Business stakeholders"
    objective: str = ""
    explicit_requirements: list[str] = []
    missing_information: list[str] = []
    conflicts: list[Conflict] = []
    assumptions: list[str] = []
    key_facts: list[str] = []
    complexity: str = "standard"

    @field_validator("explicit_requirements", "missing_information", "assumptions", "key_facts", mode="before")
    @classmethod
    def _lists(cls, v):
        return _str_list(v)

    @field_validator("conflicts", mode="before")
    @classmethod
    def _conflicts(cls, v):
        if not v:
            return []
        if isinstance(v, (str, dict)):
            v = [v]
        return [{"issue": c} if isinstance(c, str) else c for c in v]


class PlannedTask(BaseModel):
    id: str = ""
    action: Literal["executive_summary", "write_section", "build_table"] = "write_section"
    title: str
    instructions: str = ""
    covers: list[str] = []

    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, v):
        return _normalize_action(v)

    @field_validator("covers", mode="before")
    @classmethod
    def _covers(cls, v):
        return _str_list(v)


class Plan(BaseModel):
    rationale: str = ""
    tasks: list[PlannedTask]


class TableData(BaseModel):
    caption: str = ""
    columns: list[str]
    rows: list[list[str]] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if not isinstance(data, dict):
            return data
        cols = [str(c).strip() for c in data.get("columns") or []]
        rows = []
        for row in data.get("rows") or []:
            if isinstance(row, dict):  # {"Phase": "...", "Owner": "..."} -> ordered by columns
                row = [row.get(c, "") for c in cols] if cols else list(row.values())
            elif not isinstance(row, list):
                row = [row]
            cells = ["" if c is None else str(c).strip() for c in row]
            if any(cells):
                rows.append((cells + [""] * len(cols))[: len(cols)] if cols else cells)
        return {**data, "columns": cols, "rows": rows, "caption": str(data.get("caption") or "")}


class SectionContent(BaseModel):
    paragraphs: list[str] = []
    bullets: list[str] = []
    table: TableData | None = None

    @model_validator(mode="before")
    @classmethod
    def _alternative_keys(cls, data):
        # Models sometimes put the prose under another key; an empty section is worse than a renamed one.
        if isinstance(data, dict) and not data.get("paragraphs"):
            for key in ("content", "text", "summary", "body", "section", "paragraph"):
                if data.get(key):
                    return {**data, "paragraphs": data[key]}
        return data

    @field_validator("paragraphs", mode="before")
    @classmethod
    def _paragraphs(cls, v):
        return [p.lstrip("# ").strip() for p in _str_list(v)]

    @field_validator("bullets", mode="before")
    @classmethod
    def _bullets(cls, v):
        return [_LIST_MARKER.sub("", b) for b in _str_list(v)]

    @field_validator("table", mode="before")
    @classmethod
    def _table(cls, v):
        # Accept the shapes models actually produce: "headers" instead of "columns",
        # a bare list of row dicts, or row dicts without a column list.
        if isinstance(v, list) and v and isinstance(v[0], dict):
            v = {"rows": v}
        if not isinstance(v, dict):
            return None
        cols = v.get("columns") or v.get("headers") or v.get("header") or []
        rows = v.get("rows") or v.get("data") or []
        if not cols and rows and isinstance(rows[0], dict):
            cols = list(rows[0].keys())
        if not cols or not rows:
            return None
        return {**v, "columns": cols, "rows": rows}

    def as_text(self, max_words: int | None = None) -> str:
        parts = list(self.paragraphs) + [f"- {b}" for b in self.bullets]
        if self.table:
            parts.append(f"[Table: {self.table.caption}]")
            parts.append(" | ".join(self.table.columns))
            parts += [" | ".join(r) for r in self.table.rows]
        text = "\n".join(parts)
        if max_words:
            words = text.split(" ")
            if len(words) > max_words:
                text = " ".join(words[:max_words]) + " …"
        return text

    def word_count(self) -> int:
        return len(self.as_text().split())


class ReviewIssue(BaseModel):
    section_id: str = "GLOBAL"
    severity: Literal["high", "medium", "low"] = "medium"
    category: Literal["coverage", "arithmetic", "dates", "consistency", "clarity", "other"] = "other"
    problem: str
    fix: str = ""
    source: Literal["llm", "static"] = "llm"

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, v):
        v = str(v or "").lower()
        return next((s for s in ("high", "medium", "low") if s in v), "medium")

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        v = str(v or "").lower()
        aliases = {"math": "arithmetic", "calculation": "arithmetic", "number": "arithmetic", "budget": "arithmetic",
                   "date": "dates", "timeline": "dates", "schedule": "dates", "requirement": "coverage"}
        return next((c for c in ("coverage", "arithmetic", "dates", "consistency", "clarity") if c in v),
                    next((c for k, c in aliases.items() if k in v), "other"))


class PreviousIssueCheck(BaseModel):
    number: int
    resolved: bool


class NewSection(BaseModel):
    title: str
    action: Literal["write_section", "build_table"] = "write_section"
    instructions: str = ""

    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, v):
        return "build_table" if _normalize_action(v) == "build_table" else "write_section"


class Review(BaseModel):
    score: int = 5
    requirements_missing: list[str] = []
    previous_issues: list[PreviousIssueCheck] = []
    issues: list[ReviewIssue] = []
    new_sections: list[NewSection] = []
    verdict: str = "revise"

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, v):
        m = re.search(r"\d+", str(v))
        return max(1, min(10, int(m.group()))) if m else 5

    @field_validator("requirements_missing", mode="before")
    @classmethod
    def _missing(cls, v):
        return _str_list(v)


# ---------- API response ----------

class TodoItem(BaseModel):
    id: str
    title: str
    action: str
    status: Literal["pending", "running", "done", "revised", "failed", "skipped"] = "pending"
    detail: str = ""
    duration_s: float | None = None


class ReviewRound(BaseModel):
    round: int
    score: int
    verdict: str
    requirements_missing: list[str] = []
    previous_issues_resolved: str = ""  # e.g. "5/6" on re-review rounds
    issues: list[ReviewIssue] = []
    revised_sections: list[str] = []
    added_sections: list[str] = []
    shipped: bool = False  # this round's reviewed version is the one rendered


class DocumentInfo(BaseModel):
    filename: str
    download_url: str
    document_type: str
    title: str
    sections: list[str]
    word_count: int


class ChecksInfo(BaseModel):
    """Deterministic tools and verification (see tools.py, verify.py)."""
    tools_enabled: bool
    tool_calls: dict[str, int] = {}
    first_draft_errors: int = Field(0, description="Arithmetic/date errors found in sections as first written")
    fact_sheet_errors: int = Field(0, description="Date errors found in the analysis' fact sheet")
    totals_checked: int = 0
    corrections: list[str] = Field([], description="Fixes applied by code (table totals, shares, weekdays)")


class UsageInfo(BaseModel):
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    retries: int
    fallbacks: int
    models_used: dict[str, int]
    rate_limit_wait_s: float = 0.0


class AgentResponse(BaseModel):
    status: Literal["success", "partial"]
    message: str
    document: DocumentInfo
    key_points: list[str]
    analysis: RequestAnalysis
    quality_score: int | None = Field(None, description="Self-review score of the shipped version")
    plan_rationale: str
    todo: list[TodoItem]
    reflection: list[ReviewRound]
    checks: ChecksInfo
    usage: UsageInfo
    elapsed_s: Annotated[float, Field(description="Wall-clock time for the whole run")]

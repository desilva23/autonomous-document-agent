"""Before/after evaluation of the tool-calling improvement.

Runs each test request with tools OFF (the previous agent) and ON, stopping after the
first review, so it measures first-draft quality: the stage the tools are meant to improve.

Per run:
- first_draft_errors       arithmetic/date errors that code found in sections as first written
- errors_reaching_review   errors still in the draft the reviewer sees (tools ON fixes totals/weekdays in code)
- reviewer_math_date       issues the reviewer categorised as arithmetic or dates
- first_review_score, tool calls, tokens, time

    python examples/eval_tools.py [reps] [both|off|on] [request,...]   -> examples/tool_eval.json + a markdown table
Re-running an arm or a request replaces just those runs in the saved file.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.agent import DocumentAgent  # noqa: E402
from app.llm import LLMClient, build_endpoints  # noqa: E402
from app.main import EXAMPLES  # noqa: E402

OUT = Path(__file__).resolve().parent / "tool_eval.json"
# The two assignment inputs plus one arithmetic-heavy request, where tools should matter most.
REQUESTS = {name: ex["value"]["request"] for name, ex in EXAMPLES.items()} | {
    "numbers": ("Prepare a business case for opening 3 new retail stores. Per-store capex is EUR 180,000, monthly rent "
                "EUR 6,500, 8 staff per store at EUR 2,400 per month each, and expected revenue of EUR 75,000 per month "
                "per store with a 40% cost of goods. Include a budget table, a 12-month P&L summary, the break-even "
                "month, first-year ROI, and a launch timeline starting 1 February 2027 with a milestone every 3 weeks."),
}
PAUSE_S = 45  # let the free tier's per-minute token budget refill between runs


async def main(reps: int, arms: tuple[bool, ...], only: set[str]) -> None:
    llm = LLMClient(build_endpoints())
    saved = json.loads(OUT.read_text()) if OUT.exists() else []
    runs = [r for r in saved if r["tools"] not in arms or r["request"] not in only]
    first = True
    try:
        for rep in range(1, reps + 1):
            for name, request in ((n, r) for n, r in REQUESTS.items() if n in only):
                for tools in arms:
                    if not first:
                        await asyncio.sleep(PAUSE_S)
                    first = False
                    agent = DocumentAgent(llm, tools=tools, review_rounds=1)
                    resp, _ = await agent.run(request)
                    review = resp.reflection[0] if resp.reflection else None
                    math_dates = [i for i in (review.issues if review else [])
                                  if i.source == "llm" and i.category in ("arithmetic", "dates")]
                    static_dates = [i for i in (review.issues if review else [])
                                    if i.source == "static" and i.category == "dates"]
                    runs.append({
                        "request": name, "rep": rep, "tools": tools,
                        "first_review_score": review.score if review else None,
                        "first_draft_errors": resp.checks.first_draft_errors,
                        "fact_sheet_errors": resp.checks.fact_sheet_errors,
                        "errors_reaching_review": (resp.checks.first_draft_errors
                                                   - sum(not c.startswith("[fact sheet]") for c in resp.checks.corrections)
                                                   if tools else resp.checks.first_draft_errors),
                        "reviewer_math_date": len(math_dates),
                        "reviewer_math_date_examples": [i.problem[:200] for i in math_dates[:4]],
                        "date_order_issues": len(static_dates),
                        "corrections_by_code": resp.checks.corrections,
                        "tool_calls": resp.checks.tool_calls,
                        "tokens": resp.usage.prompt_tokens + resp.usage.completion_tokens,
                        "elapsed_s": resp.elapsed_s,
                    })
                    OUT.write_text(json.dumps(runs, indent=2, ensure_ascii=False))
                    r = runs[-1]
                    print(f"\n>>> {name} tools={'ON ' if tools else 'OFF'} score={r['first_review_score']} "
                          f"draft_errors={r['first_draft_errors']} reaching_review={r['errors_reaching_review']} "
                          f"reviewer_math_date={r['reviewer_math_date']} tools={r['tool_calls']} "
                          f"tokens={r['tokens']} {r['elapsed_s']}s\n")
    finally:
        await llm.aclose()
    print(table(runs))


def table(runs: list[dict]) -> str:
    def avg(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(vals) / len(vals) if vals else 0

    lines = ["| Request | Tools | First-review score | Math/date errors in first draft | Reaching the reviewer "
             "| Reviewer math/date issues | Tool calls | Tokens |", "|---|---|---|---|---|---|---|---|"]
    for name in dict.fromkeys(r["request"] for r in runs):
        for tools in (False, True):
            rows = [r for r in runs if r["request"] == name and r["tools"] == tools]
            if not rows:
                continue
            calls = sum(sum(r["tool_calls"].values()) for r in rows) / len(rows)
            lines.append(f"| {name} | {'on' if tools else 'off'} | {avg(rows, 'first_review_score'):.1f} "
                         f"| {avg(rows, 'first_draft_errors'):.1f} | {avg(rows, 'errors_reaching_review'):.1f} "
                         f"| {avg(rows, 'reviewer_math_date'):.1f} | {calls:.0f} | {avg(rows, 'tokens'):,.0f} |")
    return "\n".join(lines)


if __name__ == "__main__":
    arm = sys.argv[2] if len(sys.argv) > 2 else "both"
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else set(REQUESTS)
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1,
                     {"both": (False, True), "off": (False,), "on": (True,)}[arm], only))

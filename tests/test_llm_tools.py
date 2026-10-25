"""The function-calling loop in LLMClient, against a mocked Groq HTTP API."""
import asyncio
import json

import httpx
from pydantic import BaseModel

import pytest

from app.llm import _FINAL_NUDGE, Endpoint, LLMClient, RateLimitedError, UsageStats


class Total(BaseModel):
    total: float


def tool_call(name, args, call_id="call_1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def answer(text):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def run(responses):
    """Serve `responses` in order; return the parsed result, the stats and the request payloads."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        status, body = responses[len(requests) - 1]
        return httpx.Response(status, json=body, headers={"retry-after": "0.01"} if status == 429 else {})

    client = LLMClient([Endpoint("groq", "test-model", "http://groq.test", "key")])
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stats = UsageStats()
    result = asyncio.run(client.json("Reply with JSON.", "Add 20000 and 15000.", Total, stats, tools=True))
    return result, stats, requests


def test_tool_result_is_fed_back_to_the_model():
    result, stats, requests = run([(200, tool_call("calculate", {"expression": "20000 + 15000"})),
                                   (200, answer('{"total": 35000}'))])
    assert result.total == 35000
    assert stats.tool_calls == {"calculate": 1}
    assert "tools" in requests[0] and "response_format" not in requests[0]  # Groq can't mix them
    tool_msg = requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["result"] == 35000


def test_tool_budget_ends_with_json_mode_and_a_nudge():
    calls = [(200, tool_call("calculate", {"expression": "1+1"}, f"c{i}")) for i in range(3)]
    result, stats, requests = run(calls + [(200, answer('{"total": 2}'))])
    assert result.total == 2 and stats.tool_calls == {"calculate": 3}
    final = requests[-1]
    assert "tools" not in final and final["response_format"] == {"type": "json_object"}
    assert final["messages"][-1] == _FINAL_NUDGE


def test_tool_use_rejection_is_retried_not_fatal():
    rejected = {"error": {"message": "Tool choice is none, but model called a tool", "code": "tool_use_failed"}}
    calls = [(200, tool_call("calculate", {"expression": "1+1"}, f"c{i}")) for i in range(3)]
    result, _, requests = run(calls + [(400, rejected), (200, answer('{"total": 2}'))])
    assert result.total == 2 and len(requests) == 5


def test_rate_limit_and_invalid_json_recover():
    result, stats, _ = run([(429, {"error": {"message": "rate limited"}}), (200, answer("not json")),
                            (200, answer('{"total": 7}'))])
    assert result.total == 7 and stats.retries == 2


def test_malformed_tool_calls_are_answered_with_errors():
    bad = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
        {"type": "function", "function": {"name": "calculate", "arguments": "[1, 2]"}},  # no id, args not an object
        {"id": "c2", "type": "function", "function": {"name": "nope", "arguments": "{}"}}]}}]}
    result, stats, requests = run([(200, bad), (200, answer('{"total": 3}'))])
    tool_msgs = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert result.total == 3 and len(tool_msgs) == 2
    assert all("error" in json.loads(m["content"]) for m in tool_msgs)


def test_quota_exhaustion_raises_rate_limited_error_with_retry_after():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "tokens per day"}}, headers={"retry-after": "626"})

    client = LLMClient([Endpoint("groq", "test-model", "http://groq.test", "key")])
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RateLimitedError) as err:
        asyncio.run(client.json("Reply with JSON.", "hi", Total, UsageStats()))
    assert 620 < err.value.retry_after <= 626


def test_rate_limits_are_quiet_but_counted(caplog):
    caplog.set_level("INFO", logger="agent.llm")
    _, stats, _ = run([(429, {"error": {"message": "rate limited"}}), (200, answer('{"total": 1}'))])
    assert stats.rate_limit_wait_s > 0
    assert not [r for r in caplog.records if "rate limit" in r.getMessage()]  # DEBUG only


def test_tool_arguments_are_logged_in_parameter_order(caplog):
    caplog.set_level("INFO", logger="agent.llm")
    client = LLMClient([])
    client._run_tool({"id": "x", "function": {"name": "days_between",
                                              "arguments": '{"end_date": "2026-12-26", "start_date": "2026-11-01"}'}},
                     UsageStats())
    assert "days_between(2026-11-01, 2026-12-26)" in caplog.records[-1].getMessage()

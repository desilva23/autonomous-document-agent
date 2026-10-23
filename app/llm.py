"""OpenAI-compatible JSON chat client with retry, self-repair and model fallback.

Every agent module asks for a JSON object validated against a Pydantic schema.
Calls made with tools=True run a native function-calling loop first: the model may
request deterministic tools (calculator, calendar; see tools.py), the client runs
them and returns the results, and the model then writes its JSON answer. Groq can't
combine JSON mode with tools, so if that answer isn't valid JSON, the next attempt
drops the tools and forces JSON mode with the tool results still in context.
Invalid JSON is fed back to the model to fix; 5xx/timeouts back off; an endpoint
that keeps failing is dropped for that call. A 429 never fails a call by itself:
the endpoint is put on cooldown (provider retry-after) and the call either waits
for its preferred model or moves to whichever Groq model frees up first, within
a time budget. Each call names a preferred model (role routing).
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import settings
from .tools import TOOL_SPECS, run_tool

log = logging.getLogger("agent.llm")
T = TypeVar("T", bound=BaseModel)
MAX_TOOL_TURNS = 3
_FINAL_NUDGE = {"role": "user", "content": "You now have every tool result you need. Do not call any more tools: "
                                           "reply with the final JSON object only."}


@dataclass(frozen=True)
class Endpoint:
    provider: str
    model: str
    base_url: str
    api_key: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class UsageStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    fallbacks: int = 0
    models_used: dict[str, int] = field(default_factory=dict)
    tool_calls: dict[str, int] = field(default_factory=dict)


class LLMError(RuntimeError):
    """Every endpoint in the chain failed."""


class _RateLimited(Exception):
    def __init__(self, wait: float):
        super().__init__(f"rate limited, retry in {wait:.1f}s")
        self.wait = wait


class _Transient(Exception):
    pass


class _BadOutput(Exception):
    pass


class _EndpointDown(Exception):
    pass


def build_endpoints() -> list[Endpoint]:
    endpoints = []
    if settings.groq_api_key:
        models = [settings.reasoning_model, settings.writer_model, *settings.groq_fallback_models]
        for model in dict.fromkeys(models):
            endpoints.append(Endpoint("groq", model, settings.groq_base_url, settings.groq_api_key))
    if settings.ollama_model:
        endpoints.append(Endpoint("ollama", settings.ollama_model, settings.ollama_base_url))
    return endpoints


class LLMClient:
    def __init__(self, endpoints: list[Endpoint]):
        self.endpoints = endpoints
        self._http = httpx.AsyncClient(timeout=settings.request_timeout)
        self._cool_until: dict[str, float] = {}  # endpoint -> when its rate limit resets (monotonic)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _pick(self, prefer: str | None, dead: set[str]) -> tuple[Endpoint | None, float]:
        """Choose the endpoint for the next attempt and how long to wait for it.

        The preferred model (role routing) wins if its rate limit clears within
        max_rate_limit_wait, which keeps the writer and reviewer roles consistent.
        Otherwise use whichever live endpoint is free soonest.
        """
        now = time.monotonic()
        live = [e for e in self.endpoints if e.name not in dead]
        if not live:
            return None, 0.0
        wait = {e.name: max(0.0, self._cool_until.get(e.name, 0) - now) for e in live}
        preferred = next((e for e in live if e.model == prefer), None)
        if preferred and wait[preferred.name] <= settings.max_rate_limit_wait:
            return preferred, wait[preferred.name]
        best = min(live, key=lambda e: wait[e.name])
        return best, wait[best.name]

    async def json(
        self,
        system: str,
        user: str,
        schema: type[T],
        stats: UsageStats,
        *,
        prefer: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 2048,
        effort: str | None = None,
        tools: bool = False,
    ) -> T:
        if not self.endpoints:
            raise LLMError("No LLM configured. Set GROQ_API_KEY in .env (free key at console.groq.com).")

        convo = [{"role": "system", "content": system}, {"role": "user", "content": user}]  # + tool exchanges
        repair: list[dict] = []  # latest self-repair turn, replaced on each retry
        temp, tool_turns, force_json = temperature, 0, False
        failures: dict[str, int] = {}  # endpoint -> failed attempts (rate limits don't count)
        dead: set[str] = set()
        errors: list[str] = []
        deadline = time.monotonic() + settings.rate_limit_budget
        prefer = prefer or self.endpoints[0].model
        last = None

        while True:
            ep, wait = self._pick(prefer, dead)
            if ep is None or time.monotonic() + wait > deadline:
                raise LLMError("All LLM endpoints failed: " + "; ".join(errors[-4:] or ["rate-limit budget exhausted"]))
            if wait > 0:
                log.warning("  waiting %.1fs for %s rate limit", wait, ep.name)
                await asyncio.sleep(wait)
            if ep.name != last and (last or ep.model != prefer):
                log.warning("  ↪ using %s", ep.name)
            last = ep.name

            raw = ""
            use_tools = tools and not force_json and tool_turns < MAX_TOOL_TURNS
            nudge = [_FINAL_NUDGE] if tool_turns and not use_tools else []
            try:
                msg = await self._chat(ep, convo + nudge + repair, temp, max_tokens, effort, stats, use_tools)
                if msg.get("tool_calls"):
                    convo = convo + [{"role": "assistant", "content": msg.get("content") or "",
                                      "tool_calls": msg["tool_calls"]}]
                    for i, call in enumerate(msg["tool_calls"]):
                        call.setdefault("id", f"call_{tool_turns}_{i}")
                        convo.append({"role": "tool", "tool_call_id": call["id"], "content": self._run_tool(call, stats)})
                    tool_turns += 1
                    repair = []
                    continue
                raw = msg.get("content") or ""
                result = schema.model_validate(_extract_json(raw))
                if ep.model != prefer:
                    stats.fallbacks += 1
                return result
            except _RateLimited as e:
                self._cool_until[ep.name] = time.monotonic() + e.wait
                stats.retries += 1
                errors.append(f"{ep.name}: {e}")
                log.warning("  %s rate limited (%.1fs)", ep.name, e.wait)
                continue
            except (_BadOutput, ValueError, ValidationError) as e:
                errors.append(f"{ep.name}: invalid output ({_short(e)})")
                log.warning("  %s gave invalid output: %s", ep.name, _short(e))
                # Self-repair: show the model its own output and the validation error.
                repair = []
                if raw and not isinstance(e, _BadOutput):
                    repair = [
                        {"role": "assistant", "content": raw[:6000]},
                        {"role": "user", "content": f"That reply did not match the required JSON schema: "
                                                    f"{_short(e)}. Reply with ONLY the corrected JSON object."},
                    ]
                temp = max(0.1, temp - 0.2)
                force_json = True  # retry in JSON mode (no tools); tool results stay in context
                if "cut off" in str(e):
                    max_tokens = min(int(max_tokens * 1.6), 8000)
            except _Transient as e:
                errors.append(f"{ep.name}: {e}")
                log.warning("  %s transient error (%s), backing off", ep.name, e)
                await asyncio.sleep(min(2 ** failures.get(ep.name, 0), 8))
            except _EndpointDown as e:
                errors.append(f"{ep.name}: {e}")
                log.warning("  %s unavailable: %s", ep.name, e)
                dead.add(ep.name)
                continue

            stats.retries += 1
            failures[ep.name] = failures.get(ep.name, 0) + 1
            if failures[ep.name] >= settings.max_attempts:
                dead.add(ep.name)

    def _run_tool(self, call: dict, stats: UsageStats) -> str:
        name = call.get("function", {}).get("name", "")
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
            if not isinstance(args, dict):
                args, result = {}, {"error": "arguments must be a JSON object"}
            else:
                result = run_tool(name, args)
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            args, result = {}, {"error": f"invalid arguments: {e}"}
        stats.tool_calls[name] = stats.tool_calls.get(name, 0) + 1
        if "error" in result or "summary" in result or "result" in result:
            shown = result.get("error", result.get("summary", result.get("result")))
        elif isinstance(result.get("weeks"), list):
            shown = f"{len(result['weeks'])} weeks, ends {result['end']}"
        else:
            shown = result
        log.info("    🔧 %s(%s) → %s", name, ", ".join(f"{v}" for v in args.values())[:70], str(shown)[:90])
        return json.dumps(result)

    async def _chat(self, ep: Endpoint, messages, temperature, max_tokens, effort, stats: UsageStats,
                    use_tools: bool = False) -> dict:
        payload = {
            "model": ep.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if use_tools:
            payload["tools"] = TOOL_SPECS
            payload["tool_choice"] = "auto"
        else:
            payload["response_format"] = {"type": "json_object"}
        if ep.model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = effort or settings.reasoning_effort
        headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
        try:
            r = await self._http.post(f"{ep.base_url}/chat/completions", json=payload, headers=headers)
        except httpx.ConnectError as e:
            raise _EndpointDown(f"cannot connect ({e})") from e
        except httpx.TransportError as e:
            raise _Transient(type(e).__name__) from e

        stats.calls += 1
        if r.status_code == 429:
            raise _RateLimited(_retry_after(r))
        if r.status_code >= 500:
            raise _Transient(f"HTTP {r.status_code}")
        if r.status_code == 400 and "tool_use_failed" in r.text and "ool choice is none" in r.text:
            raise _BadOutput("model tried to call a tool after its tool budget was spent")
        if r.status_code == 400 and ("json_validate_failed" in r.text or "failed_generation" in r.text):
            raise _BadOutput("provider could not parse the model's output")
        if r.status_code >= 400:
            raise _EndpointDown(f"HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        usage = data.get("usage") or {}
        stats.prompt_tokens += usage.get("prompt_tokens", 0)
        stats.completion_tokens += usage.get("completion_tokens", 0)
        stats.models_used[ep.name] = stats.models_used.get(ep.name, 0) + 1
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise _BadOutput("reply was cut off at the token limit; be more concise")
        return choice["message"]


def _extract_json(raw: str):
    text = raw.strip()
    if not text:
        raise _BadOutput("empty response")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise _BadOutput("no JSON object found")
    return json.loads(text[start : end + 1])


def _retry_after(r: httpx.Response) -> float:
    header = r.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", r.text)
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return 5.0


def _short(e: Exception) -> str:
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
    return " ".join(lines[:3])[:300]

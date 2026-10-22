"""OpenAI-compatible JSON chat client with retry, self-repair and model fallback.

Every agent module asks for a JSON object validated against a Pydantic schema.
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

log = logging.getLogger("agent.llm")
T = TypeVar("T", bound=BaseModel)


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
    ) -> T:
        if not self.endpoints:
            raise LLMError("No LLM configured. Set GROQ_API_KEY in .env (free key at console.groq.com).")

        base = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        messages, temp = base, temperature
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
            try:
                raw = await self._chat(ep, messages, temp, max_tokens, effort, stats)
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
                messages = base
                if raw and not isinstance(e, _BadOutput):
                    messages = base + [
                        {"role": "assistant", "content": raw[:6000]},
                        {"role": "user", "content": f"That reply did not match the required JSON schema: "
                                                    f"{_short(e)}. Reply with ONLY the corrected JSON object."},
                    ]
                temp = max(0.1, temp - 0.2)
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

    async def _chat(self, ep: Endpoint, messages, temperature, max_tokens, effort, stats: UsageStats) -> str:
        payload = {
            "model": ep.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
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
        if r.status_code == 400 and "json_validate_failed" in r.text:
            raise _BadOutput("provider rejected malformed JSON")
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
        return choice["message"].get("content") or ""


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

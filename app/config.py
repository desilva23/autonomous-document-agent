"""Runtime configuration, read from the environment (and .env)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # Groq (free tier, OpenAI-compatible API) is the primary provider. Work is routed by role so the
    # reviewer is a different model family from the writer, and so each role draws on its own
    # per-model rate-limit bucket (free tier: 8k tokens/min per model).
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    reasoning_model: str = os.getenv("GROQ_REASONING_MODEL", "openai/gpt-oss-120b")  # analyze, plan, review
    writer_model: str = os.getenv("GROQ_WRITER_MODEL", "qwen/qwen3.8-27b")  # write and revise sections
    groq_fallback_models: tuple[str, ...] = _csv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b")
    reasoning_effort: str = os.getenv("GROQ_REASONING_EFFORT", "low")  # for gpt-oss models

    # Optional last-resort local model (e.g. OLLAMA_MODEL=llama3.2:3b). Empty disables it.
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "")

    request_timeout: float = float(os.getenv("LLM_TIMEOUT", "90"))
    max_attempts: int = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
    # Wait up to this long for the preferred model after a 429 before using another model,
    # and give up on a call only after this much total rate-limit waiting.
    max_rate_limit_wait: float = float(os.getenv("LLM_MAX_RATE_LIMIT_WAIT", "2"))
    rate_limit_budget: float = float(os.getenv("LLM_RATE_LIMIT_BUDGET", "120"))

    # Reflection loop: number of review passes, and the score (1-10) that ends it early.
    review_rounds: int = int(os.getenv("AGENT_REVIEW_ROUNDS", "3"))
    quality_threshold: int = int(os.getenv("AGENT_QUALITY_THRESHOLD", "8"))

    output_dir: Path = ROOT / "outputs"


settings = Settings()

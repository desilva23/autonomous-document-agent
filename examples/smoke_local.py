"""Run the agent in-process (no server) on one example. Useful for quick local checks."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.agent import DocumentAgent  # noqa: E402
from app.llm import LLMClient, build_endpoints  # noqa: E402
from app.main import EXAMPLES  # noqa: E402


async def main(name: str):
    llm = LLMClient(build_endpoints())
    try:
        result, filename = await DocumentAgent(llm).run(EXAMPLES[name]["value"]["request"])
        print(json.dumps(result.model_dump(include={"status", "message", "usage", "elapsed_s"}), indent=2))
        print("outputs/" + filename)
    finally:
        await llm.aclose()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "standard"))

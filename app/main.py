"""FastAPI entry point: POST /agent turns a natural-language request into a Word document."""
import logging
import re
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from .agent import DocumentAgent, OutOfScopeError
from .config import settings
from .llm import LLMClient, LLMError, RateLimitedError, build_endpoints
from .schemas import AgentRequest, AgentResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agent")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

EXAMPLES = {
    "standard": {
        "summary": "Standard business request",
        "value": {"request": (
            "Create a project plan for migrating our customer support team (25 agents) from Zendesk to Freshdesk "
            "over 8 weeks. Include scope, timeline with milestones, team roles and responsibilities, budget, "
            "risks with mitigations, and success metrics."
        )},
    },
    "complex": {
        "summary": "Complex: ambiguous, conflicting, missing information",
        "value": {"request": (
            "Our CEO wants something for next week's board meeting about rolling out our AI customer-service "
            "chatbot across Europe. Keep it to one page but make sure it covers everything: GDPR and the EU AI Act, "
            "costs, timeline, hiring, and how we compare with competitors. Budget is around €50k, though finance "
            "mentioned €200k might be approved. Sales wants launch before Christmas, but legal says nothing goes "
            "live before Q2 next year. We haven't decided which countries go first."
        )},
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    endpoints = build_endpoints()
    app.state.llm = LLMClient(endpoints)
    app.state.agent = DocumentAgent(app.state.llm)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    log.info("LLM chain: %s", " → ".join(e.name for e in endpoints) or "NONE (set GROQ_API_KEY)")
    yield
    await app.state.llm.aclose()


app = FastAPI(
    title="Autonomous Document Agent",
    version="1.0.0",
    description="Plans, writes, self-reviews and renders business documents (.docx) from a natural-language request.",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.get("/health")
async def health(request: Request):
    return {"status": "ok", "llm_chain": [e.name for e in request.app.state.llm.endpoints]}


@app.post("/agent", response_model=AgentResponse)
async def run_agent(
    request: Request,
    body: Annotated[AgentRequest, Body(openapi_examples=EXAMPLES)],
    download: Annotated[bool, Query(description="Return the .docx file itself instead of JSON")] = False,
):
    try:
        result, filename = await request.app.state.agent.run(body.request)
    except OutOfScopeError as e:
        raise HTTPException(status_code=422, detail={"status": "rejected", "reason": str(e)})
    except RateLimitedError as e:
        minutes = max(1, round(e.retry_after / 60))
        log.error("Rate limited for %.0fs: %s", e.retry_after, e)
        raise HTTPException(status_code=503, headers={"Retry-After": str(int(e.retry_after) + 1)}, detail={
            "status": "unavailable", "retry_after_s": int(e.retry_after) + 1,
            "reason": f"The free-tier LLM quota is exhausted for now. Try again in about {minutes} minute(s)."})
    except LLMError as e:
        log.error("LLM failure: %s", e)
        raise HTTPException(status_code=502, detail={"status": "error", "reason": str(e)})

    result.document.download_url = str(request.url_for("get_document", filename=filename))
    log.info("━━━━ Done in %.1fs: %s", result.elapsed_s, result.message)
    if download:
        return FileResponse(settings.output_dir / filename, media_type=DOCX_MIME, filename=filename,
                            headers={"X-Agent-Summary": result.message.encode("ascii", "ignore").decode()[:900]})
    return result


@app.get("/documents/{filename}", name="get_document")
async def get_document(filename: str):
    path = settings.output_dir / filename
    if not re.fullmatch(r"[\w\-]+\.docx", filename) or not path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(path, media_type=DOCX_MIME, filename=filename)

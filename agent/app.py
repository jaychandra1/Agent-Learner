"""HTTP API.

app (8000, internal only)
    /healthz /ready /chat /status /teams/test /security/restricted, plus /teams/webhook
    for local testing
public_app (8080, or PORT when the platform sets it: the only port reachable from outside)
    POST /teams/webhook, and the web UI's API when WEB_UI is set (web.py: POST /api/chat,
    GET /api/health). Nothing else, not even /docs. A mistake in the Cloudflare or platform
    settings can't expose the internal API this way.

main.py starts both.
"""
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import agent
import boundaries
import teams
import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pq.app")

API_KEY = os.getenv("AGENT_API_KEY", "").strip()
# Servers that died, filled in by main.py. "teams" in here makes /healthz fail.
SERVER_FAILURES: dict[str, str] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not teams.OUTGOING_SECRET:
        logger.info("TEAMS_OUTGOING_WEBHOOK_SECRET not set: /teams/webhook is disabled")
    if not teams.REPLY_TARGET:
        logger.warning("TEAMS_REPLY_WEBHOOK_URL is not set: answers can't be posted to Teams")
    if not API_KEY:
        logger.warning("AGENT_API_KEY is not set: the internal API (/chat, /status, /teams/test, /security/*) is off")
    if web.ENABLED:
        logger.info("Web UI API on for %s (access code %s, %d questions per minute per address)",
                    ", ".join(web.ORIGINS), "required" if web.ACCESS_CODE else "not set", web.RATE_PER_MINUTE)
        if not web.ACCESS_CODE:
            logger.warning("WEB_ACCESS_CODE is not set: anyone who knows this address can ask PQ questions")
    else:
        logger.info("WEB_UI not set: the web UI's API (/api/chat) is disabled")
    if boundaries.ENABLED:
        logger.info("Boundaries on: questions about PQ itself are refused and reported to %s; protected "
                    "namespaces: %s", ", ".join(name for name, _ in teams.SECURITY_TARGETS) or "the log only",
                    ", ".join(sorted(boundaries.PROTECTED_NAMESPACES)) or "none")
    else:
        logger.warning("Boundaries are off (BOUNDARIES_ENABLED=false): people in Teams can ask about PQ itself")
    yield


app = FastAPI(title="PQ", version="1.0.0", lifespan=lifespan)


def auth(key: str | None):
    """No AGENT_API_KEY configured means the internal API is off, otherwise the header must match."""
    if not API_KEY:
        raise HTTPException(status_code=403, detail="The internal API is off: set AGENT_API_KEY in the Secret")
    if not key or not hmac.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key header")


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


@app.get("/healthz")
def healthz():
    """Liveness. 503 when the Teams server died: better a restart than a pod that silently
    stopped answering."""
    if "teams" in SERVER_FAILURES:
        return JSONResponse({"status": "failing", "service": "pq",
                             "problems": [f"teams server: {SERVER_FAILURES['teams']}"]}, status_code=503)
    return {"status": "ok", "service": "pq"}


@app.get("/ready")
def ready():
    return {"status": "ready", "service": "pq"}


public_app = FastAPI(title="PQ public", docs_url=None, redoc_url=None, openapi_url=None)
teams_app = public_app   # the older name
public_app.include_router(web.router)
if web.ENABLED:
    # a browser only lets a page from these origins call the API
    public_app.add_middleware(CORSMiddleware, allow_origins=web.ORIGINS, allow_methods=["GET", "POST"],
                              allow_headers=["Content-Type", "X-Access-Code"], max_age=3600)


@teams_app.post("/teams/webhook")
@app.post("/teams/webhook")
async def teams_webhook(request: Request, authorization: str = Header(None)):
    """Teams outgoing webhook. Teams waits 5 seconds for the reply."""
    if not teams.OUTGOING_SECRET:
        raise HTTPException(status_code=404, detail="Teams outgoing webhook is not configured")
    raw = await request.body()
    if not teams.verify_hmac(raw, authorization):
        logger.warning("Rejected /teams/webhook call with a missing or wrong HMAC signature")
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")
    try:
        message = teams.parse_body(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON")
    reply = await run_in_threadpool(teams.handle_outgoing, message)
    return JSONResponse(reply)


@app.post("/chat")
def chat(body: ChatIn, x_api_key: str = Header(None)):
    """Ask PQ without Teams. Send the session_id from the reply back to continue a conversation."""
    auth(x_api_key)
    sid = body.session_id or str(uuid.uuid4())
    return {"session_id": sid, "answer": teams.answer_in_session(f"api:{sid}", body.message)}


@app.get("/status")
def status(x_api_key: str = Header(None)):
    auth(x_api_key)
    return {
        "teams": {
            "reply_webhook": teams.REPLY_TARGET[0] if teams.REPLY_TARGET else None,
            "security_targets": [name for name, _ in teams.SECURITY_TARGETS],
            "outgoing_webhook": bool(teams.OUTGOING_SECRET),
        },
        "llm": {"configured": agent.llm is not None, **(agent.llm.describe() if agent.llm else {}),
                "status": agent.LLM_STATUS},
        "web_ui": {"enabled": web.ENABLED, "origins": web.ORIGINS, "access_code": bool(web.ACCESS_CODE)},
        "boundaries": {"enabled": boundaries.ENABLED, "protected_namespaces": sorted(boundaries.PROTECTED_NAMESPACES)},
    }


@app.post("/teams/test")
def teams_test(x_api_key: str = Header(None)):
    """Send a test card to the reply webhook."""
    auth(x_api_key)
    if not teams.REPLY_TARGET:
        raise HTTPException(status_code=400, detail="TEAMS_REPLY_WEBHOOK_URL is not set")
    results = teams.post_test()
    return {"sent_to": len(results), "results": results}


@app.get("/security/restricted")
def security_restricted(limit: int = 50, x_api_key: str = Header(None)):
    """Refused questions about PQ itself, since the pod started."""
    auth(x_api_key)
    events = boundaries.recent(max(1, min(limit, boundaries.MAX_EVENTS)))
    return {"enabled": boundaries.ENABLED, "protected_namespaces": sorted(boundaries.PROTECTED_NAMESPACES),
            "count": len(events), "events": events}

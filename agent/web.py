"""Chat API for the web UI (webui/ in this repository).

The UI is a static site on another origin. It keeps every conversation on the person's own
device and sends the conversation along with each question, so nothing is stored here:

    POST /api/chat   {"messages": [{"role": "user" | "assistant", "content": "..."}, ...]}
                     -> {"answer": "..."}            the last message is the new question
    GET  /api/health -> {"status": "ok", ...}        also what a hosting platform can probe

Served on the public port only (app.public_app). It is off until WEB_UI names the UI's
origin, e.g. WEB_UI=https://pq-ui.onrender.com (comma separated for several, "*" for any).

An open endpoint in front of a paid AI model needs some care:
  WEB_UI               browsers only let pages from these origins call the API (CORS). Other
                       programs can still call it, so:
  WEB_ACCESS_CODE      optional shared code. When set, every call must carry it in the
                       X-Access-Code header; the UI asks for it once and keeps it on the device.
  WEB_RATE_PER_MINUTE  questions one address may ask per minute (default 20)

People on the web UI are treated like people in Teams: the boundaries apply (boundaries.py).
"""
import hmac
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

import agent
import boundaries
import teams

logger = logging.getLogger("pq.web")



def origins(value: str) -> list[str]:
    """WEB_UI -> the origins a browser sends: scheme and host, nothing else.
    'pq-ui.vercel.app/', 'https://pq-ui.vercel.app/chat' -> 'https://pq-ui.vercel.app'."""
    out = []
    for entry in re.split(r"[,\s]+", (value or "").strip()):
        entry = entry.strip("\"'")
        if not entry:
            continue
        if entry != "*":
            if not re.match(r"^https?://", entry, re.I):
                entry = ("http://" if entry.startswith(("localhost", "127.0.0.1")) else "https://") + entry
            scheme, rest = entry.split("://", 1)
            entry = f"{scheme.lower()}://{rest.split('/', 1)[0].lower()}"
        if entry not in out:
            out.append(entry)
    return out


ORIGINS = origins(os.getenv("WEB_UI", ""))
ENABLED = bool(ORIGINS)
ACCESS_CODE = os.getenv("WEB_ACCESS_CODE", "").strip()
try:
    RATE_PER_MINUTE = max(1, int(os.getenv("WEB_RATE_PER_MINUTE", "") or 20))
except ValueError:
    RATE_PER_MINUTE = 20

MAX_MESSAGES = 40             # of a conversation, the newest are used
MAX_MESSAGE_CHARS = 20000     # one message (a question with pasted code or logs)
MAX_WAITING = 8               # questions running or waiting, above that people get "busy"
_slots = threading.BoundedSemaphore(MAX_WAITING)
_calls: dict[str, deque] = defaultdict(deque)
_calls_lock = threading.Lock()

router = APIRouter(prefix="/api")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatIn(BaseModel):
    messages: list[Message]


def _client(request: Request) -> str:
    """The caller's address. Behind a platform's proxy that is the first X-Forwarded-For entry."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _guard(access_code: str | None):
    if not ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")
    if ACCESS_CODE and not (access_code and hmac.compare_digest(access_code.encode(), ACCESS_CODE.encode())):
        raise HTTPException(status_code=401, detail="This PQ needs an access code.")


def _rate_limit(client: str):
    now = time.time()
    with _calls_lock:
        for key in [k for k, v in _calls.items() if not v or now - v[-1] > 60]:
            del _calls[key]
        calls = _calls[client]
        while calls and now - calls[0] > 60:
            calls.popleft()
        if len(calls) >= RATE_PER_MINUTE:
            raise HTTPException(status_code=429, detail="Too many questions in a minute. Please wait a moment.")
        calls.append(now)


def conversation(messages: list[Message]) -> tuple[list[dict], str]:
    """What the device sent -> (history for agent.chat, the new question).

    The device is not trusted to send a well-formed conversation: empty messages are
    dropped, two turns of the same role in a row are joined, and the history starts with
    a question."""
    turns: list[dict] = []
    for m in messages[-MAX_MESSAGES:]:
        text = m.content.strip()
        if not text:
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(status_code=413, detail=f"A message is longer than {MAX_MESSAGE_CHARS} characters.")
        if turns and turns[-1]["role"] == m.role:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": m.role, "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if not turns or turns[-1]["role"] != "user":
        raise HTTPException(status_code=422, detail="The last message must be a question from the user.")
    return turns[:-1], turns[-1]["content"]


@router.get("/health")
def health():
    """Always answers, also with the API off: a hosting platform uses it as its health check."""
    return {"status": "ok", "service": "pq", "web_ui": ENABLED, "access_code_required": bool(ACCESS_CODE)}


@router.post("/chat")
def chat(body: ChatIn, request: Request, x_access_code: str = Header(None)):
    _guard(x_access_code)
    client = _client(request)
    _rate_limit(client)
    history, question = conversation(body.messages)
    sender = {"name": "Web UI user", "aadObjectId": client}
    logger.info("Web question (%d characters, %d earlier messages)", len(question), len(history))

    reason = boundaries.restricted_question(question)
    if reason:
        teams.report_restricted(sender, question, reason, "question filter")
        return {"answer": boundaries.REFUSAL}
    if not agent.llm:
        raise HTTPException(status_code=503, detail=agent.NO_MODEL)
    if not _slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="PQ is busy with other questions. Please try again in a minute.")
    try:
        with boundaries.restricted_access():
            answer = agent.chat(history, question)
    except Exception as e:  # the raw error can name the AI provider: log only
        logger.exception("Web request failed")
        raise HTTPException(status_code=502, detail=f"Sorry, that failed ({type(e).__name__}). Please try again.")
    finally:
        _slots.release()
    if boundaries.is_refusal(answer):  # the model refused (its boundary rule), show only that
        teams.report_restricted(sender, question, boundaries.REASON_AI, "AI rule")
        answer = boundaries.REFUSAL
    return {"answer": answer}

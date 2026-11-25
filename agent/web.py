"""Chat API for the web UI (webui/ in this repository).

The UI is a static site on another origin. It keeps every conversation on the person's own
device and sends the conversation along with each question, so nothing is stored here:

    POST /api/chat   {"messages": [{"role": "user" | "assistant", "content": "..."}, ...]}
                     -> {"answer": "..."}            the last message is the new question
                     optional: "mode": "chat" | "interview" | "mock" (how to answer), and on a user
                     message "images": ["data:image/png;base64,...", ...]
    POST /api/stream the same question, answered while the model writes (server-sent events):
                     data: {"delta": "text"} ... then data: {"done": true, "answer": "all of it"}
                     or data: {"error": "..."}
    POST /api/extract {"name": "resume.pdf", "data": "data:application/pdf;base64,..."}
                     -> {"name": ..., "text": "..."}   the text of a PDF or Word file (a resume); the
                     page keeps it on the device and sends it as text with the questions
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
import base64
import binascii
import hmac
import html
import io
import json
import logging
import zipfile
import os
import re
import threading
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
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
MAX_IMAGES = 4                # pictures one request may carry, the newest are used
MAX_IMAGE_CHARS = 3_000_000   # one picture as a data URL (about 2 MB of image)
IMAGE_ONLY = "Explain what this image shows."   # the question when a picture comes without words
_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+=*)$")
MAX_DOCUMENT_CHARS = 7_000_000  # a PDF or Word file as a data URL (about 5 MB)
MAX_DOCUMENT_TEXT = 40000       # characters of text taken from it
MAX_DOCUMENT_PAGES = 40
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_WAITING = 8               # questions running or waiting, above that people get "busy"
_slots = threading.BoundedSemaphore(MAX_WAITING)
_calls: dict[str, deque] = defaultdict(deque)
_calls_lock = threading.Lock()

router = APIRouter(prefix="/api")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = ""
    images: list[str] = []      # data URLs, only on user messages


class ChatIn(BaseModel):
    messages: list[Message]
    mode: Literal["chat", "interview", "mock"] = "chat"


class ExtractIn(BaseModel):
    name: str = "document"
    data: str


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
        images = [_image(url) for url in m.images] if m.role == "user" else []
        if not text and not images:
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(status_code=413, detail=f"A message is longer than {MAX_MESSAGE_CHARS} characters.")
        if turns and turns[-1]["role"] == m.role:
            turns[-1]["text"] = "\n\n".join(t for t in (turns[-1]["text"], text) if t)
            turns[-1]["images"] += images
        else:
            turns.append({"role": m.role, "text": text, "images": images})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    if not turns or turns[-1]["role"] != "user":
        raise HTTPException(status_code=422, detail="The last message must be a question from the user.")

    # pictures are large: only the newest few go to the model, older ones leave a note
    room = MAX_IMAGES
    for turn in reversed(turns):
        keep = turn["images"][-room:] if room else []
        dropped = len(turn["images"]) - len(keep)
        room -= len(keep)
        text = turn["text"] or (IMAGE_ONLY if turn["images"] else "")
        if dropped:
            text += f"\n\n[{dropped} image(s) sent earlier in this chat are no longer attached]"
        turn["content"] = [*keep, {"type": "text", "text": text}] if keep else text
    turns = [{"role": t["role"], "content": t["content"]} for t in turns]
    return turns[:-1], turns[-1]["content"]


def _image(url: str) -> dict:
    """A data URL from the device -> an image block (Anthropic Messages shape)."""
    if len(url) > MAX_IMAGE_CHARS:
        raise HTTPException(status_code=413, detail="An image is too large. Use a smaller picture.")
    match = _DATA_URL.match(url)
    if not match:
        raise HTTPException(status_code=422, detail="Images must be PNG, JPEG, WebP or GIF.")
    return {"type": "image", "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)}}


def _words(question) -> str:
    """The text of a question, for the filter, the log and the administrators' notices."""
    return question if isinstance(question, str) else "".join(
        b.get("text", "") for b in question if b.get("type") == "text")


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
    words = _words(question)
    logger.info("Web question (%d characters, %d earlier messages, mode %s)", len(words), len(history), body.mode)

    reason = boundaries.restricted_question(words)
    if reason:
        teams.report_restricted(sender, words, reason, "question filter")
        return {"answer": boundaries.REFUSAL}
    if not agent.llm:
        raise HTTPException(status_code=503, detail=agent.NO_MODEL)
    if not _slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="PQ is busy with other questions. Please try again in a minute.")
    try:
        with boundaries.restricted_access():
            answer = agent.chat(history, question, mode=body.mode)
    except Exception as e:  # the raw error can name the AI provider: log only
        logger.exception("Web request failed")
        raise HTTPException(status_code=502, detail=f"Sorry, that failed ({type(e).__name__}). Please try again.")
    finally:
        _slots.release()
    if boundaries.is_refusal(answer):  # the model refused (its boundary rule), show only that
        teams.report_restricted(sender, words, boundaries.REASON_AI, "AI rule")
        answer = boundaries.REFUSAL
    return {"answer": answer}


def _event(data: dict) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


# no proxy on the way may hold the pieces back or compress them into one block
_STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


@router.post("/stream")
def stream(body: ChatIn, request: Request, x_access_code: str = Header(None)):
    """Like /api/chat, but the answer arrives while it is written. Anything wrong with the
    request itself is an ordinary error reply; what goes wrong later is an "error" event."""
    _guard(x_access_code)
    client = _client(request)
    _rate_limit(client)
    history, question = conversation(body.messages)
    sender = {"name": "Web UI user", "aadObjectId": client}
    words = _words(question)
    logger.info("Web question, streamed (%d characters, %d earlier messages, mode %s)", len(words), len(history),
                body.mode)

    reason = boundaries.restricted_question(words)
    if reason:
        teams.report_restricted(sender, words, reason, "question filter")
        return StreamingResponse(iter([_event({"done": True, "answer": boundaries.REFUSAL})]),
                                 media_type="text/event-stream", headers=_STREAM_HEADERS)
    if not agent.llm:
        raise HTTPException(status_code=503, detail=agent.NO_MODEL)

    def events():
        # the slot is taken in here, so it is always given back, also when the person leaves
        slots = _slots
        if not slots.acquire(blocking=False):
            yield _event({"error": "PQ is busy with other questions. Please try again in a minute."})
            return
        parts = []
        try:
            for piece in agent.chat_stream(history, question, restricted=boundaries.ENABLED, mode=body.mode):
                parts.append(piece)
                yield _event({"delta": piece})
            answer = "".join(parts).strip()
            if boundaries.is_refusal(answer):  # the model refused (its boundary rule), show only that
                teams.report_restricted(sender, words, boundaries.REASON_AI, "AI rule")
                answer = boundaries.REFUSAL
            yield _event({"done": True, "answer": answer})
        except Exception as e:  # the raw error can name the AI provider: log only
            logger.exception("Web request failed")
            yield _event({"error": f"Sorry, that failed ({type(e).__name__}). Please try again."})
        finally:
            slots.release()

    return StreamingResponse(events(), media_type="text/event-stream", headers=_STREAM_HEADERS)


# --- reading a resume (or any PDF or Word file) ---

def _pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is in requirements.txt
        raise HTTPException(status_code=501, detail="This PQ can't read PDF files. Paste the text instead.")
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise HTTPException(status_code=422, detail="That PDF is password-protected. Save it without a password.")
        pages = [page.extract_text() or "" for page in reader.pages[:MAX_DOCUMENT_PAGES]]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422, detail="That PDF could not be read.")
    return "\n\n".join(p.strip() for p in pages if p.strip())


_PARAGRAPH = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_RUN_TEXT = re.compile(r"<w:t(?: [^>]*)?>([^<]*)</w:t>|<w:tab/>|<w:br/>")


def _docx_text(raw: bytes) -> str:
    """The paragraphs of a Word file. A .docx is a zip with the text in word/document.xml;
    the text is taken with a pattern rather than an XML parser, so no XML feature of the file
    can do anything here."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 30_000_000:  # a zip bomb, or not a resume
                raise HTTPException(status_code=413, detail="That document is too large.")
            xml = z.read(info).decode("utf-8", "replace")
    except HTTPException:
        raise
    except (zipfile.BadZipFile, KeyError, OSError):
        raise HTTPException(status_code=422, detail="That Word file could not be read. Use .docx, or a PDF.")
    lines = []
    for paragraph in _PARAGRAPH.findall(xml):
        parts = [m.group(1) if m.group(1) is not None else ("\t" if m.group(0) == "<w:tab/>" else "\n")
                 for m in _RUN_TEXT.finditer(paragraph)]
        lines.append(html.unescape("".join(parts)).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@router.post("/extract")
def extract(body: ExtractIn, request: Request, x_access_code: str = Header(None)):
    """The text of a PDF or Word file. Nothing is kept: the text goes back to the page."""
    _guard(x_access_code)
    _rate_limit(_client(request))
    if len(body.data) > MAX_DOCUMENT_CHARS:
        raise HTTPException(status_code=413, detail="That file is too large (5 MB at most).")
    header, _, payload = body.data.partition(",")
    match = re.match(r"^data:([\w.+/-]*);base64$", header)
    if not match:
        raise HTTPException(status_code=422, detail="Send the file as a data URL.")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="That file is damaged.")
    name = body.name.strip()[:120] or "document"
    if raw.startswith(b"%PDF"):
        text = _pdf_text(raw)
    elif raw.startswith(b"PK") and (match.group(1) in (DOCX, "application/zip", "application/octet-stream", "")
                                    or name.lower().endswith(".docx")):
        text = _docx_text(raw)
    else:
        raise HTTPException(status_code=422, detail="PQ reads PDF and Word (.docx) files here.")
    if not text:
        raise HTTPException(status_code=422, detail="No text was found in that file. If it is a scan, paste the "
                                                    "text, or attach it as a picture.")
    logger.info("Read a document (%d characters of text)", len(text))
    cut = len(text) > MAX_DOCUMENT_TEXT
    return {"name": name, "text": text[:MAX_DOCUMENT_TEXT] + ("\n... (cut: the document is longer)" if cut else "")}

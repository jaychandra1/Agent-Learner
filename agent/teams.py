"""Microsoft Teams integration, without a bot registration.

In: questions arrive through a Teams outgoing webhook (Manage team -> Apps -> Create an
outgoing webhook). When someone @mentions it, Teams POSTs the message here signed with
HMAC-SHA256 and shows whatever we return within 5 seconds as the reply.

Out: an explanation takes longer than 5 seconds, so the question is acknowledged right away
and the answer is an Adaptive Card POSTed to a Teams Workflows webhook (set up in the
Workflows app), as a reply in the thread the question was asked in. Notices about refused
questions go to the administrators' webhook the same way.
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

import requests

import agent
import boundaries
import cards

logger = logging.getLogger("pq.teams")


def _split(value: str) -> list[str]:
    return [v.strip() for v in re.split(r"[,\s]+", value or "") if v.strip()]


_NAMED_TARGET = re.compile(r"^([A-Za-z0-9._-]+)=(https?://\S+)$")


def parse_targets(value: str, prefix: str) -> list[tuple[str, str]]:
    """Webhook targets from a comma separated list, each entry optionally "name=url".

    'chat=https://a, https://b' -> [('chat', 'https://a'), ('security-2', 'https://b')]
    The names are what shows up in logs and /status. The URLs are as good as passwords."""
    targets = []
    for i, entry in enumerate(_split(value), start=1):
        named = _NAMED_TARGET.match(entry)
        targets.append((named.group(1), named.group(2)) if named else (f"{prefix}-{i}", entry))
    return targets


_reply = parse_targets(os.getenv("TEAMS_REPLY_WEBHOOK_URL", ""), "reply")
REPLY_TARGET = _reply[0] if _reply else None          # where answers go
# where the notices about refused questions go. Not set = they are only logged and kept
SECURITY_TARGETS = parse_targets(os.getenv("TEAMS_SECURITY_WEBHOOK_URLS", ""), "security")
OUTGOING_SECRET = os.getenv("TEAMS_OUTGOING_WEBHOOK_SECRET", "").strip()
TENANT_ID = os.getenv("TEAMS_TENANT_ID", "").strip()
ALLOWED_USER_IDS = {u.lower() for u in _split(os.getenv("TEAMS_ALLOWED_USER_IDS", ""))}

POST_ATTEMPTS = 3                # Workflows throttles above 4 requests/s (429)

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="teams-answer")
MAX_WAITING = 6                  # running + queued. Above that people get a "busy" reply
_slots = threading.BoundedSemaphore(MAX_WAITING)
_notify_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="teams-notify")  # notices for the admins
_seen_ids = deque(maxlen=500)    # Teams resends messages sometimes, answer once
_seen_lock = threading.Lock()
# chat history per channel thread (and per /chat session), capped in number and age
MAX_SESSIONS = 200
SESSION_IDLE_SECONDS = 24 * 3600
_sessions: "OrderedDict[str, dict]" = OrderedDict()
_sessions_lock = threading.Lock()


# --- outbound: Workflows webhooks ---

def _post_one(label: str, url: str, payload: dict) -> dict:
    """POST to one webhook, retrying 429s, server errors and network errors.
    Only the target's name and the error type are logged. The URL holds the webhook's
    signature and exception messages like to quote it."""
    result = {"target": label, "status": None, "ok": False}
    for attempt in range(1, POST_ATTEMPTS + 1):
        delay = 2 ** attempt
        try:
            r = requests.post(url, json=payload, timeout=15)
            result["status"] = r.status_code
            result.pop("error", None)
            if 200 <= r.status_code < 300:
                result["ok"] = True
                return result
            retryable = r.status_code == 429 or r.status_code >= 500
            retry_after = (r.headers or {}).get("Retry-After", "")
            if retry_after.isdigit():
                delay = min(int(retry_after), 30)
            logger.warning("Teams target '%s' returned %s (attempt %d/%d): %s",
                           label, r.status_code, attempt, POST_ATTEMPTS, r.text[:300])
        except Exception as e:
            result["status"], result["error"] = None, type(e).__name__
            retryable = True
            logger.warning("Teams target '%s' failed (attempt %d/%d): %s",
                           label, attempt, POST_ATTEMPTS, type(e).__name__)
        if not retryable or attempt == POST_ATTEMPTS:
            return result
        time.sleep(delay)
    return result


def post_card(targets, adaptive_card: dict, thread: dict | None = None) -> list[dict]:
    """POST a card to each (name, url) target. Never raises, returns one result per target.
    thread (from thread_of) goes into the payload so the workflow can reply in the thread
    the question was asked in."""
    if isinstance(targets, tuple):
        targets = [targets]
    payload = {**cards.message(adaptive_card), **(thread or {})}
    return [_post_one(label, url, payload) for label, url in (targets or [])]


def post_test() -> list[dict]:
    """Send the test card to the reply webhook."""
    return post_card(REPLY_TARGET, cards.test_card()) if REPLY_TARGET else []


# --- inbound: outgoing webhook ---

def verify_hmac(raw_body: bytes, auth_header: str | None, secret_b64: str = None) -> bool:
    """Teams signs the body with HMAC-SHA256 using the base64-decoded security token
    and sends `Authorization: HMAC <base64 digest>`."""
    secret_b64 = OUTGOING_SECRET if secret_b64 is None else secret_b64
    if not secret_b64 or not auth_header or not auth_header.startswith("HMAC "):
        return False
    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(key, raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, auth_header[5:].strip())


_BREAK = re.compile(r"<br\s*/?>|</(?:p|div|li|pre|h[1-6])>", re.I)


def strip_mentions(text: str) -> str:
    """'<at>PQ</at>&nbsp;what is  DNS?' -> 'what is DNS?'; 'Hi <at>PQ</at>, why?' -> 'Hi, why?'
    Teams sends the message as HTML. Line breaks are kept, people paste code and logs."""
    text = re.sub(r"<at>.*?</at>", " ", text or "", flags=re.S)
    text = _BREAK.sub("\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(r" ([,.?!])(?= |$)", r"\1", text, flags=re.M).lstrip(",.?! \n")


_THREAD_ROOT = re.compile(r";messageid=(\d+)")


def thread_of(activity: dict) -> dict:
    """Where a question was asked: {"threadMessageId", "channelId"}. Sent along with the answer.

    In a channel the conversation id is "<channel id>;messageid=<id of the thread's first
    message>". The workflow passes threadMessageId to "Reply with an adaptive card in a
    channel", which puts the answer under the question."""
    conversation = str((activity.get("conversation") or {}).get("id") or "")
    match = _THREAD_ROOT.search(conversation)
    root = match.group(1) if match else (activity.get("replyToId") or activity.get("id"))
    channel = ((activity.get("channelData") or {}).get("channel") or {}).get("id") or conversation.split(";")[0]
    out = {}
    if root:
        out["threadMessageId"] = str(root)
    if channel:
        out["channelId"] = channel
    return out


HELP = """**PQ** - your technical teacher. Mention me with any technical question, for example:

- `what is a Kubernetes operator?` - a full explanation: what it is, why it exists, how it works, a real-time scenario, an example, common mistakes
- `why does my Java HashMap lose entries with a mutable key?` - a direct answer to a doubt, with the reason
- `Kafka vs RabbitMQ` - a comparison and when to choose which
- paste an error message or a piece of code - what it means and how to fix it
- `quiz me on SQL joins`, `interview questions on system design`, `a 4-week plan to learn Docker`

Reply in the same thread to follow up: `go deeper`, `simpler please`, `another example`, `continue`.

Answers take a little while and arrive as a reply in the thread."""


def session_for(conv_id: str) -> dict:
    """{"messages", "lock"} for one conversation. Idle ones expire after a day and at most
    MAX_SESSIONS are kept (LRU)."""
    now = time.time()
    with _sessions_lock:
        for key in [k for k, v in _sessions.items() if now - v["used"] > SESSION_IDLE_SECONDS]:
            del _sessions[key]
        session = _sessions.pop(conv_id, None) or {"messages": [], "lock": threading.Lock()}
        session["used"] = now
        _sessions[conv_id] = session
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
        return session


def answer_in_session(conv_id: str, question: str) -> str:
    session = session_for(conv_id)
    with session["lock"]:
        return agent.chat(session["messages"], question)


def _submit(fn, *args):
    """Queue an answer. Returns None when too much is waiting already."""
    slots = _slots  # the one we take from is the one we give back to, whatever happens meanwhile
    if not slots.acquire(blocking=False):
        return None
    future = _pool.submit(fn, *args)
    future.add_done_callback(lambda _f: slots.release())
    return future


def _reply(text: str) -> dict:
    return {"type": "message", "text": text}


# --- boundaries (see boundaries.py) ---

def _as_teams_user(fn, *args):
    """Run fn with the boundaries on: the model then gets its boundary rule."""
    with boundaries.restricted_access():
        return fn(*args)


def report_restricted(sender: dict, question: str, reason: str, stopped_by: str) -> dict:
    """Log and keep a refused request, and send the admins a card saying who asked what
    (to TEAMS_SECURITY_WEBHOOK_URLS, if set)."""
    event = boundaries.record(sender.get("name"), sender.get("aadObjectId"), boundaries.redact(question), reason,
                              stopped_by)
    if event["notify"] and SECURITY_TARGETS:
        _notify_pool.submit(post_card, SECURITY_TARGETS, cards.restricted_card(event))
    return event


def _refuse(sender: dict, question: str, reason: str, stopped_by: str) -> dict:
    report_restricted(sender, question, reason, stopped_by)
    return _reply(boundaries.REFUSAL)


def _failure(e: Exception) -> str:
    """Error text for the person who asked. The raw error can name the AI provider, so that
    only goes to the log."""
    return f"Sorry, that failed ({type(e).__name__}). The administrators can find the reason in the log."


def _deliver_later(future, sender: dict, question: str, thread: dict | None = None):
    """Post the answer to the reply webhook, in the question's thread. A long one is several cards."""
    asked_by = sender.get("name")

    def done(f):
        try:
            answer = str(f.result())
            if boundaries.is_refusal(answer):  # the model refused (its boundary rule), show only that
                report_restricted(sender, question, boundaries.REASON_AI, "AI rule")
                answer = boundaries.REFUSAL
        except Exception as e:
            logger.exception("Teams request failed")
            answer = _failure(e)
        for card in cards.answer_cards(question, asked_by, answer):
            post_card(REPLY_TARGET, card, thread)
    future.add_done_callback(done)


def handle_outgoing(activity: dict) -> dict:
    """Handle one message from the outgoing webhook (HMAC is already checked). Returns the reply."""
    msg_id = activity.get("id")
    with _seen_lock:
        if msg_id and msg_id in _seen_ids:
            return _reply("(already answered)")
        if msg_id:
            _seen_ids.append(msg_id)

    sender = activity.get("from") or {}
    tenant = ((activity.get("channelData") or {}).get("tenant") or {}).get("id", "")
    if TENANT_ID and tenant.lower() != TENANT_ID.lower():
        logger.warning("Rejected Teams message from tenant %s", tenant)
        return _reply("This agent only answers its own organization.")
    if ALLOWED_USER_IDS and (sender.get("aadObjectId") or "").lower() not in ALLOWED_USER_IDS:
        return _reply(f"Sorry {sender.get('name', '')}, you're not on this agent's allowed-users list.")

    text = strip_mentions(activity.get("text", ""))
    conv_id = (activity.get("conversation") or {}).get("id", "default")
    thread = thread_of(activity)
    if not text or text.lower() in ("help", "help?", "hi", "hello", "hey"):
        return _reply(HELP)
    logger.info("Teams question from %s (%d characters)", sender.get("name"), len(text))

    # questions about PQ itself are refused (and reported) before anything runs
    reason = boundaries.restricted_question(text)
    if reason:
        return _refuse(sender, text, reason, "question filter")

    if not agent.llm:
        return _reply(agent.NO_MODEL)
    if not REPLY_TARGET:
        return _reply("I can't send my answer: no reply webhook is set up. Please ask an administrator.")
    future = _submit(_as_teams_user, answer_in_session, conv_id, text)
    if future is None:
        return _reply("I'm busy with other questions right now. Please ask again in a few minutes.")
    _deliver_later(future, sender, text, thread=thread)
    return _reply("Working on it - the explanation will follow in this thread shortly.")


def parse_body(raw_body: bytes) -> dict:
    return json.loads(raw_body.decode("utf-8"))

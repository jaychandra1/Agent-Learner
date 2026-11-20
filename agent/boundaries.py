"""Boundaries: people in Teams can ask about any technology, not about PQ itself.

There are two layers (docs/RULES.md section 3), neither has to be perfect alone:
  1. restricted_question()  regex filter on the question, refuses before any work is done
  2. agent.BOUNDARY_RULE    a rule in the system prompt, for what a regex can't see

PQ has no tools and reads no system, so there is nothing for a request to reach behind
these two: the model knows nothing about its own installation beyond its rules.

The person gets REFUSAL as the answer. Every refusal is kept (recent()) and reported to the
admins (teams.report_restricted).

The internal API (needs AGENT_API_KEY) and the CLI are not restricted.
"""
import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

logger = logging.getLogger("pq.boundaries")

ENABLED = os.getenv("BOUNDARIES_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
_raw = os.getenv("PROTECTED_NAMESPACES", "").strip() or "pq"
# Namespaces nobody in Teams may ask about. "none" protects no namespace, the rest of the filter still runs.
PROTECTED_NAMESPACES = frozenset() if not ENABLED or _raw.lower() == "none" else frozenset(
    n.lower() for n in re.split(r"[,\s]+", _raw) if n)

REFUSAL = "You are not authorised to ask this question. It has been reported to the administrators."
_REFUSAL_MARK = re.compile(r"not\s+authori[sz]ed\s+to\s+ask\s+this\s+question", re.I)

# refusal reasons, shown to the admins
REASON_SETUP = "asked how PQ is built, installed or configured"
REASON_MODEL = "asked which AI model or provider PQ uses"
REASON_RULES = "asked for PQ's rules or prompt, or tried to get around them"
REASON_PROTECTED = "asked about PQ's own namespace or objects"
REASON_AI = "the AI recognised a question about PQ itself"

MAX_EVENTS = 200           # kept in memory for /security/restricted
MAX_CARDS_PER_HOUR = 20    # above this, refusals are only logged and kept


# --- which requests the boundaries apply to ---

_restricted: ContextVar[bool] = ContextVar("pq_restricted", default=False)


@contextmanager
def restricted_access(active: bool = True):
    """Everything inside runs with the boundaries on (agent.py then adds its boundary rule).
    Context vars don't follow work into another thread, so code that hands off to a thread
    has to enter this again there."""
    token = _restricted.set(bool(active) and ENABLED)
    try:
        yield
    finally:
        _restricted.reset(token)


def is_restricted() -> bool:
    return _restricted.get()


def is_refusal(answer: str | None) -> bool:
    """Did the model answer with the refusal sentence (layer 2)?"""
    return ENABLED and bool(_REFUSAL_MARK.search(answer or ""))


# --- secrets in what we keep and report ---

_KV = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)(\"?\s*[:=]\s*\"?)([^\s\",;]+)")
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@")
_OTHER = [re.compile(r"AKIA[0-9A-Z]{16}"),
          re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")]


def redact(text: str) -> str:
    """Hide things that look like credentials. People paste them into questions."""
    if not text:
        return ""
    text = _KV.sub(r"\1\2[REDACTED]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    for p in _OTHER:
        text = p.sub("[REDACTED]", text)
    return text


# --- layer 1: the question filter ---
# Precision over recall. PQ teaches, and learners write "you" and "your" for people in
# general all the time ("how do you run a container?", "where do you keep your API keys?").
# Refusing one of those reports an innocent person to the administrators. So:
#   - a pattern needs PQ as its subject: "PQ", "this bot", or a "you" that can only be PQ
#   - a pattern with a plain "you" must end right after the verb ("how do you work?"), and
#     one with a plain "your" needs a direct request in front ("show me your configuration")
# What slips through here is caught by layer 2.

_YOU = r"(?:you|u|yourself)"
_NAMED = (r"(?:pq|this\s+(?:whole\s+|entire\s+)?(?:agent|bot|chat\s?bot|assistant|tutor|integration|automation)|"
          r"the\s+(?:bot|chat\s?bot|assistant))")
_SELF = rf"(?:{_YOU}|{_NAMED})"
_YOUR = r"(?:your|ur)"
_NAMED_OWNER = (r"(?:pq(?:'?s)?|this\s+(?:agent|bot|chat\s?bot|assistant|tutor)(?:'?s)?|"
                r"the\s+(?:bot|chat\s?bot|assistant)(?:'?s)?)")
_ADVERB = r"(?:(?:even|actually|really|currently|originally|initially|all|now|being|been|get|got|getting)\s+)*"
_ADJECTIVE = r"(?:(?:own|current|full|complete|exact|internal|underlying|whole|entire|real|actual)\s+)*"
_BUILT = (r"(?:set[\s-]?up|configured|installed|deployed|built|made|created|developed|implemented|hosted|running|"
          r"located|designed|integrated|connected|secured|authenticated|programmed|coded|written|trained|wired|"
          r"provisioned|architected)")
_BUILD = (r"(?:set[\s-]?up|configured?|install(?:ed)?|deploy(?:ed)?|buil[dt]|created?|develop(?:ed)?|"
          r"implement(?:ed)?|host(?:ed|s)?|made|wrote|written|design(?:ed)?|maintains?|owns?|manages?|operates?|"
          r"programmed|trained|provisioned)")
_PART = (r"(?:set[\s-]?up|config(?:uration|map|s)?|install(?:ation)?|deployment|architecture|design|infra(?:structure)?|"
         r"internals?|implementation|source(?:\s+code)?|code(?:base)?|repo(?:sitory)?|git\s?hub|docker\s?file|"
         r"docker\s+image|image|manifests?|yaml|helm\s+chart|env(?:ironment)?(?:\s+variables?)?|settings?|"
         r"system\s+prompt|prompt|instructions?|rules|guidelines|guardrails|restrictions|boundaries|"
         r"(?:ai\s+|llm\s+|language\s+)?model|llm|provider|api[\s-]?keys?|keys?|tokens?|secrets?|credentials?|"
         r"passwords?|webhooks?(?:\s+urls?)?|tunnel|certificates?|permissions?|access\s+rights|rbac|roles?|"
         r"service\s?account|namespace|pods?|logs?|containers?|hosting|tech(?:nology)?\s+stack|stack|backend|"
         r"creator|developers?|owner|admin(?:istrator)?s?|maintainers?|version)")
# after a plain "your" only these are surely about PQ ("your deployment" is anyone's deployment)
_OWN_PART = (r"(?:system\s+prompt|guardrails|creators?|(?:ai|llm|language)\s+model|llm|(?:ai|llm|model)\s+provider|"
             r"internals)")
_REVEAL = (r"(?:what(?:'s|\s+is|\s+are|\s+was)|show(?:\s+me)?|give(?:\s+me)?|send(?:\s+me)?|"
           r"tell(?:\s+me)?(?:\s+about)?|share|reveal|print|display|dump|leak|expose|output|repeat|summari[sz]e|"
           r"translate)")
_ASK_FOR = r"(?:" + _REVEAL + r"|explain|describe)"
_VENDOR = (r"(?:chat\s?gpt|gpt[-\s]?\d[\w.]*|gpt|open\s?ai|claude|anthropic|gemini|llama|cursor|groq|copilot|mistral|"
           r"ollama|open\s?router|deepseek)")
_INSIDE = (r"(?:\s+(?:internally|exactly|actually|really|under\s+the\s+hood|behind\s+the\s+scenes|"
           r"in\s+the\s+background|right\s+now|currently|here))*")
_STOP = r"\s*(?:[?.!,]|$)"   # the sentence ends here: nothing else can be the subject
_END = r"[^.?!\n]"           # stay inside one sentence
_HOW = r"\b(?:how|where|when|why)\b"
_WORKS = (r"(?:work|function|operate|authenticate|connect|talk|communicate|run|live|"
          r"get\s+(?:access|deployed|installed|built|configured|set[\s-]?up))")
_MODEL_VERB = (r"(?:us(?:e|es|ing)|run(?:s|ning)?(?:\s+on)?|built(?:\s+(?:on|with))?|based(?:\s+on)?|"
               r"powered(?:\s+by)?|trained(?:\s+on)?|call(?:s|ing)?)")
_HOLDS = (r"(?:us(?:e|ing)|ha(?:ve|s)|read(?:ing)?|hold(?:ing)?|stor(?:e|ing)|load(?:ing)?|"
          r"run(?:ning)?\s+(?:as|with|under))")
_CREDENTIALS = (r"(?:webhooks?(?:\s+urls?)?|api[\s-]?keys?|tokens?|secrets?|credentials?|passwords?|"
                r"env(?:ironment)?\s+variables?|config\s?maps?|tunnel|certificates?|service\s?accounts?|roles?|"
                r"permissions?|access|privileges|rights)")


# A short namespace name can be an ordinary word: "pq" is also a priority queue ("insert in
# PQ", "a PQ-tree"). The two loose forms below therefore only count when the question also
# talks about Kubernetes objects.
_K8S = (r"^(?=.*\b(?:pods?|logs?|deploy(?:ment)?s?|services?|svc|config\s?maps?|secrets?|containers?|images?|"
        r"kubectl|describe|rollout|events?|replicas?|network\s?polic(?:y|ies)|ingress(?:es)?)\b).*?")


def _protected_patterns() -> list[str]:
    out = []
    for p in sorted(PROTECTED_NAMESPACES):
        name = re.escape(p)
        out += [_K8S + rf"(?<![\w.-]){name}[-/:][\w.-]+",                               # logs of pq-tunnel, pq/pod
                _K8S + rf"\b(?:in|from|inside)\s+[\"'`]?{name}(?![\w-])",                # pods in pq
                rf"\b(?:namespace|ns|-n)\s+[\"'`]?{name}(?![\w-])",                      # what is in namespace pq
                rf"(?<![\w-]){name}[\"'`]?\s+(?:namespace|ns)\b"]                       # the pq namespace
    return out


_CHECKS = [(reason, [re.compile(p, re.I) for p in patterns]) for reason, patterns in (
    (REASON_RULES, [
        # "print the system prompt", but not "what is the system prompt in the OpenAI API"
        r"\b(?:" + _REVEAL + r"|ignore|forget)\s+(?:the|this|that)\s+" + _ADJECTIVE
        + r"(?:system|initial|original|hidden|developer|base)\s+(?:prompt|instructions?|message)\b"
        r"(?!\s+(?:in|of|for|to|from|on|with|when|means?|is|does|do|work|concept|role|field|parameter)\b)",
        r"\b" + _YOUR + r"\s+" + _ADJECTIVE + r"(?:system|initial|original|hidden|developer|base)\s+"
        r"(?:prompt|instructions?|message)\b",
        # "ignore your previous instructions", "disregard all the rules"
        r"\b(?:ignore|disregard|forget|override|bypass|skip|drop)\b" + _END + r"{0,20}?\b(?:your|previous|prior|above|"
        r"earlier|system|safety|these|those|all\s+(?:of\s+)?(?:the|your|previous|prior))\s+"
        r"(?:(?:previous|prior|above|earlier|own|system|safety)\s+)*"
        r"(?:instructions?|rules|prompt|restrictions|boundaries|guardrails|guidelines)\b",
        r"\b(?:repeat|print|output|show|reveal|display|dump|translate|summari[sz]e)\b" + _END + r"{0,30}\b(?:everything|all|"
        r"the\s+(?:text|message|words|instructions|prompt))\s+(?:above|before\s+this)\b",
        r"\b(?:dan|jailbreak|unrestricted)\s+mode\b",
        # "enable developer mode", but not "how do I enable developer mode in Chrome"
        r"\b(?:enable|enter|activate|switch\s+to|go\s+into|turn\s+on|you\s+are\s+(?:now\s+)?in)\s+(?:your\s+)?"
        r"(?:developer|god|sudo)\s+mode\b(?!\s+(?:in|on|for|of|with)\b)",
    ]),
    (REASON_MODEL, [
        # "which model are you using?", but not "which model do you use for image classification"
        r"\b(?:which|what)\s+(?:(?:ai|llm|language)\s+)?(?:model|llm)\b" + _END + r"{0,15}\b" + _YOU + r"\b" + _END
        + r"{0,15}\b" + _MODEL_VERB + _INSIDE + _STOP,
        r"\b(?:which|what)\s+(?:(?:ai|llm|language)\s+)?(?:model|llm)\b" + _END + r"{0,15}\b" + _NAMED + r"\b" + _END
        + r"{0,15}\b" + _MODEL_VERB + r"\b",
        r"\b(?:which|what)\s+(?:(?:ai|llm|language)\s+)?(?:model|llm|ai)\b" + _END
        + r"{0,15}\b(?:powers?|is\s+behind|drives?|runs)\s+" + _SELF + r"\b",
        r"\b(?:which|what)\s+(?:ai|llm|model)\s+(?:provider|vendor|company)\b" + _END + r"{0,15}\b(?:is\s+this|"
        r"(?:is|are|does|do)\s+" + _SELF + r")\b",
        r"\bare\s+(?:you|u)\s+(?:(?:really|actually|based\s+on|powered\s+by|built\s+(?:on|with)|using|running\s+on|"
        r"running|an?)\s+)*" + _VENDOR + r"\b(?!\s+(?:api|sdk|cli|code|rules?|agents?|models?|pro|plus)\b)",
        # "do you use openai?", but not "how do you use the OpenAI API"
        r"(?<!how )(?<!when )(?<!why )(?<!where )(?<!what )\b(?:do|does)\s+" + _SELF + r"\s+use\s+(?:an?\s+)?(?:"
        + _VENDOR + r"|llm|ai\s+model|language\s+model)\b(?!\s+(?:api|sdk|cli|code|models?|embeddings?|functions?|"
        r"with|in|for|to|as)\b)",
    ]),
    (REASON_PROTECTED, _protected_patterns()),
    (REASON_SETUP, [
        # "how are you set up", "where is PQ hosted", "how was this bot built"
        _HOW + _END + r"{0,30}?\b(?:is|are|was|were|r)\s+" + _SELF + r"\s+" + _ADVERB + _BUILT + r"\b",
        # "explain how you were built", "how PQ is deployed"
        r"\b(?:how|where)\s+(?:exactly\s+)?" + _YOU + r"\s+(?:is|are|was|were|got|gets|has\s+been|have\s+been|is\s+been)\s+"
        + _ADVERB + _BUILT + r"\b",
        _HOW + _END + r"{0,30}?\b" + _NAMED + r"\s+(?:is|are|was|were|got|gets|has\s+been|have\s+been|is\s+been)\s+"
        + _ADVERB + _BUILT + r"\b",
        _HOW + _END + r"{0,30}?\b(?:has|have)\s+" + _SELF + r"\s+been\s+" + _ADVERB + _BUILT + r"\b",
        # "how do you work?", "where do you run?"; not "how do you run a container"
        r"\b(?:how|where)\b" + _END + r"{0,20}?\b(?:do|does|did)\s+" + _YOU + r"\s+" + _ADVERB + _WORKS + _INSIDE + _STOP,
        # "how does PQ authenticate to Teams", "how does this bot work"
        r"\b(?:how|where)\b" + _END + r"{0,20}?\b(?:do|does|did)\s+" + _NAMED + r"\s+" + _ADVERB + _WORKS + r"\b",
        # "who built you", "how did they deploy PQ", "how do I install this bot"
        r"\b(?:how|who|when|where|why)\b" + _END + r"{0,30}?\b" + _BUILD + r"\s+" + _SELF + r"\b",
        r"\b(?:install|deploy|replicate|clone|reproduce|recreate|rebuild)\s+" + _SELF + r"\b",
        # "what are you built with", "which language is PQ written in"
        r"\b(?:what|which)\b" + _END + r"{0,30}?\b(?:is|are|was|were)\s+" + _SELF + r"\s+" + _ADVERB
        + r"(?:built|made|written|coded|programmed|developed|based|powered|running|hosted|deployed|implemented)\s+"
        r"(?:on|with|in|by|of|using|from|upon)\b",
        # "PQ's configuration", "this bot's permissions"
        r"\b" + _NAMED_OWNER + r"\s+" + _ADJECTIVE + _PART + r"\b",
        # "your system prompt", "your own source code"
        r"\b" + _YOUR + r"\s+" + _ADJECTIVE + _OWN_PART + r"\b",
        r"\b" + _YOUR + r"\s+own\s+" + _ADJECTIVE + _PART + r"\b",
        # "what is your configuration", "share your source code"; not "where do you keep your API keys"
        r"\b" + _ASK_FOR + r"\s+(?:all\s+(?:of\s+)?)?" + _YOUR + r"\s+" + _ADJECTIVE + _PART + r"\b",
        # "the architecture of this bot", "the model behind you"
        r"\b" + _PART + r"\s+(?:of|behind|used\s+by|that\s+powers?|powering)\s+" + _SELF + r"\b",
        # "which service account do you use", "what permissions do you have"
        r"\b" + _CREDENTIALS + r"\b" + _END + r"{0,15}\b(?:do|does|did|are|is)\s+" + _YOU + r"\s+" + _HOLDS + _INSIDE + _STOP,
        r"\b" + _CREDENTIALS + r"\b" + _END + r"{0,15}\b(?:do|does|did|are|is)\s+" + _NAMED + r"\s+" + _HOLDS + r"\b",
    ]),
)]


def restricted_question(text: str) -> str | None:
    """Returns why the question is refused, or None if it's fine."""
    if not ENABLED or not text:
        return None
    text = re.sub(r"\s+", " ", text.replace("\u2019", "'").replace("\u2018", "'")).strip()
    for reason, patterns in _CHECKS:
        if any(p.search(text) for p in patterns):
            return reason
    return None


# --- who asked what ---

_events: deque = deque(maxlen=MAX_EVENTS)
_card_times: deque = deque()
_lock = threading.Lock()


def record(user: str, user_id: str, question: str, reason: str, stopped_by: str) -> dict:
    """Keep and log one refused request (question has to be redacted already).
    The returned event has notify=False once this hour's card limit is used up."""
    now = time.time()
    event = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user": user or "unknown",
             "user_id": user_id or "", "question": (question or "")[:600], "reason": reason, "stopped_by": stopped_by}
    with _lock:
        while _card_times and now - _card_times[0] > 3600:
            _card_times.popleft()
        event["notify"] = len(_card_times) < MAX_CARDS_PER_HOUR
        if event["notify"]:
            _card_times.append(now)
        event["last_card_this_hour"] = event["notify"] and len(_card_times) == MAX_CARDS_PER_HOUR
        _events.append(event)
    logger.warning("Refused a restricted request from %s (%s), stopped by the %s: %s | %s",
                   event["user"], event["user_id"] or "no user id", stopped_by, reason, event["question"][:300])
    return event


def recent(limit: int = 50) -> list[dict]:
    with _lock:
        return list(_events)[-limit:]

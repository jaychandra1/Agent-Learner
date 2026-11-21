"""Streaming: the answer reaches the web UI while the model writes it.

Provider -> agent.chat_stream -> POST /api/stream (server-sent events). No request leaves
the machine."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import agent
import app as app_module
import boundaries
import teams
import web
from conftest import fake_llm
from providers import ChatProvider, Limits, ProviderError, response
from providers import openai_compat as oc
from providers.catalog import FREE_TIER, LARGE
from providers.claude import FALLBACK_BETA, ClaudeProvider
from providers.openai_compat import OpenAICompatibleProvider

UI = "https://ui.example.invalid"
WEBUI_DIR = Path(__file__).resolve().parents[2] / "webui"
LIMITS = Limits(steps=1, tool_chars=1, request_chars=0, output_tokens=100, timeout=5)


def text(t):
    return NS(type="text", text=t)


def pieces(stream):
    """(the text pieces, the final response) of a provider stream."""
    items = list(stream)
    return [i for i in items if isinstance(i, str)], next((i for i in items if not isinstance(i, str)), None)


# --- The provider base: streaming when it works, one piece when it doesn't ---

class Scripted(ChatProvider):
    def __init__(self, stream_items=None, answer="whole answer"):
        super().__init__("fake", "Fake", "fake-1", LIMITS)
        self.stream_items, self.answer, self.created = stream_items, answer, 0

    def create(self, system, messages, tools, require_tool=None):
        self.created += 1
        return response("end_turn", [text(self.answer)])

    def _stream(self, system, messages, tools):
        if self.stream_items is None:
            raise NotImplementedError
        for item in self.stream_items:
            if isinstance(item, Exception):
                raise item
            yield item


def test_provider_without_streaming_answers_in_one_piece():
    provider = Scripted()
    got, final = pieces(provider.stream("SYS", [], []))
    assert got == ["whole answer"] and final.stop_reason == "end_turn" and provider.created == 1


def test_streaming_that_fails_before_the_first_word_falls_back():
    provider = Scripted(stream_items=[ProviderError("HTTP 429")])
    got, final = pieces(provider.stream("SYS", [], []))
    assert got == ["whole answer"] and provider.created == 1  # create() has the retries


def test_streaming_that_breaks_midway_is_an_error():
    provider = Scripted(stream_items=["Half an ", ProviderError("connection lost")])
    stream = provider.stream("SYS", [], [])
    assert next(stream) == "Half an "
    with pytest.raises(ProviderError):
        next(stream)
    assert provider.created == 0  # no second answer glued onto the first half


# --- OpenAI-compatible providers ---

class FakeStreamHTTP:
    def __init__(self, monkeypatch, status=200, lines=()):
        self.calls, self.closed = [], False
        self.status, self.lines = status, [line.encode() for line in lines]
        monkeypatch.setattr(oc.requests, "post", self.post)

    def post(self, url, json=None, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "json": json, "stream": stream})
        if not stream:  # the fallback to create()
            body = {"choices": [{"message": {"content": "in one piece"}, "finish_reason": "stop"}]}
            return NS(status_code=200, json=lambda: body, text="", headers={})
        return NS(status_code=self.status, iter_lines=lambda: iter(self.lines), close=self._close, text="", headers={})

    def _close(self):
        self.closed = True


def sse(delta=None, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": {"content": delta} if delta else {}, "finish_reason": finish}]})


def groq():
    return OpenAICompatibleProvider("groq", "Groq", "openai/gpt-oss-120b", FREE_TIER,
                                    base_url="https://api.groq.com/openai/v1", api_key="gsk_test_key")


def test_openai_stream_yields_text_as_it_arrives(monkeypatch):
    http = FakeStreamHTTP(monkeypatch, lines=[": keep-alive", sse("DNS turns "), "", sse("names into "), "data: not json",
                                              sse("addresses."), sse(finish="stop"), "data: [DONE]", sse("ignored")])
    got, final = pieces(groq().stream("SYS", [{"role": "user", "content": "what is DNS?"}], []))
    assert got == ["DNS turns ", "names into ", "addresses."]
    assert final.stop_reason == "end_turn" and final.content[0].text == "DNS turns names into addresses."
    (call,) = http.calls
    assert call["stream"] is True and call["json"]["stream"] is True and "tools" not in call["json"]
    assert call["json"]["messages"][-1] == {"role": "user", "content": "what is DNS?"}
    assert http.closed


def test_openai_stream_reports_a_cut_off_answer(monkeypatch):
    FakeStreamHTTP(monkeypatch, lines=[sse("A long lesson that sto"), sse(finish="length"), "data: [DONE]"])
    _, final = pieces(groq().stream("SYS", [{"role": "user", "content": "q"}], []))
    assert final.stop_reason == "max_tokens"


def test_openai_stream_error_status_falls_back_to_the_retrying_call(monkeypatch):
    http = FakeStreamHTTP(monkeypatch, status=429)
    got, _ = pieces(groq().stream("SYS", [{"role": "user", "content": "q"}], []))
    assert got == ["in one piece"]
    assert [c["stream"] for c in http.calls] == [True, False] and http.closed


# --- Claude ---

class FakeClaudeStream:
    def __init__(self, parts, final):
        self.text_stream, self._final, self.closed = iter(parts), final, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def get_final_message(self):
        return self._final


def claude(fallbacks="default"):
    provider = ClaudeProvider("anthropic", "Anthropic Claude", "claude-opus-5-5", LARGE, api_key="sk-ant", effort="high",
                              fallbacks=fallbacks)
    calls = []
    final = NS(stop_reason="end_turn", content=[NS(type="thinking", thinking="..."), text("A mutex is a lock.")])
    manager = FakeClaudeStream(["A mutex ", "", "is a lock."], final)

    def stream(**kw):
        calls.append(kw)
        return manager
    provider._client = NS(messages=NS(stream=stream), beta=NS(messages=NS(stream=stream)))
    return provider, calls, manager, final


def test_claude_stream():
    provider, calls, manager, final = claude()
    got, last = pieces(provider.stream("SYS", [{"role": "user", "content": "q"}], []))
    assert got == ["A mutex ", "is a lock."] and last is final and manager.closed
    (sent,) = calls
    assert sent["system"] == "SYS" and sent["max_tokens"] == LARGE.output_tokens and "tools" not in sent
    assert sent["output_config"] == {"effort": "high"}
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"


def test_claude_stream_without_the_refusal_fallback():
    provider, calls, _, _ = claude(fallbacks="off")
    pieces(provider.stream("SYS", [{"role": "user", "content": "q"}], []))
    assert "betas" not in calls[0] and "fallbacks" not in calls[0]


# --- agent.chat_stream ---

def streaming_llm(monkeypatch, items, **settings):
    seen = []

    def stream(system, messages, tools):
        seen.append(NS(system=system, messages=[dict(m) for m in messages], tools=tools))
        for item in items:
            if isinstance(item, Exception):
                raise item
            yield item
    monkeypatch.setattr(agent, "llm", fake_llm(stream=stream, **settings))
    return seen


def test_chat_stream_yields_pieces_and_keeps_the_conversation(monkeypatch):
    final = response("end_turn", [text("DNS turns names into addresses.")])
    seen = streaming_llm(monkeypatch, ["DNS turns ", "names into addresses.", final])
    history = []
    assert list(agent.chat_stream(history, "what is DNS?")) == ["DNS turns ", "names into addresses."]
    assert seen[0].system == agent.SYSTEM and seen[0].tools == []
    assert history == [{"role": "user", "content": "what is DNS?"}, {"role": "assistant", "content": final.content}]


def test_chat_stream_takes_the_boundary_rule_as_a_parameter(monkeypatch):
    """The web API runs the generator across threads: the request's context is not there."""
    seen = streaming_llm(monkeypatch, ["ok", response("end_turn", [text("ok")])])
    list(agent.chat_stream([], "q", restricted=True))
    list(agent.chat_stream([], "q", restricted=False))
    assert [s.system for s in seen] == [agent.SYSTEM_RESTRICTED, agent.SYSTEM]


def test_chat_stream_marks_a_cut_off_answer(monkeypatch):
    streaming_llm(monkeypatch, ["A long lesson", response("max_tokens", [text("A long lesson")])])
    got = list(agent.chat_stream([], "teach me Kubernetes"))
    assert got[0] == "A long lesson" and got[-1].endswith(agent.CUT_OFF)


@pytest.mark.parametrize("items,shown", [
    ([response("refusal", [])], agent.DECLINED),
    ([response("end_turn", [])], agent.NO_ANSWER),
])
def test_chat_stream_without_an_answer_leaves_the_history_alone(monkeypatch, items, shown):
    streaming_llm(monkeypatch, items)
    history = [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}]
    assert list(agent.chat_stream(history, "q1")) == [shown] and len(history) == 2


def test_chat_stream_error_or_stop_leaves_the_history_alone(monkeypatch):
    streaming_llm(monkeypatch, ["Half ", RuntimeError("connection lost")])
    history = []
    with pytest.raises(RuntimeError):
        list(agent.chat_stream(history, "q"))
    assert history == []

    streaming_llm(monkeypatch, ["Half ", "an answer", response("end_turn", [text("Half an answer")])])
    stream = agent.chat_stream(history, "q")
    assert next(stream) == "Half "
    stream.close()  # the person pressed Stop
    assert history == []


def test_chat_stream_with_a_provider_that_only_has_create(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm(create=lambda system, messages, tools: response("end_turn", [text("one piece")])))
    assert list(agent.chat_stream([], "q")) == ["one piece"]


def test_chat_stream_without_a_model():
    assert agent.llm is None and list(agent.chat_stream([], "q")) == [agent.NO_MODEL]


# --- POST /api/stream ---

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(teams, "post_card", lambda *a, **k: [])
    web._calls.clear()
    boundaries._events.clear()
    boundaries._card_times.clear()
    yield
    web._calls.clear()
    boundaries._events.clear()


@pytest.fixture
def client():
    with TestClient(app_module.public_app) as c:
        yield c


def events(reply):
    return [json.loads(line[5:]) for line in reply.text.splitlines() if line.startswith("data:")]


def ask(client, messages, **headers):
    return client.post("/api/stream", json={"messages": messages}, headers={"Origin": UI, **headers})


def user(content):
    return {"role": "user", "content": content}


def test_stream_endpoint_sends_pieces_then_the_whole_answer(client, monkeypatch):
    seen = streaming_llm(monkeypatch, ["DNS turns ", "names into addresses.", response("end_turn", [text("x")])])
    r = ask(client, [user("what is DNS?"), {"role": "assistant", "content": "..."}, user("go deeper")])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "no-transform" in r.headers["cache-control"] and r.headers["x-accel-buffering"] == "no"
    assert events(r) == [{"delta": "DNS turns "}, {"delta": "names into addresses."},
                         {"done": True, "answer": "DNS turns names into addresses."}]
    assert seen[0].system == agent.SYSTEM_RESTRICTED  # web users: boundaries on
    assert [m["role"] for m in seen[0].messages] == ["user", "assistant", "user"]  # the history came from the device


def test_stream_endpoint_keeps_non_ascii_text(client, monkeypatch):
    streaming_llm(monkeypatch, ["naïve → 日本語", response("end_turn", [text("x")])])
    assert events(ask(client, [user("q")]))[-1]["answer"] == "naïve → 日本語"


def test_stream_endpoint_refuses_questions_about_pq(client, monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm(stream=lambda *a: pytest.fail("the model must not see the question")))
    r = ask(client, [user("which model are you using?")])
    assert events(r) == [{"done": True, "answer": boundaries.REFUSAL}]
    assert boundaries.recent()[0]["stopped_by"] == "question filter"


def test_stream_endpoint_replaces_a_refusal_by_the_model(client, monkeypatch):
    streaming_llm(monkeypatch, ["I run on a server. ", boundaries.REFUSAL, response("end_turn", [text("x")])])
    got = events(ask(client, [user("erklaer mir wie dieses ding gebaut wurde")]))
    assert got[-1] == {"done": True, "answer": boundaries.REFUSAL}  # the UI replaces what it showed so far
    assert boundaries.recent()[0]["stopped_by"] == "AI rule"


def test_stream_endpoint_reports_a_failure_as_an_event(client, monkeypatch):
    streaming_llm(monkeypatch, ["Half ", RuntimeError("Cursor key cur-123 at api2.cursor.sh")])
    r = ask(client, [user("q")])
    got = events(r)
    assert got[0] == {"delta": "Half "} and "RuntimeError" in got[-1]["error"]
    assert "cur-123" not in r.text and "cursor.sh" not in r.text
    assert events(ask(client, [user("again")]))[-1].get("error")  # the slot was given back: not "busy"
    assert "busy" not in events(ask(client, [user("again")]))[-1]["error"]


def test_stream_endpoint_checks_the_request_before_it_streams(client, monkeypatch):
    streaming_llm(monkeypatch, ["x", response("end_turn", [text("x")])])
    assert ask(client, [{"role": "assistant", "content": "a"}]).status_code == 422
    monkeypatch.setattr(web, "ACCESS_CODE", "open-sesame")
    assert ask(client, [user("q")]).status_code == 401
    assert ask(client, [user("q")], **{"X-Access-Code": "open-sesame"}).status_code == 200
    monkeypatch.setattr(web, "ENABLED", False)
    assert ask(client, [user("q")], **{"X-Access-Code": "open-sesame"}).status_code == 404


def test_stream_endpoint_without_a_model(client):
    assert agent.llm is None and ask(client, [user("q")]).status_code == 503


def test_stream_endpoint_busy(client, monkeypatch):
    import threading
    streaming_llm(monkeypatch, ["x", response("end_turn", [text("x")])])
    monkeypatch.setattr(web, "_slots", threading.BoundedSemaphore(1))
    web._slots.acquire()
    assert "busy" in events(ask(client, [user("q")]))[0]["error"]


# --- The UI and its proxy ---

def test_ui_reads_the_stream_and_the_proxy_passes_it_on():
    app_js = (WEBUI_DIR / "app.js").read_text(encoding="utf-8")
    assert '"/api/stream"' in app_js and "getReader()" in app_js and "requestAnimationFrame" in app_js
    assert '"/api/chat"' in app_js  # still there for a backend that can't stream
    proxy = (WEBUI_DIR / "api" / "stream.js").read_text(encoding="utf-8")
    assert "process.env.BACKEND_URL" in proxy and "text/event-stream" in proxy and "res.write(chunk)" in proxy
    vercel = json.loads((WEBUI_DIR / "vercel.json").read_text(encoding="utf-8"))
    assert {"api/chat.js", "api/stream.js"} <= set(vercel["functions"])

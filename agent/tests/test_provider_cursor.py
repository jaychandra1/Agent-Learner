"""Cursor provider, with a stand-in for the Cursor SDK: no Cursor process or API is used.

The fake agent runs a "script" for each message: it may call the custom tools it was
given, exactly as Cursor's agent would, and returns a final text."""
import sys
import time
from types import SimpleNamespace as NS

import pytest

import agent
from providers import ProviderError
from providers.catalog import AGENT
from providers.cursor import WORKDIR, CursorProvider


def fake_sdk(script=None, status="finished", delay=0.0):
    """A module shaped like cursor_sdk. `script(agent, text)` plays the agent's part."""
    created = []

    class FakeRun:
        def __init__(self, owner, text):
            self.owner, self.text, self.cancelled = owner, text, False

        def wait(self):
            time.sleep(delay)
            result = script(self.owner, self.text) if script else "ok"
            return NS(status=status, result=result, error_message="boom" if status != "finished" else None)

        def cancel(self):
            self.cancelled = True

    class FakeAgent:
        def __init__(self, options):
            self.options, self.sent, self.closed, self.runs = options, [], False, []

        @classmethod
        def create(cls, options):
            created.append(cls(options))
            return created[-1]

        def send(self, text):
            self.sent.append(text)
            self.runs.append(FakeRun(self, text))
            return self.runs[-1]

        def call(self, tool, args):  # what Cursor's agent does when it uses a tool
            return self.options.local.custom_tools[tool].execute(args, NS(tool_call_id="t1"))

        def close(self):
            self.closed = True

    return NS(Agent=FakeAgent, AgentOptions=lambda **kw: NS(**kw), LocalAgentOptions=lambda **kw: NS(**kw),
              CustomTool=lambda **kw: NS(**kw), created=created)


TOOLS = [{"name": "list_pods", "description": "List pods", "input_schema": {"type": "object"}}]


def provider(monkeypatch, sdk, timeout=None):
    monkeypatch.setitem(sys.modules, "cursor_sdk", sdk)
    limits = AGENT.with_overrides(timeout=timeout)
    return CursorProvider("cursor", "Cursor", "composer-2.5", limits, api_key="cur-key")


def test_cursor_agent_gets_only_the_tools_it_is_given(monkeypatch):
    sdk = fake_sdk()
    with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda name, args: "x") as session:
        session.send("hello")
    (a,) = sdk.created
    o = a.options
    # Cursor offers custom tools through its "mcp" group: that group only, and never the shell.
    # (PQ passes no tools: then tools=[] and the model can only answer in text, see below.)
    assert o.tools == ["mcp"] and o.disallowed_tools == ["shell"]
    assert not hasattr(o, "mcp_servers")  # no other MCP server is configured
    assert o.model == "composer-2.5" and o.api_key == "cur-key"
    assert o.local.cwd == WORKDIR and o.local.setting_sources == []
    assert set(o.local.custom_tools) == {"list_pods"}
    assert o.local.custom_tools["list_pods"].input_schema == {"type": "object"}
    assert a.closed


def test_system_prompt_goes_with_the_first_message_only(monkeypatch):
    sdk = fake_sdk()
    with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda name, args: "x") as session:
        session.send("first")
        session.send("second")
    first, second = sdk.created[0].sent
    assert first.startswith("SYS") and first.endswith("first") and second == "second"
    assert "MCP tools" in first and "no shell, no files and no web" in first  # tells the agent where its tools are


def test_tool_calls_reach_the_given_tools(monkeypatch):
    calls = []
    sdk = fake_sdk(script=lambda a, text: a.call("list_pods", {"namespace": "apps"}))
    with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda n, args: calls.append((n, args)) or "pods") as s:
        assert s.send("which pods?") == "pods"
        assert s.tool_calls == 1
    assert calls == [("list_pods", {"namespace": "apps"})]


def test_failed_run_is_reported(monkeypatch):
    sdk = fake_sdk(status="error")
    with pytest.raises(ProviderError, match="status 'error' boom"):
        with provider(monkeypatch, sdk).session("SYS", TOOLS, lambda n, a: "x") as session:
            session.send("q")


def test_slow_run_is_cancelled(monkeypatch):
    sdk = fake_sdk(delay=1.0)
    with pytest.raises(ProviderError, match="longer than"):
        with provider(monkeypatch, sdk, timeout=0.1).session("SYS", TOOLS, lambda n, a: "x") as session:
            session.send("q")
    assert sdk.created[0].runs[0].cancelled and sdk.created[0].closed


# --- Through agent.py: a question answered by an agent provider ---

def use_cursor(monkeypatch, sdk):
    monkeypatch.setattr(agent, "llm", provider(monkeypatch, sdk))


def test_pq_gives_cursor_no_tools_at_all(monkeypatch):
    sdk = fake_sdk(script=lambda a, text: "**DNS turns names into addresses.**")
    use_cursor(monkeypatch, sdk)
    assert agent.chat([], "what is DNS?") == "**DNS turns names into addresses.**"
    (a,) = sdk.created
    assert a.options.tools == [] and a.options.disallowed_tools == ["shell"] and a.options.local.custom_tools == {}
    assert len(a.sent) == 1 and a.sent[0].startswith(agent.SYSTEM) and a.sent[0].endswith("what is DNS?")
    assert "You have no tools here" in a.sent[0] and "MCP tools" not in a.sent[0]


def test_chat_through_cursor_keeps_the_conversation(monkeypatch):
    sdk = fake_sdk(script=lambda a, text: f"answer {len(a.sent)}")
    use_cursor(monkeypatch, sdk)
    history = []
    assert agent.chat(history, "what is DNS?") == "answer 1"
    assert agent.chat(history, "go deeper") == "answer 1"  # each question is a new Cursor agent
    second_prompt = sdk.created[1].sent[0]
    assert "Conversation so far:" in second_prompt and "User: what is DNS?" in second_prompt
    assert "PQ: answer 1" in second_prompt and second_prompt.endswith("New question: go deeper")
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_empty_cursor_answer_is_not_kept(monkeypatch):
    sdk = fake_sdk(script=lambda a, text: "")
    use_cursor(monkeypatch, sdk)
    history = []
    assert agent.chat(history, "what is DNS?") == agent.NO_ANSWER and history == []

"""Provider base classes. There are two shapes:

ChatProvider   we ask the provider for one reply at a time
               (Claude and all the OpenAI-compatible APIs)
AgentProvider  the provider runs its own agent loop (Cursor)

Both get the same system prompt and conversation, so agent.py can treat them more or less
the same. Both can take tool definitions too; PQ passes none, it answers from the model's
knowledge.
"""
import logging
from dataclasses import dataclass, replace
from types import SimpleNamespace

logger = logging.getLogger("pq.llm")


class ProviderError(RuntimeError):
    """Bad provider config, or the provider's API returned an error."""


@dataclass(frozen=True)
class Limits:
    steps: int           # model calls per answer when tools are used (PQ makes one call per answer)
    tool_chars: int      # max chars of one tool result (unused while PQ has no tools)
    request_chars: int   # max request size, rules + conversation (0 = no limit); agent.py cuts the history to it
    output_tokens: int   # max reply length
    timeout: float       # seconds per model call (agent providers: per run)

    def with_overrides(self, **values) -> "Limits":
        return replace(self, **{k: v for k, v in values.items() if v})


def response(stop_reason: str, content: list) -> SimpleNamespace:
    """A model reply in our own shape: .stop_reason is "tool_use", "end_turn", "max_tokens"
    or "refusal", .content is a list of blocks with .type "text" or "tool_use"."""
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class Provider:
    kind = ""

    def __init__(self, name: str, title: str, model: str, limits: Limits):
        self.name, self.title, self.model, self.limits = name, title, model, limits

    def describe(self) -> dict:
        return {"provider": self.name, "title": self.title, "model": self.model, "kind": self.kind}


class ChatProvider(Provider):
    kind = "chat"

    def create(self, system: str, messages: list, tools: list, require_tool: str | None = None):
        """One model call. messages are in the Anthropic Messages shape (that's how agent.py
        keeps the history). require_tool makes the model call that tool now, where the API
        supports it. tools may be empty. Returns response(...)."""
        raise NotImplementedError


    def stream(self, system: str, messages: list, tools: list):
        """The same call as create(), piece by piece: yields the text as it is written (str)
        and, as the last item, the complete response(...).

        A provider that can stream implements _stream(). If that fails before the first
        piece of text (the API or an SDK version doesn't support it, a rate limit, ...) the
        answer is fetched with create() instead, which has the retries. After the first
        piece an error is raised: the person has already seen half an answer."""
        started = False
        try:
            for item in self._stream(system, messages, tools):
                started = started or isinstance(item, str)
                yield item
            return
        except NotImplementedError:
            pass
        except Exception as e:
            if started:
                raise
            logger.info("%s: streaming not available (%s), answering in one piece", self.title, type(e).__name__)
        resp = self.create(system, messages, tools)
        text = "".join(getattr(b, "text", "") or "" for b in resp.content or [] if getattr(b, "type", "") == "text")
        if text:
            yield text
        yield resp

    def _stream(self, system: str, messages: list, tools: list):
        """Yield text pieces (str) and then the complete response(...). Optional."""
        raise NotImplementedError


class AgentProvider(Provider):
    kind = "agent"

    def session(self, system: str, tools: list, execute):
        """Context manager for one conversation with the provider's agent, with
        .send(text) -> answer text. The agent calls execute(tool_name, args) -> str for every
        tool it uses and has no other tools. With tools=[] it has none and answers in text."""
        raise NotImplementedError

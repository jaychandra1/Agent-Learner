"""Test setup: a fake environment and a stand-in AI provider.

No AI model API or Teams webhook is contacted by any test.
"""
import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

# Environment must be set before the agent modules are imported.
# Tests start without an AI provider, whatever the shell has set.
PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "CURSOR_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                 "AZURE_OPENAI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "OLLAMA_API_KEY")
for _key in [k for k in os.environ if k.startswith("LLM_")] + list(PROVIDER_KEYS):
    os.environ.pop(_key, None)
for _key in ("BOUNDARIES_ENABLED", "PROTECTED_NAMESPACES", "ANSWER_MAX_WORDS", "AGENT_API_KEY", "TEAMS_TENANT_ID",
             "TEAMS_ALLOWED_USER_IDS"):
    os.environ.pop(_key, None)
TEST_SECRET = base64.b64encode(b"test-outgoing-webhook-secret").decode()
os.environ["TEAMS_OUTGOING_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["TEAMS_REPLY_WEBHOOK_URL"] = "https://example.invalid/reply"
os.environ.pop("PORT", None)
os.environ.pop("WEB_ACCESS_CODE", None)
os.environ.pop("WEB_RATE_PER_MINUTE", None)
os.environ["WEB_UI"] = "https://ui.example.invalid"
os.environ["TEAMS_SECURITY_WEBHOOK_URLS"] = "admins=https://example.invalid/security"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# A structured tool input, for the provider tests: the providers still carry tool calls
# even though PQ itself passes no tools.
SAMPLE_RCA = {
    "title": "Application Crash and Restart Loop in web",
    "severity": "SEV2",
    "summary": "web keeps restarting",
    "timeline": [{"time": "now", "event": "crash"}],
    "resolution": ["Fix the config", "Roll back"],
    "confidence": "high",
}


def fake_llm(kind="chat", steps=14, request_chars=60000, **extra):
    """Stand-in for agent.llm: an AI provider of the given shape (see providers/base.py)."""
    from providers import Limits
    fake = NS(kind=kind, name="fake", title="Fake model", model="fake-1",
              limits=Limits(steps=steps, tool_chars=15000, request_chars=request_chars, output_tokens=4000,
                            timeout=10),
              **extra)
    fake.describe = lambda: {"provider": fake.name, "title": fake.title, "model": fake.model, "kind": fake.kind}
    return fake

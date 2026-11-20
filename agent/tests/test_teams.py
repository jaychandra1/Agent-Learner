import base64
import hashlib
import hmac
import threading
import time
import uuid

import pytest

import agent
import teams
from conftest import TEST_SECRET, fake_llm


def sign(body: bytes) -> str:
    digest = hmac.new(base64.b64decode(TEST_SECRET), body, hashlib.sha256).digest()
    return "HMAC " + base64.b64encode(digest).decode()


@pytest.fixture
def posted(monkeypatch):
    calls = []
    monkeypatch.setattr(teams, "post_card", lambda url, card, thread=None: calls.append((url, card, thread)) or [])
    return calls


def activity(text, msg_id=None, tenant="t1", aad="u1"):
    return {"type": "message", "id": msg_id or str(uuid.uuid4()), "text": text,
            "from": {"name": "Asha", "aadObjectId": aad},
            "conversation": {"id": "19:abc@thread.tacv2;messageid=1731916515731"},
            "channelData": {"tenant": {"id": tenant}, "channel": {"id": "19:abc@thread.tacv2"}}}


THREAD = {"threadMessageId": "1731916515731", "channelId": "19:abc@thread.tacv2"}


def wait_for(predicate, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_hmac_valid_tampered_and_missing():
    body = b'{"text": "hi"}'
    assert teams.verify_hmac(body, sign(body))
    assert not teams.verify_hmac(body + b" ", sign(body))
    assert not teams.verify_hmac(body, None)
    assert not teams.verify_hmac(body, "Bearer abc")


def test_strip_mentions():
    assert teams.strip_mentions("<at>PQ</at>&nbsp;what is&nbsp; DNS?<br>") == "what is DNS?"
    assert teams.strip_mentions("Hi <at>PQ</at>, can you explain OAuth?") == "Hi, can you explain OAuth?"
    assert teams.strip_mentions("") == ""


def test_pasted_code_keeps_its_lines():
    html = "<p><at>PQ</at> why does this fail?</p><p>for i in range(3):<br>&nbsp;&nbsp;print(i)</p>"
    assert teams.strip_mentions(html) == "why does this fail?\nfor i in range(3):\nprint(i)"


@pytest.mark.parametrize("text", ["<at>PQ</at> help", "<at>PQ</at>", "<at>PQ</at> Hello"])
def test_help(text):
    reply = teams.handle_outgoing(activity(text))["text"]
    assert "technical teacher" in reply and "real-time scenario" in reply and "continue" in reply


def test_other_tenant_is_rejected(monkeypatch):
    monkeypatch.setattr(teams, "TENANT_ID", "t1")
    assert "own organization" in teams.handle_outgoing(activity("help", tenant="evil"))["text"]


def test_allowed_users(monkeypatch):
    monkeypatch.setattr(teams, "ALLOWED_USER_IDS", {"u1"})
    assert "technical teacher" in teams.handle_outgoing(activity("help", aad="u1"))["text"]
    assert "allowed-users" in teams.handle_outgoing(activity("help", aad="u2"))["text"]


def test_retried_message_is_answered_once():
    first = teams.handle_outgoing(activity("help", msg_id="dup-1"))
    second = teams.handle_outgoing(activity("help", msg_id="dup-1"))
    assert "technical teacher" in first["text"] and second["text"] == "(already answered)"


def test_question_without_an_ai_model():
    assert teams.handle_outgoing(activity("<at>PQ</at> what is DNS?"))["text"] == agent.NO_MODEL


def test_question_without_a_reply_webhook(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(teams, "REPLY_TARGET", None)
    reply = teams.handle_outgoing(activity("<at>PQ</at> what is DNS?"))["text"]
    assert "no reply webhook" in reply and "TEAMS_" not in reply


def test_question_is_answered_later_through_reply_webhook(monkeypatch, posted):
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "chat", lambda msgs, q: f"answer to: {q}")
    reply = teams.handle_outgoing(activity("<at>PQ</at> what is DNS?"))
    assert reply["text"].startswith("Working on it")
    assert wait_for(lambda: posted)
    target, card, thread = posted[0]
    assert target == ("reply-1", "https://example.invalid/reply")
    assert "answer to: what is DNS?" in str(card)
    assert thread == THREAD  # the answer says which thread it belongs to


def test_long_explanation_arrives_as_several_cards_in_the_thread(monkeypatch, posted):
    lesson = "\n\n".join(f"**Part {i}.** " + "word " * 80 for i in range(60))
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "chat", lambda msgs, q: lesson)
    teams.handle_outgoing(activity("<at>PQ</at> teach me Kubernetes"))
    assert wait_for(lambda: len(posted) >= 3)
    assert all(thread == THREAD for _, _, thread in posted)
    assert "part 1 of" in str(posted[0][1]) and "part 2 of" in str(posted[1][1])


def test_follow_up_in_the_same_thread_shares_the_conversation(monkeypatch, posted):
    seen = []
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "chat", lambda msgs, q: seen.append((id(msgs), q)) or "ok")
    teams.handle_outgoing(activity("<at>PQ</at> what is DNS?"))
    assert wait_for(lambda: len(posted) == 1)
    teams.handle_outgoing(activity("<at>PQ</at> go deeper"))
    other = dict(activity("<at>PQ</at> what is TLS?"), conversation={"id": "19:abc@thread.tacv2;messageid=999"})
    teams.handle_outgoing(other)
    assert wait_for(lambda: len(posted) == 3)
    by_question = {q: history for history, q in seen}
    assert by_question["what is DNS?"] == by_question["go deeper"] != by_question["what is TLS?"]


def test_failure_message_is_generic(monkeypatch, posted):
    monkeypatch.setattr(agent, "llm", fake_llm())

    def fail(msgs, q):
        raise RuntimeError("Cursor run failed: HTTPSConnectionPool(host='api2.cursor.sh') key cur-123")
    monkeypatch.setattr(agent, "chat", fail)
    teams.handle_outgoing(activity("<at>PQ</at> what is DNS?"))
    assert wait_for(lambda: posted)
    shown = str(posted[0][1])
    assert "Sorry, that failed (RuntimeError)" in shown and "Cursor" not in shown and "cur-123" not in shown


def test_thread_is_found_for_replies_and_odd_payloads():
    reply_in_thread = {"id": "1731916599999", "replyToId": "1731916515731",
                       "conversation": {"id": "19:abc@thread.tacv2;messageid=1731916515731"}}
    assert teams.thread_of(reply_in_thread) == THREAD  # the thread's first message, not the reply
    assert teams.thread_of({"id": "42", "conversation": {"id": "19:abc@thread.tacv2"}}) == {
        "threadMessageId": "42", "channelId": "19:abc@thread.tacv2"}
    assert teams.thread_of({}) == {}


def test_thread_is_sent_to_the_workflow(monkeypatch):
    sent = []
    monkeypatch.setattr(teams.requests, "post",
                        lambda url, json=None, timeout=None: sent.append(json) or FakeResponse(202))
    teams.post_card(("reply-1", "https://a"), dict(CARD), THREAD)
    teams.post_card(("admins", "https://a"), dict(CARD))
    assert sent[0]["threadMessageId"] == "1731916515731" and sent[0]["channelId"] == "19:abc@thread.tacv2"
    assert sent[0]["type"] == "message" and sent[0]["attachments"][0]["content"]["type"] == "AdaptiveCard"
    assert "threadMessageId" not in sent[1]  # notices for the administrators aren't answers: no thread


# --- Bounded memory and a busy reply ---

def test_sessions_are_bounded(monkeypatch):
    monkeypatch.setattr(teams, "MAX_SESSIONS", 3)
    teams._sessions.clear()
    for i in range(5):
        teams.session_for(f"c{i}")
    assert list(teams._sessions) == ["c2", "c3", "c4"]
    teams._sessions["c2"]["used"] -= teams.SESSION_IDLE_SECONDS + 1
    teams.session_for("c5")
    assert "c2" not in teams._sessions


def test_too_many_waiting_questions_get_a_busy_reply(monkeypatch):
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(teams, "_slots", threading.BoundedSemaphore(1))
    release = threading.Event()
    monkeypatch.setattr(teams, "answer_in_session", lambda conv, q: release.wait(5) and "done")
    monkeypatch.setattr(teams, "post_card", lambda *a: [{"ok": True}])
    body = {"type": "message", "text": "<at>PQ</at> why?", "conversation": {"id": "c"}}
    assert teams.handle_outgoing({**body, "id": "busy-1"})["text"].startswith("Working on it")
    assert "busy" in teams.handle_outgoing({**body, "id": "busy-2"})["text"]
    release.set()


# --- Posting to Workflows webhooks ---

SECRET_URL = "https://example.invalid/workflows/abc/invoke?sig=TOP-SECRET-SIGNATURE"
CARD = {"type": "AdaptiveCard", "body": []}


class FakeResponse:
    def __init__(self, status, headers=None, text=""):
        self.status_code, self.headers, self.text = status, headers or {}, text


def scripted_post(monkeypatch, outcomes):
    """Make requests.post return/raise the given outcomes in order; record calls; no real sleeping."""
    calls = []

    def fake_post(url, json, timeout):
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(teams.requests, "post", fake_post)
    monkeypatch.setattr(teams.time, "sleep", lambda s: None)
    return calls


def test_parse_targets_with_and_without_names():
    assert teams.parse_targets("chat=https://a/x?sig=1, https://b/y?sig=2\nadmins-private=https://c/z", "security") == [
        ("chat", "https://a/x?sig=1"), ("security-2", "https://b/y?sig=2"), ("admins-private", "https://c/z")]
    assert teams.parse_targets("", "security") == []


def test_posts_to_every_target_including_private_channels(monkeypatch):
    calls = scripted_post(monkeypatch, [FakeResponse(202), FakeResponse(202)])
    results = teams.post_card([("chat", "https://a"), ("admins-private", "https://b")], CARD)
    assert calls == ["https://a", "https://b"]
    assert [r["target"] for r in results] == ["chat", "admins-private"] and all(r["ok"] for r in results)


def test_throttled_post_is_retried(monkeypatch):
    calls = scripted_post(monkeypatch, [FakeResponse(429, {"Retry-After": "1"}), FakeResponse(202)])
    assert teams.post_card(("chat", "https://a"), CARD) == [{"target": "chat", "status": 202, "ok": True}]
    assert len(calls) == 2


def test_bad_request_is_not_retried(monkeypatch):
    calls = scripted_post(monkeypatch, [FakeResponse(400, text="bad card")])
    assert teams.post_card(("chat", "https://a"), CARD) == [{"target": "chat", "status": 400, "ok": False}]
    assert len(calls) == 1


def test_post_card_never_raises_and_never_logs_the_url(monkeypatch, caplog):
    scripted_post(monkeypatch, [ConnectionError(f"Max retries exceeded with url: {SECRET_URL}")] * 3)
    results = teams.post_card([("admins-private", SECRET_URL)], CARD)
    assert results == [{"target": "admins-private", "status": None, "ok": False, "error": "ConnectionError"}]
    assert "TOP-SECRET-SIGNATURE" not in caplog.text and "admins-private" in caplog.text

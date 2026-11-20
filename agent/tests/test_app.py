import json

import pytest
from fastapi.testclient import TestClient

import agent
import app as app_module
import main
import teams
from conftest import fake_llm
from test_teams import sign


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "pq"}
    assert client.get("/ready").json()["status"] == "ready"


def test_teams_webhook_requires_valid_signature(client):
    body = json.dumps({"type": "message", "id": "app-1", "text": "<at>PQ</at> help"}).encode()
    assert client.post("/teams/webhook", content=body).status_code == 401
    assert client.post("/teams/webhook", content=body, headers={"Authorization": "HMAC bad"}).status_code == 401
    r = client.post("/teams/webhook", content=body, headers={"Authorization": sign(body)})
    assert r.status_code == 200 and r.json()["type"] == "message" and "technical teacher" in r.json()["text"]


def test_teams_webhook_disabled_without_secret(client, monkeypatch):
    monkeypatch.setattr(teams, "OUTGOING_SECRET", "")
    assert client.post("/teams/webhook", content=b"{}").status_code == 404


def test_teams_test_posts_sample_card(client, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "API_KEY", "k")
    monkeypatch.setattr(teams, "post_card", lambda target, card: sent.append((target, card)) or [{"ok": True}])
    r = client.post("/teams/test", headers={"x-api-key": "k"})
    assert r.status_code == 200 and r.json()["sent_to"] == 1
    assert sent[0][0] == ("reply-1", "https://example.invalid/reply")


def test_status_and_api_key(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "k")
    assert client.get("/status").status_code == 401
    status = client.get("/status", headers={"x-api-key": "k"}).json()
    assert status["teams"] == {"reply_webhook": "reply-1", "security_targets": ["admins"], "outgoing_webhook": True}
    assert status["llm"]["configured"] is False and "LLM_PROVIDER is not set" in status["llm"]["status"]
    assert status["web_ui"] == {"enabled": True, "origins": ["https://ui.example.invalid"], "access_code": False}
    assert status["boundaries"] == {"enabled": True, "protected_namespaces": ["pq"]}


def test_chat_keeps_a_session(client, monkeypatch):
    seen = []
    monkeypatch.setattr(app_module, "API_KEY", "k")
    monkeypatch.setattr(agent, "llm", fake_llm())
    monkeypatch.setattr(agent, "chat", lambda msgs, q: seen.append(id(msgs)) or f"answer to {q}")
    first = client.post("/chat", json={"message": "what is DNS?"}, headers={"x-api-key": "k"}).json()
    assert first["answer"] == "answer to what is DNS?" and first["session_id"]
    client.post("/chat", json={"message": "go deeper", "session_id": first["session_id"]}, headers={"x-api-key": "k"})
    assert seen[0] == seen[1]  # the same conversation


def test_internal_api_is_closed_without_a_key(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "")
    for method, path in [("get", "/status"), ("post", "/teams/test"), ("get", "/security/restricted")]:
        r = getattr(client, method)(path)
        assert r.status_code == 403 and "AGENT_API_KEY" in r.text, path
    assert client.post("/chat", json={"message": "hi"}).status_code == 403


def test_healthz_fails_when_the_teams_server_died(client, monkeypatch):
    assert client.get("/healthz").status_code == 200
    monkeypatch.setitem(app_module.SERVER_FAILURES, "teams", "SystemExit: 1")
    r = client.get("/healthz")
    assert r.status_code == 503 and "teams server" in r.text


def test_a_server_that_stops_is_recorded(monkeypatch):
    failures = {}

    class Boom:
        def __init__(self, config):
            pass

        def run(self):
            raise SystemExit(1)  # what uvicorn does when the port can't be bound
    monkeypatch.setattr(main.uvicorn, "Server", Boom)
    thread = main.supervised("teams", None, lambda name, why: failures.update({name: why}))
    thread.start()
    thread.join(5)
    assert failures == {"teams": "SystemExit: 1"}


def test_two_servers_on_their_ports():
    api, public = main.build_configs()
    assert (api.port, public.port) == (8000, 8080) and public.app is app_module.teams_app


def test_public_port_serves_only_the_teams_webhook():
    """Port 8080 is what the Cloudflare Tunnel reaches: nothing but POST /teams/webhook."""
    with TestClient(app_module.teams_app) as public:
        body = json.dumps({"type": "message", "id": "pub-1", "text": "<at>PQ</at> help"}).encode()
        assert public.post("/teams/webhook", content=body).status_code == 401
        assert public.post("/teams/webhook", content=body, headers={"Authorization": sign(body)}).status_code == 200
        for path in ("/healthz", "/status", "/security/restricted", "/docs", "/openapi.json"):
            assert public.get(path).status_code == 404, path
        assert public.post("/chat", json={"message": "hi"}).status_code == 404
        assert public.post("/teams/test").status_code == 404
        assert public.get("/api/health").status_code == 200  # the web UI's API, see test_web.py
        assert public.get("/teams/webhook").status_code == 405

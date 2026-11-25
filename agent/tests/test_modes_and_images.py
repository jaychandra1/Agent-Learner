"""Interview mode, and questions that come with pictures or files."""
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
from providers import response
from providers.openai_compat import to_openai_messages
from test_provider_cursor import fake_sdk, provider

UI = "https://ui.example.invalid"
WEBUI_DIR = Path(__file__).resolve().parents[2] / "webui"
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def text(t):
    return NS(type="text", text=t)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(teams, "post_card", lambda *a, **k: [])
    web._calls.clear()
    boundaries._events.clear()
    yield
    web._calls.clear()
    boundaries._events.clear()


@pytest.fixture
def client():
    with TestClient(app_module.public_app) as c:
        yield c


@pytest.fixture
def model(monkeypatch):
    """A chat model that records each call. Returns the list of calls."""
    calls = []

    def create(system, messages, tools, **kw):
        calls.append(NS(system=system, messages=[dict(m) for m in messages]))
        return response("end_turn", [text("ok")])
    monkeypatch.setattr(agent, "llm", fake_llm(create=create))
    return calls


def user(content="", images=None):
    return {"role": "user", "content": content, **({"images": images} if images else {})}


def ask(client, messages, path="/api/chat", **body):
    return client.post(path, json={"messages": messages, **body}, headers={"Origin": UI})


# --- Interview mode ---

def test_interview_rule_is_added_only_in_that_mode():
    chat, interview = agent._system(False), agent._system(False, "interview")
    assert chat == agent.SYSTEM and interview == agent.SYSTEM + agent.INTERVIEW_RULE
    assert agent._system(True, "interview") == agent.SYSTEM_RESTRICTED + agent.INTERVIEW_RULE  # boundaries stay
    for must in ("Interview mode is on", "Definition; Why it exists; How it works", "technical deep dive: Situation",
                 "Requirements clarification", "Capacity estimation", "Behavioral or managerial",
                 "12+ years: strategy", "answer at the 5-8 year level", "**What the interviewer is listening for:**",
                 "**How a senior engineer lifts it:**", "**Likely follow-ups:**", "own project and numbers in",
                 "set up their interview profile", "(attached with the + button, or pasted)",
                 "build the real-world examples from their own", "Kubernetes/OpenShift, or all of these",
                 "never repeat phone numbers, email addresses or home addresses"):
        assert must in agent.INTERVIEW_RULE, must
    assert "Mock interview mode" not in interview


def test_mock_rule_makes_pq_the_interviewer():
    mock = agent._system(False, "mock")
    assert mock == agent.SYSTEM + agent.MOCK_RULE and agent.INTERVIEW_RULE not in mock
    assert agent._system(True, "mock") == agent.SYSTEM_RESTRICTED + agent.MOCK_RULE  # boundaries stay
    rule = agent.MOCK_RULE
    for must in ("You are the interviewer", "their resume", "Kubernetes/OpenShift", "Ask only once",
                 "Ask ONE question at a time", "stop and wait", "Never answer\n   your own question",
                 "accuracy 40%, depth of knowledge 20%, real-world experience 20%",
                 "Never expect a 3-year engineer to answer like a 10-year one",
                 "**Score: X/10**", "**What went well:**", "**What was missing:**", "**Red flags:**",
                 "**What the interviewer expected:**", "**Best possible answer:**", "**Senior-level touch:**",
                 '"hint"', '"skip"', '"repeat"', '"end"', "readiness verdict",
                 "Never repeat\n   phone numbers, email addresses or home addresses"):
        assert must in rule, must


def test_mode_from_the_ui_reaches_the_model(client, model):
    assert ask(client, [user("what is a mutex?")]).status_code == 200
    assert ask(client, [user("what is a mutex?")], mode="interview").status_code == 200
    assert ask(client, [user("what is a mutex?")], path="/api/stream", mode="interview").status_code == 200
    assert ask(client, [user("Start the mock interview.")], path="/api/stream", mode="mock").status_code == 200
    assert [agent.INTERVIEW_RULE in c.system for c in model] == [False, True, True, False]
    assert [agent.MOCK_RULE in c.system for c in model] == [False, False, False, True]
    assert all(c.system.startswith(agent.SYSTEM_RESTRICTED) for c in model)


def test_unknown_mode_is_rejected(client, model):
    assert ask(client, [user("q")], mode="pirate").status_code == 422 and model == []


def test_teams_and_the_cli_stay_in_chat_mode(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "llm", fake_llm(create=lambda system, messages, tools: seen.append(system)
                                               or response("end_turn", [text("ok")])))
    agent.chat([], "what is a mutex?")
    assert seen == [agent.SYSTEM]


# --- Pictures ---

def test_picture_goes_to_the_model_as_an_image_block(client, model):
    assert ask(client, [user("what is wrong in this diagram?", [PNG])]).status_code == 200
    (sent,) = model[0].messages
    assert sent["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUg=="}},
        {"type": "text", "text": "what is wrong in this diagram?"}]


def test_picture_without_words_gets_a_question(client, model):
    ask(client, [user("", [JPEG])])
    assert model[0].messages[0]["content"][-1] == {"type": "text", "text": web.IMAGE_ONLY}


def test_only_the_newest_pictures_are_sent(client, model):
    ask(client, [user("first", [PNG, PNG, PNG]), {"role": "assistant", "content": "a"}, user("second", [JPEG, JPEG])])
    first, _, second = model[0].messages
    assert [b["type"] for b in second["content"]] == ["image", "image", "text"]
    assert [b["type"] for b in first["content"]] == ["image", "image", "text"]  # 4 in total
    assert "1 image(s) sent earlier" in first["content"][-1]["text"]


@pytest.mark.parametrize("bad,status", [
    ("data:image/svg+xml;base64,PHN2Zz4=", 422),
    ("https://example.com/cat.png", 422),
    ("data:image/png;base64,not base64!", 422),
    ("data:image/png;base64," + "A" * web.MAX_IMAGE_CHARS, 413),
])
def test_bad_pictures_are_rejected(client, model, bad, status):
    assert ask(client, [user("q", [bad])]).status_code == status and model == []


def test_pictures_from_an_assistant_turn_are_ignored(client, model):
    ask(client, [user("q"), {"role": "assistant", "content": "a", "images": [PNG]}, user("again")])
    assert all(isinstance(m["content"], str) for m in model[0].messages)


def test_question_filter_reads_the_words_next_to_a_picture(client, model):
    r = ask(client, [user("which model are you using?", [PNG])])
    assert r.json() == {"answer": boundaries.REFUSAL} and model == []
    assert boundaries.recent()[0]["question"] == "which model are you using?"  # no image data in the record


def test_picture_question_streams_too(client, model):
    r = ask(client, [user("explain", [PNG])], path="/api/stream")
    events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    assert events[-1] == {"done": True, "answer": "ok"} and model[0].messages[0]["content"][0]["type"] == "image"


def test_openai_compatible_providers_get_pictures_as_image_url_parts():
    out = to_openai_messages("SYS", [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "text", "text": "what is this?"}]}])
    assert out[1] == {"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}


def test_history_with_pictures_is_trimmed_by_its_text():
    blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 50000}},
              {"type": "text", "text": "what is this?"}]
    history = [{"role": "user", "content": blocks}, {"role": "assistant", "content": "a diagram"},
               {"role": "user", "content": "go deeper"}]
    assert agent.trim_history(history, keep=20, max_chars=1000) is history


def test_cursor_cannot_read_pictures_and_says_so(monkeypatch):
    sdk = fake_sdk(script=lambda a, message: "answer")
    monkeypatch.setattr(agent, "llm", provider(monkeypatch, sdk))
    question = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {"type": "text", "text": "what is this?"}]
    history = []
    assert agent.chat(history, question) == agent.NO_IMAGES and history == [] and sdk.created == []
    assert list(agent.chat_stream(history, question)) == [agent.NO_IMAGES]
    assert agent.chat(history, [{"type": "text", "text": "what is DNS?"}]) == "answer"  # text alone is fine
    assert sdk.created[0].sent[0].endswith("what is DNS?")


# --- The UI ---

def test_ui_has_the_plus_button_the_mode_setting_and_a_simple_greeting():
    page = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (WEBUI_DIR / "app.js").read_text(encoding="utf-8")
    assert "Hey, need any help?" in page and "What do you want to learn?" not in page and 'id="suggestions"' not in page
    assert 'id="attach"' in page and 'id="attachFile"' in page and "image/png" in page
    assert '<select id="mode">' in page and 'value="interview"' in page and 'value="chat"' in page
    assert 'value="mock"' in page and 'id="startMock"' in page and 'id="autoListen"' in page and ".pdf" in page
    assert "mode: requestMode" in app_js and "toDataURL" in app_js and "SUGGESTIONS" not in app_js
    assert '"/api/extract"' in app_js and "startListening()" in app_js and "mode: settings.mode }" in app_js
    assert "Set up my interview profile." in app_js and "Share my resume & details" in app_js
    assert "Start the mock interview." in app_js


# --- Reading a resume: PDF and Word ---

def _pdf(text):
    """A small, valid PDF with one page of text."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
               b"/Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def _docx(paragraphs):
    import io
    import zipfile
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="w"><w:body>' + body
                   + "</w:body></w:document>")
    return buf.getvalue()


def data_url(raw, mime):
    import base64
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def extract(client, name, data, **headers):
    return client.post("/api/extract", json={"name": name, "data": data}, headers={"Origin": UI, **headers})


def test_pdf_resume_is_read(client):
    r = extract(client, "resume.pdf", data_url(_pdf("Jaya - DevOps Engineer - 6 years Kubernetes"), "application/pdf"))
    assert r.status_code == 200 and r.json()["name"] == "resume.pdf"
    assert "DevOps Engineer - 6 years Kubernetes" in r.json()["text"]


def test_word_resume_is_read(client):
    raw = _docx(["Senior SRE", "Skills: Terraform &amp; AWS", "Led migration to EKS"])
    r = extract(client, "cv.docx", data_url(raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    assert r.status_code == 200
    assert r.json()["text"].splitlines() == ["Senior SRE", "Skills: Terraform & AWS", "Led migration to EKS"]


@pytest.mark.parametrize("name,data,status", [
    ("x.pdf", "not a data url", 422),
    ("x.pdf", "data:application/pdf;base64,@@@", 422),
    ("x.txt", data_url(b"plain text", "text/plain"), 422),
    ("x.docx", data_url(b"PK not a zip", "application/zip"), 422),
    ("x.pdf", data_url(b"%PDF-1.4 broken", "application/pdf"), 422),
    ("x.pdf", "data:application/pdf;base64," + "A" * web.MAX_DOCUMENT_CHARS, 413),
])
def test_bad_documents_are_refused(client, name, data, status):
    assert extract(client, name, data).status_code == status


def test_scanned_pdf_without_text_says_so(client):
    r = extract(client, "scan.pdf", data_url(_pdf(""), "application/pdf"))
    assert r.status_code == 422 and "No text was found" in r.json()["detail"]


def test_extract_needs_the_access_code_and_the_ui(client, monkeypatch):
    data = data_url(_pdf("hello"), "application/pdf")
    monkeypatch.setattr(web, "ACCESS_CODE", "open-sesame")
    assert extract(client, "a.pdf", data).status_code == 401
    assert extract(client, "a.pdf", data, **{"X-Access-Code": "open-sesame"}).status_code == 200
    monkeypatch.setattr(web, "ENABLED", False)
    assert extract(client, "a.pdf", data, **{"X-Access-Code": "open-sesame"}).status_code == 404


def test_resume_text_is_not_logged(client, caplog):
    extract(client, "resume.pdf", data_url(_pdf("phone 98765 43210"), "application/pdf"))
    assert "98765" not in caplog.text and "Read a document" in caplog.text


def test_vercel_proxies_the_resume_reader():
    proxy = (WEBUI_DIR / "api" / "extract.js").read_text(encoding="utf-8")
    assert 'backend + "/api/extract"' in proxy and "process.env.BACKEND_URL" in proxy
    assert "api/extract.js" in json.loads((WEBUI_DIR / "vercel.json").read_text(encoding="utf-8"))["functions"]

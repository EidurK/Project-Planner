import pytest
from fastapi.testclient import TestClient

from app import agent, settings, storage
from app.main import app


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.delenv("MODEL", raising=False)
    agent._sessions.clear()


def test_default_and_persist():
    assert settings.get_model() == "claude-sonnet-5-5"
    c = TestClient(app)
    r = c.put("/api/settings", json={"model": "claude-opus-5-5"})
    assert r.status_code == 200 and r.json()["model"] == "claude-opus-5-5"
    assert settings.get_model() == "claude-opus-5-5"
    assert {m["id"] for m in c.get("/api/settings").json()["models"]} >= {"claude-haiku-4-5", "claude-opus-5-5"}


def test_rejects_unknown_model():
    assert TestClient(app).put("/api/settings", json={"model": "gpt-4"}).status_code == 400


def test_env_model_is_shown_even_if_unlisted(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-something-new")
    pub = settings.public()
    assert pub["model"] == "claude-something-new" and any(m["id"] == "claude-something-new" for m in pub["models"])


def test_model_used_per_call_and_thinking_stripped_on_switch(monkeypatch):
    seen = []

    class C:
        messages = None

        def __init__(self):
            self.messages = self

        def stream(self, *, model, **kw):
            seen.append(model)
            raise RuntimeError("stop here")

    monkeypatch.setattr(agent, "_client", C())
    settings.set_model("claude-haiku-4-5")
    with pytest.raises(RuntimeError):
        list(agent.chat("x", "hi"))
    assert seen == ["claude-haiku-4-5"]

    agent._sessions["y"] = [{"role": "assistant", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "a"}]}]
    TestClient(app).put("/api/settings", json={"model": "claude-opus-5-5"})
    assert agent._sessions["y"][0]["content"] == [{"type": "text", "text": "a"}]


def test_api_key_lifecycle(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "verify_key", lambda k: None)
    c = TestClient(app)
    assert c.get("/api/settings").json()["api_key"] == {"configured": False, "source": None, "hint": ""}
    key = "sk-ant-api03-SECRETSECRETSECRET1234"
    r = c.put("/api/settings/api-key", json={"key": f"  {key}\n"})
    assert r.status_code == 200 and r.json()["status"]["source"] == "app"
    assert settings.get_api_key() == key
    # the full key never comes back to the browser
    assert key not in c.get("/api/settings").text and key not in r.text
    assert c.get("/api/settings").json()["api_key"]["hint"] == "sk-ant-…1234"
    assert oct((tmp_path / "secrets.json").stat().st_mode & 0o777) == "0o600"
    assert c.delete("/api/settings/api-key").json()["status"]["configured"] is False
    assert settings.get_api_key() is None


def test_api_key_validation_and_rejection(monkeypatch):
    c = TestClient(app)
    assert c.put("/api/settings/api-key", json={"key": "short"}).status_code == 400
    assert c.put("/api/settings/api-key", json={"key": "has space " * 5}).status_code == 400

    def reject(k):
        raise ValueError("Anthropic rejected this API key.")

    monkeypatch.setattr(settings, "verify_key", reject)
    r = c.put("/api/settings/api-key", json={"key": "sk-ant-" + "x" * 30})
    assert r.status_code == 400 and settings.get_api_key() is None  # rejected keys are not saved


def test_env_key_reported_and_saved_key_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-ENVENVENV9999")
    assert settings.api_key_status()["source"] == "environment"
    monkeypatch.setattr(settings, "verify_key", lambda k: None)
    settings.set_api_key("sk-ant-saved-" + "y" * 20)
    assert settings.api_key_status()["source"] == "app"


def test_client_rebuilt_when_key_changes(monkeypatch):
    built = []
    monkeypatch.setattr(agent.anthropic, "Anthropic", lambda **kw: built.append(kw) or object())
    monkeypatch.setattr(agent, "_client", None)
    monkeypatch.setattr(agent, "_client_key", None)
    monkeypatch.setattr(settings, "verify_key", lambda k: None)
    agent._get_client()
    settings.set_api_key("sk-ant-first-" + "a" * 20)
    agent._get_client()
    agent._get_client()  # unchanged key: reuse
    assert built == [{}, {"api_key": "sk-ant-first-" + "a" * 20}]


def test_friendly_error_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(agent, "_client", None)
    r = TestClient(app).post("/api/chat", json={"session_id": "k", "message": "hi"})
    assert "Add one in the Settings tab" in r.text

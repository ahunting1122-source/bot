import json

import httpx
import pytest

import app.connectors as connector_module
from app.config import Settings
from app.connectors import ConnectorConfig


@pytest.fixture
def config(tmp_path, monkeypatch):
    secrets = {}
    monkeypatch.setattr(
        connector_module.keyring,
        "set_password",
        lambda service, key, value: secrets.update({key: value}),
    )
    monkeypatch.setattr(
        connector_module.keyring, "get_password", lambda service, key: secrets.get(key)
    )
    return ConnectorConfig(Settings(data_dir=tmp_path, workspace=tmp_path / "workspace"))


def test_browser_config_is_separate_and_legacy_bilibili_is_migrated(config):
    config.path.write_text(json.dumps({"notion_url": "https://www.bilibili.com/"}))
    config.load()
    assert config.public()["browser_url"] == "https://www.bilibili.com/"
    assert config.public()["notion_url"] == ""
    stored = json.loads(config.path.read_text())
    assert stored["browser_url"] == "https://www.bilibili.com/"
    assert stored["notion_url"] == ""
    with pytest.raises(ValueError):
        config.update({"notion_url": "https://www.bilibili.com/"})
    with pytest.raises(ValueError):
        config.update({"browser_url": "javascript:alert(1)"})
    with pytest.raises(ValueError):
        config.update({"browser_domains": "removed.example"})


async def test_model_check_verifies_actual_response_and_invalidates_on_change(config, monkeypatch):
    config.update(
        {
            "model": "deepseek-chat",
            "model_base_url": "https://model.example/v1",
            "model_api_key": "test-secret",
        }
    )
    assert config.public()["model_status"] == "configured"
    original_client = httpx.AsyncClient

    def reply(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert json.loads(request.content)["model"] == "deepseek-chat"
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(
        connector_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(reply), **kwargs),
    )
    result = await config.check_model()
    assert result["model_status"] == "verified"
    assert result["model_checked_at"]
    assert result["model_check_error"] is None
    assert "test-secret" not in config.path.read_text()
    assert "test-secret" not in json.dumps(result)
    config.update({"model_api_key": ""})
    assert config.public()["model_status"] == "verified"
    config.update({"model": "different-model"})
    assert config.public()["model_status"] == "configured"


async def test_failed_model_check_never_returns_provider_secret(config, monkeypatch):
    config.update({"model": "test", "model_api_key": "test-secret"})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        connector_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "invalid key test-secret"})
            ),
            **kwargs,
        ),
    )
    result = await config.check_model()
    assert result["model_status"] == "failed"
    assert "401" in result["model_check_error"]
    assert "test-secret" not in json.dumps(result)

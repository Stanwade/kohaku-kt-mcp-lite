import json
import secrets

import pytest

from kt_mcp.desktop import (
    DesktopService,
    DesktopSettings,
    client_configuration,
    load_settings,
    save_settings,
)


def test_settings_and_secrets(tmp_path):
    path = tmp_path / "settings.json"
    settings = DesktopSettings(
        str(tmp_path), mode="named", public_origin="https://mcp.example.com"
    )
    save_settings(settings, path)
    assert load_settings(path) == settings
    assert "token" not in path.read_text()
    path.write_text("broken")
    assert load_settings(path) == DesktopSettings()
    token = secrets.token_urlsafe(32)
    url = "https://mcp.example.com/mcp/" + token
    config = json.loads(client_configuration(url))
    assert config == {"mcpServers": {"kt-mcp": {"url": url}}}
    service = DesktopService()
    service.secrets = (token,)
    service.emit("log", "connector reported " + token)
    assert service.events.get_nowait() == ("log", "connector reported [REDACTED]")
    service.emit("url", url)
    assert service.events.get_nowait() == ("url", url)
    service.emit("error", "request failed at " + url)
    assert service.events.get_nowait() == (
        "error",
        "request failed at https://mcp.example.com/mcp/[REDACTED]",
    )


@pytest.mark.parametrize(
    "kwargs", [{"port": 0}, {"port": 65536}, {"mode": "bad"}, {"mode": "named"}]
)
def test_invalid_settings(tmp_path, kwargs):
    with pytest.raises(ValueError):
        DesktopSettings(str(tmp_path), **kwargs).validate()


def test_token_validation_before_worker(tmp_path):
    service = DesktopService()
    with pytest.raises(ValueError, match="token"):
        service.start(DesktopSettings(str(tmp_path)), "short")
    with pytest.raises(ValueError, match="Tunnel Token"):
        service.start(
            DesktopSettings(
                str(tmp_path), mode="named", public_origin="https://mcp.example.com"
            ),
            "x" * 32,
        )
    assert not service.running

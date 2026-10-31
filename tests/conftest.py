import os

import pytest


@pytest.fixture(autouse=True)
def isolated_environment():
    original = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(original)


@pytest.fixture(autouse=True)
def isolated_launcher_config(tmp_path, monkeypatch):
    # A real owner may change Config.toml while the service is running. Tests
    # must never inherit that path or write received test files into it.
    from wechat_assistant.projects import upload_config

    config = tmp_path / "test-launcher.toml"
    config.write_text('default_upload_path = ""\n')
    monkeypatch.setattr(upload_config, "default_config_path", lambda: config)

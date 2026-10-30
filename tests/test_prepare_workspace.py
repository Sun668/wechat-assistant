"""Setup tests use simulated login and installers, never real accounts."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prepare_workspace", Path(__file__).parents[1] / "scripts" / "prepare_workspace.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


@pytest.fixture
def simulated(monkeypatch, tmp_path):
    settings = setup.settings_for(tmp_path)
    settings.owner_id = "owner"
    monkeypatch.setattr(setup, "settings_for", lambda _: settings)
    monkeypatch.setattr(setup, "resolve_binary", lambda _: "/fake/codex")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "Logged in using ChatGPT", "")

    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr(setup.SharedCodexEngine, "environment", lambda _: {})
    return settings, calls


def test_existing_binding_skips_qr(simulated, monkeypatch):
    settings, calls = simulated
    monkeypatch.setattr(setup, "run_ilink_login", lambda *_: pytest.fail("must retain binding"))
    setup.prepare(settings.workspace)
    assert calls == [
        ("/fake/codex", "--version"), ["/fake/codex", "login", "status"]
    ]


def test_unbound_generates_opens_and_confirms_qr(simulated, monkeypatch):
    settings, calls = simulated
    settings.owner_id = "self"
    phases = []

    def login(_, args):
        phases.append((args.check, args.qr_only))
        if args.check:
            settings.owner_id = "owner"

    monkeypatch.setattr(setup, "run_ilink_login", login)
    setup.prepare(settings.workspace)
    assert phases == [(False, True), (True, False)]
    assert ["open", str(settings.data_dir / "credentials" / "ilink-login-qr.png")] in calls


def test_wrong_login_stops_before_binding(simulated, monkeypatch):
    settings, _ = simulated
    monkeypatch.setattr(setup.subprocess, "run", lambda args, **_: subprocess.CompletedProcess(
        args, 0, "Logged in using API key", ""
    ))
    monkeypatch.setattr(setup, "run_ilink_login", lambda *_: pytest.fail("must not bind"))
    with pytest.raises(ValueError, match="ChatGPT"):
        setup.prepare(settings.workspace)


def test_missing_codex_installs_before_login(simulated, monkeypatch):
    settings, calls = simulated
    (settings.data_dir / "tmp").mkdir()
    resolutions = iter([None, "/fake/codex"])

    def resolve(_):
        result = next(resolutions)
        if result is None:
            raise setup.EngineUnavailable("missing")
        return result

    monkeypatch.setattr(setup, "resolve_binary", resolve)
    setup.prepare(settings.workspace)
    assert calls[0][0] == "curl"
    assert "https://chatgpt.com/codex/install.sh" in calls[0]
    assert calls[1][0] == "sh"


def test_setup_lock_prevents_second_login(simulated):
    settings, calls = simulated
    with (settings.data_dir / "setup.lock").open("a+") as lock:
        setup.fcntl.flock(lock, setup.fcntl.LOCK_EX | setup.fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="已有环境准备"):
            setup.prepare(settings.workspace)
    assert calls == []

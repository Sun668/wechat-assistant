import fcntl
import json
import os
import plistlib
from types import SimpleNamespace

import pytest

from wechat_assistant.config import Settings
from wechat_assistant.projects import service as service_module
from wechat_assistant.projects.service import AppBackgroundService, MacService


@pytest.fixture
def service(tmp_path, monkeypatch):
    settings = Settings(
        workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant", owner_id="owner"
    )
    svc = MacService(settings)
    svc.calls = []
    svc.registered = False

    def launchctl(*args):
        svc.calls.append(args)
        if args[0] == "print":
            return SimpleNamespace(
                returncode=0 if svc.registered else 113,
                stdout="state = running\n pid = 123\nSECRET_ENV=hidden\n",
            )
        svc.registered = args[0] == "bootstrap"
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(svc, "_launchctl", launchctl)
    return svc


def test_service_definition_has_no_shell_or_secrets_and_all_state_is_scoped(service, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "DO-NOT-COPY")
    definition = service.definition(8765)
    assert definition["KeepAlive"] and definition["RunAtLoad"]
    assert definition["ProgramArguments"][1:4] == ["-B", "-m", "wechat_assistant.projects.cli"]
    assert definition["WorkingDirectory"] == str(service.settings.workspace)
    assert "DO-NOT-COPY" not in str(definition)
    for field in ("StandardOutPath", "StandardErrorPath"):
        assert definition[field].startswith(str(service.settings.data_dir))
    assert service.definition(8766)["ProgramArguments"][-1] == "8766"
    with pytest.raises(ValueError):
        service.definition(0)


def test_start_is_idempotent_and_stop_unregisters_only_this_workspace(service):
    assert service.start()["pid"] == "123"
    assert service.start()["registered"]
    assert len([call for call in service.calls if call[0] == "bootstrap"]) == 1
    with service.plist.open("rb") as stream:
        assert plistlib.load(stream)["Label"] == service.label
    assert service.plist.stat().st_mode & 0o777 == 0o600
    status = service.status()
    assert "SECRET_ENV" not in str(status)
    assert not service.stop()["registered"]
    assert ("bootout", f"gui/{os.getuid()}/{service.label}") in service.calls


def test_service_refuses_to_compete_with_live_workspace(service):
    service.settings.data_dir.mkdir()
    with (service.settings.data_dir / "runtime.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="前台实例"):
            service.start()
    assert not any(call[0] == "bootstrap" for call in service.calls)


def test_service_refuses_symlinked_logs(service, tmp_path):
    service.root.mkdir(parents=True)
    (service.root / "stderr.log").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="符号链接"):
        service.start()
    assert not any(call[0] == "bootstrap" for call in service.calls)


@pytest.mark.parametrize(
    "same_user,same_workspace,alive",
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_background_pid_must_match_user_program_and_exact_workspace(
    service, monkeypatch, same_user, same_workspace, alive
):
    svc = AppBackgroundService(service.settings)
    svc.root.mkdir(parents=True)
    svc.pid_file.write_text(json.dumps({"pid": 12345, "workspace": str(svc.settings.workspace)}))
    command = "python -B -m wechat_assistant.projects.service --port 8765 --workspace " + str(
        svc.settings.workspace
    )
    if not same_workspace:
        command += "-another-workspace"
    uid = os.getuid() if same_user else os.getuid() + 1
    monkeypatch.setattr(
        service_module.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=f"{uid} {command}"),
    )
    assert svc.status()["registered"] is alive
    signals = []
    monkeypatch.setattr(service_module.os, "kill", lambda *args: signals.append(args))
    svc.stop()
    assert bool(signals) is alive


def test_background_start_detaches_with_closed_terminal_and_keeps_arguments_literal(
    service, monkeypatch
):
    svc = AppBackgroundService(service.settings)
    calls = []

    def popen(command, **options):
        calls.append((command, options))
        return SimpleNamespace(pid=45678)

    monkeypatch.setattr(service_module.subprocess, "Popen", popen)
    assert svc.start()["state"] == "starting"
    command, options = calls[0]
    assert command[-2:] == ["--workspace", str(svc.settings.workspace)]
    assert options["start_new_session"] and options["close_fds"]
    assert options["stdin"] == service_module.subprocess.DEVNULL
    assert options["stdout"].name.startswith(str(svc.settings.data_dir))


def test_background_supervisor_restarts_exit_and_forwards_stop_signal(service, monkeypatch):
    svc = AppBackgroundService(service.settings)
    children = []

    class Stop:
        stopped = False

        def set(self):
            self.stopped = True

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            if seconds == 0.25:
                self.stopped = True
            return self.stopped

    class Child:
        def __init__(self, *args, **kwargs):
            self.code = 42 if not children else None
            self.signals = []
            children.append(self)

        def poll(self):
            return self.code

        def send_signal(self, signal):
            self.signals.append(signal)
            self.code = 0

        def wait(self):
            return self.code

    monkeypatch.setattr(service_module.threading, "Event", Stop)
    monkeypatch.setattr(service_module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(service_module.subprocess, "Popen", Child)
    svc.supervise(8765)
    assert len(children) == 2
    assert children[1].signals == [service_module.signal.SIGINT]
    assert not svc.pid_file.exists()

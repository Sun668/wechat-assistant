import json
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from wechat_assistant.config import Settings
from wechat_assistant.projects.bridge import NativeBridge
from wechat_assistant.projects.cli import settings_for
from wechat_assistant.projects.router import ProjectRouter
from wechat_assistant.projects.runtime import ProjectRuntime
from wechat_assistant.projects.service import AppBackgroundService, MacService
from wechat_assistant.projects.storage import ProjectStore
from wechat_assistant.projects.upload_config import UploadConfig

P = "11111111-1111-4111-8111-111111111111"
Q = "22222222-2222-4222-8222-222222222222"


def event(text="", filename=None, **extra):
    items = [{"type": 1, "text_item": {"text": text}}] if text else []
    if filename:
        items.append({"type": 4, "file_item": {"file_name": filename}})
    return {"from_user_id": "owner", "message_id": uuid.uuid4().hex,
            "item_list": items, **extra}


@pytest.fixture
def gateway(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "Config.toml"
    config.write_text('# keep comment\nworkspace = "workspace"\ndefault_upload_path = ""\n')
    upload = UploadConfig(config, workspace / ".wechat-assistant")
    store = ProjectStore(workspace / ".wechat-assistant", "owner", 4096, upload_config=upload)
    for pid, name in ((P, "项目甲"), (Q, "项目乙")):
        primary = tmp_path / name
        primary.mkdir()
        store.register(pid, name, str(primary))
    router = ProjectRouter(store)
    yield store, router, upload
    store.close()


def files(store):
    return store.rows("SELECT * FROM files ORDER BY rowid")


def test_empty_config_keeps_primary_directory(gateway):
    store, router, _ = gateway
    router.ingest(event("选择项目 " + P))
    router.ingest(event(filename="test.txt"))
    assert Path(files(store)[0]["storage_root"]) == store.codex_project_dir(P)


def test_configured_external_path_and_execution_context(gateway, tmp_path):
    store, router, upload = gateway
    target = tmp_path / "outside" / "收件 with spaces"
    # A file edit is picked up without restarting the runtime.
    upload.path.write_text("default_upload_path = " + json.dumps(str(target)))
    router.ingest(event("选择项目 " + P))
    router.ingest(event(filename="test.txt"))
    row = files(store)[0]
    assert row["storage_root"] == str(target)
    store.save_file(row, b"owner data")
    router.ingest(event("总结文件"))
    job = NativeBridge(store).claim(P)
    assert Path(job["project_directory"]) == store.codex_project_dir(P)
    assert job["files"][0]["path"] == str(target / "test.txt")
    assert "已保存到接收目录" in store.one(
        "SELECT body FROM outbox WHERE dedupe_key=?", ("download:" + row["id"],)
    )["body"]


def test_command_sets_queries_and_resets_without_selecting_project(gateway, tmp_path):
    store, router, upload = gateway
    target = tmp_path / "incoming files"
    raw = event("/upload-path " + str(target))
    assert router.ingest(raw)["status"] == "command"
    assert upload.get() == str(target)
    assert target.is_dir()
    assert "# keep comment" in upload.path.read_text()
    assert tomllib.loads(upload.path.read_text())["workspace"] == "workspace"
    assert router.ingest(raw)["status"] == "duplicate"
    assert router.ingest(event("/upload-path"))["status"] == "command"
    assert str(target) in store.rows("SELECT body FROM outbox ORDER BY rowid DESC")[0]["body"]
    assert router.ingest(event("/upload-path reset"))["status"] == "command"
    assert tomllib.loads(upload.path.read_text())["default_upload_path"] == ""
    assert not store.rows("SELECT * FROM jobs")


@pytest.mark.parametrize("extra", [{"from_user_id": "stranger"}, {"group_id": "group"}])
def test_only_authenticated_owner_can_change_config(gateway, tmp_path, extra):
    store, router, upload = gateway
    before = upload.path.read_text()
    assert router.ingest(event("/upload-path " + str(tmp_path / "forbidden"), **extra))["status"] == "ignored"
    assert upload.path.read_text() == before
    assert not store.rows("SELECT * FROM outbox")


def test_echo_cannot_change_config(gateway, tmp_path):
    store, router, upload = gateway
    store.enqueue("old", "old", "original")
    store.db.execute("UPDATE outbox SET external_id='echo'")
    assert router.ingest(event("/upload-path " + str(tmp_path / "forbidden"),
                               message_id="echo"))["status"] == "echo"
    assert upload.get() == ""


def test_attachment_on_configuration_command_is_rejected(gateway, tmp_path):
    store, router, upload = gateway
    assert router.ingest(event("/upload-path " + str(tmp_path / "other"),
                               filename="file.txt"))["status"] == "rejected"
    assert upload.get() == ""
    assert not files(store)


def test_changing_config_and_project_does_not_redirect_pending_files(gateway, tmp_path):
    store, router, upload = gateway
    first, second = tmp_path / "first", tmp_path / "second"
    router.ingest(event("/upload-path " + str(first)))
    router.ingest(event("选择项目 " + P))
    router.ingest(event(filename="one.txt"))
    router.ingest(event("/upload-path " + str(second)))
    router.ingest(event("选择项目 " + Q))
    router.ingest(event(filename="two.txt"))
    router.ingest(event("/upload-path reset"))
    router.ingest(event(filename="three.txt"))
    a, b, c = files(store)
    assert a["project_id"] == P and a["storage_root"] == str(first)
    assert b["project_id"] == Q and b["storage_root"] == str(second)
    assert Path(c["storage_root"]) == store.codex_project_dir(Q)
    store.save_file(a, b"first")
    assert (first / "one.txt").read_bytes() == b"first"
    assert not (second / "one.txt").exists()


def test_shared_directory_reserves_pending_names_across_projects(gateway, tmp_path):
    store, router, _ = gateway
    target = tmp_path / "shared"
    router.ingest(event("/upload-path " + str(target)))

    def receive(pid):
        return router.ingest(event("[" + pid + "]", filename="same.txt"))

    with ThreadPoolExecutor(2) as pool:
        assert all(result["status"] == "accepted" for result in pool.map(receive, [P, Q]))
    a, b = files(store)
    assert {a["name"], b["name"]} == {"same.txt", "same (2).txt"}
    store.save_file(a, b"one")
    store.save_file(b, b"two")
    assert store.file_content(files(store)[0]) == b"one"
    assert store.file_content(files(store)[1]) == b"two"


def test_path_is_preserved_after_restart_and_evidence_still_checked(gateway, tmp_path):
    store, router, upload = gateway
    target = tmp_path / "incoming"
    router.ingest(event("/upload-path " + str(target)))
    router.ingest(event("选择项目 " + P))
    router.ingest(event(filename="file.txt"))
    row = files(store)[0]
    store.save_file(row, b"original")
    restarted = ProjectStore(store.root.parent, "owner", 4096,
                             upload_config=UploadConfig(upload.path, store.root.parent))
    try:
        assert restarted.receive_directory(P) == target
        (target / "file.txt").write_bytes(b"changed")
        with pytest.raises(ValueError, match="快照已失效"):
            restarted.file_content(files(restarted)[0])
    finally:
        restarted.close()


@pytest.mark.parametrize("kind", ["state", "native", "git", "file", "relative", "symlink"])
def test_invalid_paths_leave_configuration_unchanged(gateway, tmp_path, monkeypatch, kind):
    store, router, upload = gateway
    native = tmp_path / "native"
    monkeypatch.setenv("CODEX_HOME", str(native))
    target = {
        "state": store.root.parent / "credentials",
        "native": native / "auth",
        "git": tmp_path / ".git" / "objects",
        "file": upload.path,
        "relative": Path("relative/path"),
        "symlink": tmp_path / "link" / "incoming",
    }[kind]
    if kind == "symlink":
        (tmp_path / "link").symlink_to(tmp_path / "workspace", target_is_directory=True)
    assert router.ingest(event("/upload-path " + str(target)))["status"] == "rejected"
    assert upload.get() == ""


def test_unwritable_target_does_not_persist(gateway, tmp_path, monkeypatch):
    _, router, upload = gateway

    def denied(**kwargs):
        raise PermissionError()

    monkeypatch.setattr("wechat_assistant.projects.upload_config.tempfile.TemporaryFile", denied)
    assert router.ingest(event("/upload-path " + str(tmp_path / "denied")))["status"] == "rejected"
    assert upload.get() == ""


def test_config_file_reaches_service_child_processes(tmp_path):
    config = tmp_path / "custom.toml"
    config.write_text('default_upload_path = ""')
    settings = settings_for(tmp_path, config)
    assert settings.launcher_config == config
    for cls in (MacService, AppBackgroundService):
        args = cls(settings).definition(8765)["ProgramArguments"]
        assert args[args.index("--config") + 1] == str(config)


def test_runtime_uses_supplied_configuration(gateway):
    store, _, upload = gateway
    settings = Settings(workspace=store.root.parent.parent, data_dir=store.root.parent,
                        owner_id="owner", launcher_config=upload.path)
    runtime = ProjectRuntime(settings, transport=object())
    try:
        assert runtime.store.upload_config.path == upload.path
    finally:
        runtime.store.close()

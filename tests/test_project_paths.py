import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from wechat_assistant.config import Settings
from wechat_assistant.projects.api import create_app
from wechat_assistant.projects.cli import main
from wechat_assistant.projects.project_paths import is_excluded_project_path
from wechat_assistant.projects.runtime import ProjectRuntime
from wechat_assistant.projects.storage import ProjectStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    # A different username and home location must not depend on the developer's Mac.
    home = tmp_path / "another-user-home"
    (home / "Documents" / "ChatGPT").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.mark.parametrize("relative,excluded", [
    ("Documents/ChatGPT", True),
    ("Documents/ChatGPT/微信助理", True),
    ("Documents/ChatGPT/一层/二层", True),
    (".codex/.chatgpt-projects/cloud", True),
    ("Documents/git/wechat-assistant", False),
    ("Documents/ChatGPT-backup/project", False),
    ("Documents/git/ChatGPT-example", False),
    ("Documents/git/ChatGPT/project", False),
])
def test_exclusions_follow_current_home_and_directory_boundaries(home, relative, excluded):
    assert is_excluded_project_path(str(home / relative)) is excluded


def test_directory_alias_is_excluded(home):
    alias = home / "alias"
    alias.symlink_to(home / "Documents" / "ChatGPT", target_is_directory=True)
    assert is_excluded_project_path(str(alias / "微信助理"))


def test_resolved_managed_directory_is_excluded(home):
    managed = home / "Documents" / "ChatGPT"
    managed.rmdir()
    actual = home / "managed-elsewhere"
    actual.mkdir()
    managed.symlink_to(actual, target_is_directory=True)
    assert is_excluded_project_path(str(actual / "微信助理"))


def record(path, name):
    path.mkdir(parents=True, exist_ok=True)
    return {"id": str(uuid.uuid4()), "name": name,
            "roots": [{"path": str(path)}], "metadata": {}}


def event(text):
    return {"from_user_id": "owner", "message_id": uuid.uuid4().hex,
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def test_refresh_excludes_managed_root_and_preserves_raw_catalog(home, tmp_path):
    normal = record(tmp_path / "repo", "正常项目")
    excluded = record(home / "Documents" / "ChatGPT" / "微信助理", "排除项目")
    source = SimpleNamespace(list_projects=lambda: [{"data": [normal, excluded]}])
    settings = Settings(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant",
                        owner_id="owner")
    runtime = ProjectRuntime(settings, transport=object(), project_source=source)
    try:
        runtime.ingest(event("项目列表"))
        assert [p["id"] for p in runtime.router.available_projects()] == [normal["id"]]
        raw = runtime.store.one(
            "SELECT detail FROM audit WHERE action='native_project_catalog_source'"
        )["detail"]
        assert excluded["id"] in raw
        assert (home / "Documents" / "ChatGPT" / "微信助理").is_dir()
    finally:
        runtime.store.close()


def test_failed_refresh_filters_cached_projects_and_blocks_stale_selection(home, tmp_path):
    normal = record(tmp_path / "repo", "正常项目")
    previous = record(tmp_path / "previous", "历史项目")
    excluded = home / "Documents" / "ChatGPT" / "微信助理"
    excluded.mkdir()

    def unavailable():
        raise TimeoutError()

    settings = Settings(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant",
                        owner_id="owner")
    runtime = ProjectRuntime(settings, transport=object(), project_source=SimpleNamespace(
        list_projects=unavailable
    ))
    try:
        store = runtime.store
        for item in (normal, previous):
            store.register(item["id"], item["name"], item["roots"][0]["path"])
        task = store.new_task(previous["id"])
        # Simulate a historical registration made before this policy existed.
        store.db.execute("UPDATE projects SET source_path=? WHERE id=?", (str(excluded), previous["id"]))
        store.set_value("project_choices", json.dumps([previous["id"], normal["id"]]))
        assert runtime.ingest(event("选择项目 1"))["status"] == "rejected"
        runtime.ingest(event("项目列表"))
        reply = store.rows("SELECT body FROM outbox ORDER BY rowid DESC")[0]["body"]
        assert "刷新失败" in reply and normal["name"] in reply and previous["name"] not in reply
        assert runtime.ingest(event("选择项目 " + previous["id"]))["status"] == "rejected"
        assert runtime.ingest(event("[" + previous["id"] + "] 请求"))["status"] == "rejected"
        assert store.one("SELECT id FROM tasks WHERE id=?", (task,))
        assert store.project(previous["id"])
        assert not store.rows("SELECT * FROM jobs")
        with TestClient(create_app(runtime, background=False)) as client:
            headers = {"Authorization": "Bearer " + runtime.api_token}
            assert [p["id"] for p in client.get("/projects", headers=headers).json()] == [normal["id"]]
    finally:
        runtime.store.close()


def test_offline_import_excludes_current_users_managed_directory(home, tmp_path, monkeypatch):
    normal = record(tmp_path / "repo", "正常项目")
    excluded = record(home / "Documents" / "ChatGPT" / "微信助理", "排除项目")
    exported = tmp_path / "projects.json"
    exported.write_text(json.dumps({"projects": [
        {"projectId": p["id"], "projectKind": "local", "label": p["name"],
         "path": p["roots"][0]["path"]} for p in (normal, excluded)
    ]}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("sys.argv", ["clawbot-projects", "--workspace", str(workspace),
                                    "projects-import", str(exported), "--offline"])
    main()
    store = ProjectStore(workspace / ".wechat-assistant", "self", 4096)
    try:
        assert [p["id"] for p in store.projects()] == [normal["id"]]
    finally:
        store.close()

import json
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from wechat_assistant.adapters.codex_projects import CatalogUnavailable, CodexProjects
from wechat_assistant.config import Settings
from wechat_assistant.projects.catalog import refresh_initial_catalog
from wechat_assistant.projects.runtime import ProjectRuntime


def event(text="项目列表", **kwargs):
    return {"from_user_id": "owner", "message_id": uuid.uuid4().hex,
            "item_list": [{"type": 1, "text_item": {"text": text}}], **kwargs}


class Source:
    def __init__(self):
        self.pages, self.calls, self.error = [{"data": []}], 0, None

    def list_projects(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.pages


@pytest.fixture
def runtime(tmp_path):
    source = Source()
    settings = Settings(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant",
                        owner_id="owner")
    rt = ProjectRuntime(settings, transport=SimpleNamespace(), project_source=source)
    yield rt, source
    rt.store.close()


def record(tmp_path, name="项目甲", pid=None):
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return {"id": pid or str(uuid.uuid4()), "name": name,
            "roots": [{"path": str(path)}], "metadata": {}}


def body(rt):
    return rt.store.rows("SELECT body FROM outbox ORDER BY rowid DESC LIMIT 1")[0]["body"]


def test_refresh_each_new_owner_command_retains_threads(runtime, tmp_path):
    rt, source = runtime
    native = record(tmp_path)
    old_id = str(uuid.uuid4())
    rt.store.register(old_id, native["name"], native["roots"][0]["path"])
    task = rt.store.new_task(old_id)
    source.pages = [{"data": [native]}]
    first = event()
    rt.ingest(first)
    assert "已刷新" in body(rt)
    assert json.loads(rt.store.get_value("project_choices")) == [old_id]
    assert rt.store.one("SELECT project_id FROM tasks WHERE id=?", (task,))["project_id"] == old_id
    assert rt.ingest(first)["status"] == "duplicate"
    assert source.calls == 1
    native["name"] = "改名"
    rt.ingest(event())
    assert source.calls == 2
    assert rt.store.project(old_id)["name"] == "改名"
    assert rt.store.get_value("native_project_alias:" + native["id"]) == old_id


@pytest.mark.parametrize("extra", [{"from_user_id": "stranger"}, {"group_id": "group"}])
def test_non_owner_never_refreshes_or_replies(runtime, extra):
    rt, source = runtime
    assert rt.ingest(event(**extra))["status"] == "ignored"
    assert source.calls == 0
    assert not rt.store.rows("SELECT * FROM outbox")


def test_echo_does_not_refresh(runtime):
    rt, source = runtime
    rt.store.enqueue("old", "old", "original")
    rt.store.db.execute("UPDATE outbox SET external_id='echo'")
    assert rt.ingest(event(message_id="echo"))["status"] == "echo"
    assert source.calls == 0


def test_failed_refresh_preserves_previous_menu(runtime, tmp_path):
    rt, source = runtime
    native = record(tmp_path)
    source.pages = [{"data": [native]}]
    rt.ingest(event())
    previous = rt.store.get_value("project_choices")
    source.error = TimeoutError("private-detail")
    rt.ingest(event())
    assert "刷新失败" in body(rt) and "上次保存" in body(rt)
    assert native["name"] in body(rt) and "private-detail" not in body(rt)
    assert rt.store.get_value("project_choices") == previous


def test_bad_second_page_does_not_partially_register(runtime, tmp_path):
    rt, source = runtime
    source.pages = [{"data": [record(tmp_path)]}, {"data": [{"id": "invalid"}]}]
    rt.ingest(event())
    assert not rt.store.projects()
    assert "刷新失败" in body(rt)
    assert rt.store.rows("SELECT 1 FROM audit WHERE action='native_project_catalog_source'")


def test_removed_project_hidden_without_deleting_history(runtime, tmp_path):
    rt, source = runtime
    native = record(tmp_path)
    source.pages = [{"data": [native]}]
    rt.ingest(event())
    rt.ingest(event("选择项目 1"))
    task = rt.store.new_task(native["id"])
    source.pages = [{"data": []}]
    rt.ingest(event())
    assert json.loads(rt.store.get_value("project_choices")) == []
    assert not rt.store.get_value("active_project")
    assert rt.store.project(native["id"])
    assert rt.store.one("SELECT id FROM tasks WHERE id=?", (task,))
    assert rt.ingest(event("选择项目 " + native["id"]))["status"] == "rejected"


def test_refresh_survives_restart_and_manual_import(runtime, tmp_path):
    rt, source = runtime
    native = record(tmp_path)
    source.pages = [{"data": [native]}]
    rt.ingest(event())
    restarted = ProjectRuntime(rt.settings, transport=SimpleNamespace(), project_source=source)
    try:
        source.error = RuntimeError()
        restarted.ingest(event())
        assert native["name"] in body(restarted)
        second = record(tmp_path, "项目乙")
        restarted.store.register(second["id"], second["name"], second["roots"][0]["path"])
        assert second["name"] in restarted.router.project_menu()
    finally:
        restarted.store.close()


def test_concurrent_duplicate_has_single_refresh_and_reply(runtime):
    rt, source = runtime
    raw = event()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(rt.ingest, [raw, raw]))
    assert source.calls == 1
    assert sorted(r["status"] for r in results) == ["command", "duplicate"]
    assert len(rt.store.rows("SELECT * FROM outbox")) == 1


def test_network_fetch_does_not_hold_database_transaction(runtime):
    rt, source = runtime
    ready, release = threading.Event(), threading.Event()

    def fetch():
        ready.set()
        assert release.wait(2)
        return [{"data": []}]

    source.list_projects = fetch
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(rt.ingest, event())
        try:
            assert ready.wait(2)
            write = pool.submit(rt.store.set_value, "unrelated", "ok")
            write.result(timeout=1)
        finally:
            release.set()
        future.result(timeout=2)


def test_jsonl_protocol_paginates_and_rejects_server_tool_requests():
    server = '''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {}
    elif method == "project/list":
        cursor = request["params"]["cursor"]
        if cursor is None:
            print(json.dumps({"id":"tool", "method":"item/commandExecution/requestApproval"}), flush=True)
        result = {"data": [], "nextCursor": "next" if cursor is None else None}
    else:
        if request.get("id") == "tool":
            assert request["error"]["code"] == -32601
        continue
    print(json.dumps({"id":request["id"], "result":result}), flush=True)
'''
    with subprocess.Popen([sys.executable, "-u", "-c", server], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, bufsize=0) as process:
        try:
            pages = CodexProjects(None, timeout=2)._read_catalog(process)
            assert len(pages) == 2
        finally:
            process.terminate()
            process.wait(timeout=2)


def test_jsonl_timeout():
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0) as process:
        try:
            with pytest.raises(CatalogUnavailable, match="超时"):
                CodexProjects(None, timeout=0.05)._read_catalog(process)
        finally:
            process.terminate()
            process.wait(timeout=2)


def test_initialization_fetches_and_persists_once(runtime, tmp_path):
    rt, source = runtime
    native = record(tmp_path)
    source.pages = [{"data": [native]}]
    assert "已刷新" in refresh_initial_catalog(rt.settings, source)
    assert source.calls == 1
    assert rt.store.project(native["id"])["name"] == native["name"]
    assert json.loads(rt.store.get_value("project_catalog_ids")) == [native["id"]]


def test_initialization_failure_retains_previous_snapshot(runtime, tmp_path):
    rt, source = runtime
    source.pages = [{"data": [record(tmp_path)]}]
    refresh_initial_catalog(rt.settings, source)
    before = rt.store.get_value("project_catalog_ids")
    source.error = RuntimeError("private")
    assert "刷新失败" in refresh_initial_catalog(rt.settings, source)
    assert rt.store.get_value("project_catalog_ids") == before
    assert rt.store.get_value("project_catalog_status") == "failed"


def test_recreated_native_project_cannot_inherit_old_threads(runtime, tmp_path):
    rt, source = runtime
    native = record(tmp_path)
    source.pages = [{"data": [native]}]
    rt.ingest(event())
    task = rt.store.new_task(native["id"])
    replacement = {**native, "id": str(uuid.uuid4())}
    source.pages = [{"data": [replacement]}]
    rt.ingest(event())
    assert rt.store.one("SELECT project_id FROM tasks WHERE id=?", (task,))["project_id"] == native["id"]
    assert not json.loads(rt.store.get_value("project_choices"))
    assert "映射需核对" in body(rt)

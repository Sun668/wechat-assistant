import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from wechat_assistant.config import Settings
from wechat_assistant.projects.runtime import ProjectRuntime
from wechat_assistant.projects.storage import ProjectStore

P = "11111111-1111-4111-8111-111111111111"


class NoModel:
    def readiness(self):
        return "ready"

    def run(self, *args):
        pytest.fail("Unassigned receipts must never execute a model")


def event(text="", filename=None, **extra):
    items = [{"type": 1, "text_item": {"text": text}}] if text else []
    if filename:
        items.append({"type": 4, "file_item": {"file_name": filename}})
    return {"from_user_id": "owner", "message_id": uuid.uuid4().hex,
            "item_list": items, **extra}


@pytest.fixture
def runtime(tmp_path):
    config = tmp_path / "Config.toml"
    config.write_text('default_upload_path = ""')
    settings = Settings(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant",
                        owner_id="owner", launcher_config=config)
    rt = ProjectRuntime(settings, transport=SimpleNamespace(can_notify=lambda: False), engine=NoModel())
    rt.ilink = SimpleNamespace(media=SimpleNamespace(download=lambda _: b"content"))
    yield rt
    rt.execution.stop()
    rt.store.close()


def files(rt):
    return rt.store.rows("SELECT * FROM files ORDER BY rowid")


def set_path(rt, tmp_path):
    path = tmp_path / "incoming"
    rt.ingest(event("/upload-path " + str(path)))
    return path


def test_without_path_or_project_rejects_files(runtime):
    raw = event(filename="note.txt")
    assert runtime.ingest(raw)["status"] == "rejected"
    assert not files(runtime)
    assert not runtime.store.rows("SELECT * FROM jobs")
    assert runtime.store.rows("SELECT * FROM source_events WHERE external_id=?", (raw["message_id"],))


def test_unassigned_receipt_downloads_without_project_or_model(runtime, tmp_path):
    target = set_path(runtime, tmp_path)
    raw = event("请分析这个文件", filename="note.txt")
    assert runtime.ingest(raw)["status"] == "accepted"
    assert runtime.ingest(raw)["status"] == "duplicate"
    assert len(files(runtime)) == 1
    assert files(runtime)[0]["project_id"] is None
    assert not runtime.store.projects()
    assert not runtime.store.rows("SELECT * FROM tasks")
    assert {j["kind"] for j in runtime.store.rows("SELECT * FROM jobs")} == {"file_receive"}
    assert runtime.download_next()
    assert (target / "note.txt").read_bytes() == b"content"
    assert files(runtime)[0]["state"] == "ready"
    assert runtime.store.rows("SELECT project_id FROM ingress WHERE external_id=?", (raw["message_id"],))[0]["project_id"] is None
    assert all(r["project_id"] is None for r in runtime.store.rows("SELECT * FROM outbox"))
    runtime.execution.tick()
    replies = "\n".join(r["body"] for r in runtime.store.rows("SELECT * FROM outbox"))
    assert "【收件箱】" in replies and "未提交给 Codex" in replies


def test_pending_unassigned_file_keeps_path_after_reset_and_selection(runtime, tmp_path):
    target = set_path(runtime, tmp_path)
    runtime.ingest(event(filename="old.txt"))
    runtime.ingest(event("/upload-path reset"))
    primary = tmp_path / "project"
    primary.mkdir()
    runtime.store.register(P, "项目", str(primary))
    runtime.ingest(event("选择项目 " + P))
    assert runtime.download_next()
    assert (target / "old.txt").is_file()
    assert not (primary / "old.txt").exists()
    assert files(runtime)[0]["project_id"] is None
    runtime.ingest(event("总结项目"))
    assert runtime.bridge.claim(P)["files"] == []


def test_unassigned_failures_can_be_retried_and_listed(runtime, tmp_path):
    target = set_path(runtime, tmp_path)
    runtime.ingest(event(filename="retry.txt"))

    def fail(_):
        raise OSError()

    runtime.ilink.media.download = fail
    assert runtime.download_next()
    assert files(runtime)[0]["state"] == "failed"
    runtime.ingest(event("/upload-path reset"))
    assert runtime.ingest(event("重试文件《retry.txt》"))["status"] == "command"
    runtime.ilink.media.download = lambda _: b"retried"
    assert runtime.download_next()
    assert (target / "retry.txt").read_bytes() == b"retried"
    assert runtime.ingest(event("文件列表"))["status"] == "command"
    reply = runtime.store.rows("SELECT body FROM outbox ORDER BY rowid DESC")[0]["body"]
    assert "【收件箱】" in reply and "retry.txt" in reply and "已保存到接收目录" in reply


def test_restart_recovers_unassigned_download_without_reassigning(runtime, tmp_path):
    target = set_path(runtime, tmp_path)
    runtime.ingest(event(filename="restart.txt"))
    runtime.store.db.execute("UPDATE files SET state='downloading'")
    runtime.ingest(event("/upload-path reset"))
    restored = ProjectRuntime(runtime.settings, transport=SimpleNamespace(can_notify=lambda: False),
                              engine=NoModel())
    try:
        restored.store.recover()
        restored.ilink = runtime.ilink
        assert restored.download_next()
        assert (target / "restart.txt").read_bytes() == b"content"
        assert files(restored)[0]["project_id"] is None
        restored.execution.tick()
    finally:
        restored.execution.stop()
        restored.store.close()


def test_parallel_unassigned_uploads_reserve_distinct_names(runtime, tmp_path):
    set_path(runtime, tmp_path)
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(runtime.ingest, [event(filename="same.txt"), event(filename="same.txt")]))
    assert all(r["status"] == "accepted" for r in result)
    assert {f["name"] for f in files(runtime)} == {"same.txt", "same (2).txt"}


@pytest.mark.parametrize("extra", [{"from_user_id": "other"}, {"group_id": "group"}])
def test_unassigned_uploads_still_require_owner_auth(runtime, tmp_path, extra):
    set_path(runtime, tmp_path)
    assert runtime.ingest(event(filename="no.txt", **extra))["status"] == "ignored"
    assert not files(runtime)


def test_quote_of_unassigned_file_does_not_infer_project(runtime, tmp_path):
    set_path(runtime, tmp_path)
    raw = event(filename="unassigned.txt")
    runtime.ingest(raw)
    primary = tmp_path / "project"
    primary.mkdir()
    runtime.store.register(P, "项目", str(primary))
    runtime.ingest(event("选择项目 " + P))
    quoted = event("总结文件")
    quoted["item_list"][0]["ref_msg"] = {"svr_id": raw["message_id"]}
    assert runtime.ingest(quoted)["status"] == "rejected"
    assert not runtime.store.rows("SELECT * FROM tasks")
    with pytest.raises(ValueError, match="不允许模型"):
        runtime.store.new_job(None, "invalid", "message")


def legacy_database(tmp_path):
    root = tmp_path / ".wechat-assistant" / "clawbot-projects"
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "gateway.sqlite")
    db.executescript('''
        CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,
          folder TEXT,source_path TEXT,upload_path TEXT,name_override TEXT);
        CREATE TABLE tasks(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,
          project_id TEXT NOT NULL REFERENCES projects(id),thread_id TEXT UNIQUE,created_at TEXT NOT NULL);
        CREATE TABLE files(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
          source_key TEXT UNIQUE,name TEXT NOT NULL,state TEXT NOT NULL,media TEXT,size INTEGER,
          sha256 TEXT,created_at TEXT NOT NULL,relative_path TEXT,storage_root TEXT);
        CREATE TABLE jobs(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,
          project_id TEXT NOT NULL REFERENCES projects(id),task_id TEXT REFERENCES tasks(id),
          source_event TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',
          file_id TEXT REFERENCES files(id),state TEXT NOT NULL DEFAULT 'queued',
          claim_token TEXT,error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE job_files(job_id TEXT REFERENCES jobs(id),file_id TEXT REFERENCES files(id),
          sha256 TEXT NOT NULL,PRIMARY KEY(job_id,file_id));
        CREATE INDEX files_by_name ON files(name);
    ''')
    db.execute("INSERT INTO projects(id,name,created_at,folder) VALUES(?,?,?,?)", (P, "原项目", "before", "old"))
    db.execute("INSERT INTO tasks VALUES(7,'T000007',?,'exact-thread','before')", (P,))
    db.execute("INSERT INTO files(id,project_id,source_key,name,state,sha256,created_at) "
               "VALUES('f-old',?,'source:0','old.txt','ready','hash','before')", (P,))
    db.execute("INSERT INTO jobs(seq,id,project_id,task_id,source_event,kind,state,created_at) "
               "VALUES(12,'J000012',?,'T000007','source','message','completed','before')", (P,))
    db.execute("INSERT INTO job_files VALUES('J000012','f-old','hash')")
    db.execute("UPDATE sqlite_sequence SET seq=900 WHERE name='jobs'")
    db.commit()
    db.close()
    return root


def test_upgrade_preserves_records_foreign_keys_and_sequence(tmp_path):
    root = legacy_database(tmp_path)
    store = ProjectStore(root.parent, "owner", 4096)
    try:
        assert store.one("SELECT thread_id FROM tasks WHERE id='T000007'")["thread_id"] == "exact-thread"
        assert store.one("SELECT sha256 FROM job_files WHERE job_id='J000012'")["sha256"] == "hash"
        assert not store.rows("PRAGMA foreign_key_check")
        assert store.new_job(P, "new", "message", task_id="T000007") == "J000901"
        assert store.rows("SELECT name FROM sqlite_master WHERE name='files_by_name'")
        for table in ("files", "jobs"):
            assert next(r for r in store.rows(f"PRAGMA table_info({table})") if r["name"] == "project_id")["notnull"] == 0
        backups = list((root / "backups").glob("*.sqlite"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("SELECT thread_id FROM tasks").fetchone()[0] == "exact-thread"
    finally:
        store.close()
    reopened = ProjectStore(root.parent, "owner", 4096)
    reopened.close()
    assert len(list((root / "backups").glob("*.sqlite"))) == 1


def test_failed_upgrade_rolls_back_schema_without_losing_history(tmp_path):
    root = legacy_database(tmp_path)
    with sqlite3.connect(root / "gateway.sqlite") as db:
        db.execute("INSERT INTO job_files VALUES('missing','f-old','hash')")
    with pytest.raises(ValueError, match="升级校验失败"):
        ProjectStore(root.parent, "owner", 4096)
    with sqlite3.connect(root / "gateway.sqlite") as db:
        assert next(r for r in db.execute("PRAGMA table_info(files)") if r[1] == "project_id")[3] == 1
        assert db.execute("SELECT thread_id FROM tasks").fetchone()[0] == "exact-thread"


def test_concurrent_legacy_upgrade_is_safe(tmp_path):
    root = legacy_database(tmp_path)

    def open_and_close(_):
        store = ProjectStore(root.parent, "owner", 4096)
        try:
            assert not store.rows("PRAGMA foreign_key_check")
            return len(store.rows("SELECT * FROM files"))
        finally:
            store.close()

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(open_and_close, range(2))) == [1, 1]

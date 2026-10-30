import fcntl
import importlib.util
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cleanup_workspace", Path(__file__).resolve().parents[1] / "scripts/cleanup_workspace.py"
)
cleanup_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup_module)


def workspace(tmp_path):
    state = tmp_path / ".wechat-assistant"
    (state / "cache/huggingface").mkdir(parents=True)
    (state / "cache/huggingface/model.bin").write_bytes(b"unused model")
    (state / "credentials").mkdir()
    (state / "credentials/ilink.json").write_text("keep binding")
    (state / "clawbot-projects").mkdir()
    with sqlite3.connect(state / "clawbot-projects/gateway.sqlite") as db:
        db.execute("CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO kv VALUES('transport_cutover_done','true')")
    with sqlite3.connect(state / "assistant.sqlite") as db:
        db.execute("CREATE TABLE source_events(raw TEXT)")
        db.execute("INSERT INTO source_events VALUES('preserve original record')")
    (state / "attachments/F000001").mkdir(parents=True)
    (state / "attachments/F000001/用户文档.docx").write_bytes(b"keep document")
    return state


def test_cleanup_only_known_old_runtime_data_and_preserves_sources(tmp_path):
    state = workspace(tmp_path)
    (tmp_path / "用户原始文件.txt").write_text("never delete")
    (state / "unknown-directory").mkdir()
    preview = cleanup_module.cleanup(tmp_path)
    assert preview["applied"] is False
    assert (state / "cache").exists()
    report = cleanup_module.cleanup(tmp_path, apply=True)
    assert not (state / "cache").exists()
    assert not (state / "assistant.sqlite").exists()
    assert not (state / "attachments").exists()
    assert (state / "retained-files/用户文档.docx").read_bytes() == b"keep document"
    assert (state / "credentials/ilink.json").read_text() == "keep binding"
    assert (state / "unknown-directory").exists()
    assert (tmp_path / "用户原始文件.txt").read_text() == "never delete"
    assert report["archive"] == "archive/legacy-history.sqlite"
    with sqlite3.connect(state / report["archive"]) as db:
        assert (
            db.execute("SELECT raw FROM source_events").fetchone()[0] == "preserve original record"
        )


def test_cleanup_refuses_running_runtime(tmp_path):
    state = workspace(tmp_path)
    with (state / "runtime.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            cleanup_module.cleanup(tmp_path, apply=True)
    assert (state / "cache").exists()
    assert not (state / "archive").exists()


def test_cleanup_refuses_symlink_targets_without_deleting_external_data(tmp_path):
    state = workspace(tmp_path)
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (state / "vectors").symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        cleanup_module.cleanup(tmp_path, apply=True)
    assert (state / "cache").exists()
    assert (outside / "keep").read_text() == "keep"

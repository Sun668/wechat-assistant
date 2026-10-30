"""Simulated local Codex routing; never connects to a real WeChat contact."""

import asyncio
import json
import sys
import threading
import time
import uuid

from test_clawbot_projects import Transport, event

from wechat_assistant.config import Settings
from wechat_assistant.projects.local_engine import LocalAnswer, SharedCodexEngine
from wechat_assistant.projects.router import ProjectRouter
from wechat_assistant.projects.runtime import ProjectRuntime
from wechat_assistant.projects.storage import ProjectStore


class RecordingCodex:
    def __init__(self):
        self.calls = []

    def readiness(self):
        return "ready"

    def subscription_auth(self):
        return True

    def run(self, job, prompt, bind, progress, cancelled):
        if job["kind"] == "intent_classification":
            return LocalAnswer("intent-" + job["job_id"], json.dumps({
                "intent": "ordinary", "kind": None, "time": None, "date": None,
                "weekday": None, "body": None, "clarification": None,
            }))
        self.calls.append((job, prompt))
        thread = job["thread_id"] or "codex-" + job["task_id"]
        bind(thread)
        progress("started")
        return LocalAnswer(thread, "本地结果：" + job["text"])


def test_fresh_workspace_has_no_scheduled_dispatch_or_external_bridge(tmp_path):
    store = ProjectStore(tmp_path / ".wechat-assistant", "owner", 1024)
    assert not store.rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='native_dispatch'"
    )
    assert not store.rows("SELECT key FROM kv WHERE key LIKE 'native_relay_%'")
    store.close()


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition timed out")


def test_first_client_folder_is_used_for_receipt_and_codex(tmp_path):
    store = ProjectStore(tmp_path / ".wechat-assistant", "owner", 1024 * 1024)
    pid = str(uuid.uuid4())
    first, second = tmp_path / "primary", tmp_path / "secondary"
    first.mkdir()
    second.mkdir()
    store.register(pid, "本地项目", str(first), str(second))
    store.set_value("execution_backend", "codex_shared")
    router = ProjectRouter(store)
    assert "Codex 本地目录" in router.project_menu()
    assert router.ingest(event("选择项目 本地项目"))["status"] == "command"
    assert store.codex_project_dir(pid) == first
    assert store.project_dir(pid) == first
    assert router.ingest(event(files=["资料.txt"]))["status"] == "accepted"
    file = store.one("SELECT * FROM files")
    assert file["storage_root"] == str(first)
    store.save_file(file, b"owner document")
    assert (first / "资料.txt").read_bytes() == b"owner document"
    assert not (second / "资料.txt").exists()
    store.close()


def test_unbound_or_cloud_project_cannot_be_selected_for_codex(tmp_path):
    store = ProjectStore(tmp_path / ".wechat-assistant", "owner", 1024)
    override = tmp_path / "cloud-upload-override"
    override.mkdir()
    store.register("g-p-cloud-only-12345678", "云端项目", upload_path=str(override))
    store.set_value("execution_backend", "codex_shared")
    router = ProjectRouter(store)
    assert "云端项目" not in router.project_menu()
    assert router.ingest(event("选择项目 云端项目"))["status"] == "rejected"
    try:
        store.receive_directory("g-p-cloud-only-12345678")
    except ValueError as exc:
        assert "本地项目 ID" in str(exc)
    else:
        raise AssertionError("云端项目不能借旧上传目录接收文件")
    assert not store.get_value("active_project")
    store.close()


def test_runtime_runs_selected_project_and_resumes_exact_thread(tmp_path):
    settings = Settings(
        workspace=tmp_path,
        data_dir=tmp_path / ".wechat-assistant",
        owner_id="owner",
        execution_backend="codex_shared",
    )
    engine = RecordingCodex()
    runtime = ProjectRuntime(settings, transport=Transport(), engine=engine)
    pid = str(uuid.uuid4())
    primary = tmp_path / "client-primary"
    primary.mkdir()
    runtime.store.register(pid, "客户端项目", str(primary))

    async def exercise():
        await runtime.start()
        runtime.router.ingest(event("选择项目 客户端项目"))
        first = runtime.router.ingest(event("FIRST_FACT"))
        runtime.execution.tick()
        wait_for(lambda: len(engine.calls) == 1 and runtime.execution.active[pid].done())
        runtime.execution.tick()
        second = runtime.router.ingest(event("继续处理"))
        runtime.execution.tick()
        wait_for(lambda: len(engine.calls) == 2 and runtime.execution.active[pid].done())
        runtime.execution.tick()
        assert first["task_id"] == second["task_id"]
        assert engine.calls[0][0]["project_directory"] == str(primary)
        assert engine.calls[1][0]["thread_id"] == "codex-" + first["task_id"]
        assert "recent_delivered_project_turns" not in engine.calls[1][1]
        assert "FIRST_FACT" not in engine.calls[1][1]
        assert runtime.router.ingest(event("新对话"))["status"] == "accepted"
        third = runtime.router.ingest(event("从新会话开始"))
        runtime.execution.tick()
        wait_for(lambda: len(engine.calls) == 3 and runtime.execution.active[pid].done())
        runtime.execution.tick()
        assert third["task_id"] != first["task_id"]
        assert engine.calls[2][0]["thread_id"] is None
        assert (
            runtime.store.one("SELECT thread_id FROM tasks WHERE id=?", (third["task_id"],))[
                "thread_id"
            ]
            == "codex-" + third["task_id"]
        )
        assert runtime.health()["native_executor"] == "codex_shared_local"
        await runtime.stop()

    asyncio.run(exercise())


def test_shared_codex_uses_native_home_and_official_exec_resume(tmp_path, monkeypatch):
    native_home = tmp_path / "native-codex"
    native_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(native_home))
    binary = tmp_path / "fake-codex"
    binary.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""
import json,os,sys,uuid
from pathlib import Path
args=sys.argv[1:]
if args[-2:] == ['login','status']:
    raise SystemExit(0)
prompt=sys.stdin.read()
thread=args[args.index('resume')+1] if 'resume' in args else str(uuid.uuid4())
Path(__file__).with_suffix('.calls').write_text(json.dumps({
    'args':args,'cwd':os.getcwd(),'codex_home':os.getenv('CODEX_HOME'),'prompt':prompt
}))
print(json.dumps({'type':'thread.started','thread_id':thread}),flush=True)
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({'reply':'完成'}))
print(json.dumps({'type':'turn.completed'}),flush=True)
"""
    )
    binary.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        workspace=tmp_path,
        data_dir=tmp_path / ".wechat-assistant",
        owner_id="owner",
        execution_backend="codex_shared",
        codex_binary=str(binary),
    )
    engine = SharedCodexEngine(settings)
    assert engine.readiness() == "ready"
    bound = []
    job = {"job_id": "J000001", "project_directory": str(project), "thread_id": None}
    first = engine.run(job, "问题一", bound.append, lambda _: None, threading.Event())
    record = json.loads(binary.with_suffix(".calls").read_text())
    assert first.text == "完成"
    assert record["cwd"] == str(project)
    assert record["codex_home"] == str(native_home)
    assert "features.memories=true" in record["args"]
    assert "--ignore-user-config" not in record["args"]
    assert "--approve-for-me" in record["args"]
    intent_command = engine.command(
        {**job, "kind": "intent_classification"}, tmp_path / "intent.json",
        tmp_path / "intent-schema.json",
    )
    assert intent_command[intent_command.index("--sandbox") + 1] == "read-only"
    assert "--approve-for-me" not in intent_command
    assert "memories.generate_memories=false" in intent_command
    job["job_id"] = "J000002"
    job["thread_id"] = first.thread_id
    second = engine.run(job, "问题二", bound.append, lambda _: None, threading.Event())
    record = json.loads(binary.with_suffix(".calls").read_text())
    assert second.thread_id == first.thread_id
    assert record["args"][record["args"].index("resume") + 1] == first.thread_id

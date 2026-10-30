import json
import threading
import time

import pytest
from test_clawbot_projects import Transport, event, ready_file, select

from wechat_assistant.config import Settings
from wechat_assistant.projects.bridge import NativeBridge
from wechat_assistant.projects.delivery import OwnerDelivery
from wechat_assistant.projects.execution import ProjectExecution
from wechat_assistant.projects.local_engine import ExecutionUncertain, LocalAnswer
from wechat_assistant.projects.router import ProjectRouter
from wechat_assistant.projects.storage import ProjectStore

P = "11111111-1111-4111-8111-111111111111"
Q = "22222222-2222-4222-8222-222222222222"


class RecordingEngine:
    def __init__(self, state="ready"):
        self.state, self.calls = state, []
        self.gate = None
        self.exception = None
        self.mutate = None

    def readiness(self):
        return self.state

    def subscription_auth(self):
        return True

    def run(self, job, prompt, bind, progress, cancelled):
        if job["kind"] == "intent_classification":
            return LocalAnswer("intent-" + job["job_id"], json.dumps({
                "intent": "ordinary", "kind": None, "time": None, "date": None,
                "weekday": None, "body": None, "clarification": None,
            }))
        self.calls.append((job, prompt))
        session = job["thread_id"] or "local-" + job["task_id"]
        bind(session)
        progress("started")
        if self.gate:
            while not self.gate.wait(0.01):
                if cancelled.is_set():
                    raise ExecutionUncertain("test shutdown")
        if self.exception:
            raise self.exception
        if self.mutate:
            self.mutate()
        return LocalAnswer(session, "本地回答：" + job["text"])


@pytest.fixture
def execution(tmp_path):
    settings = Settings(
        workspace=tmp_path,
        data_dir=tmp_path / ".wechat-assistant",
        owner_id="owner",
        execution_backend="codex_shared",
    )
    store = ProjectStore(settings.data_dir, "owner", 1024 * 1024)
    for pid, name in ((P, "甲项目"), (Q, "乙项目")):
        path = tmp_path / "client-projects" / name
        path.mkdir(parents=True)
        store.register(pid, name, str(path))
    store.set_value("execution_backend", "codex_shared")
    router, bridge, engine = ProjectRouter(store), NativeBridge(store), RecordingEngine()
    worker = ProjectExecution(settings, store, bridge, engine)
    yield settings, store, router, bridge, engine, worker
    worker.stop()
    store.close()


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition timed out")


def drained(worker):
    wait_for(lambda: all(f.done() for f in worker.active.values()))
    worker.tick()


def test_owner_question_runs_without_approval_and_exact_session_is_resumed(execution):
    _, store, router, _, engine, worker = execution
    select(router)
    first = router.ingest(event("我的测试代号是 FIRST-ONLY"))
    worker.tick()
    drained(worker)
    transport = Transport()
    OwnerDelivery(store, transport).flush()
    assert any("FIRST-ONLY" in body for body in transport.sent)
    second = router.ingest(event("沿用刚才的代号"))
    worker.tick()
    drained(worker)
    assert first["task_id"] == second["task_id"]
    assert (
        engine.calls[1][0]["thread_id"] == engine.calls[0][0]["thread_id"]
        or engine.calls[1][0]["thread_id"] == "local-" + first["task_id"]
    )
    assert "FIRST-ONLY" not in engine.calls[1][1]
    assert {r["state"] for r in store.rows("SELECT state FROM jobs")} == {"completed"}


def test_time_mention_can_remain_an_ordinary_question(execution):
    _, store, router, _, engine, worker = execution
    select(router)
    request = router.ingest(event("下午两点的时候我写了什么？"))
    worker.tick()
    drained(worker)
    assert store.get_value("intent_classified:" + request["job_id"]) == "ordinary"
    assert len(engine.calls) == 1
    assert not store.rows("SELECT * FROM local_schedules")


def test_all_projects_execute_in_arrival_order_without_interim_messages(execution):
    _, store, router, _, engine, worker = execution
    engine.gate = threading.Event()
    select(router)
    a = router.ingest(event("A_FIRST"))
    second = router.ingest(event("A_SECOND"))
    select(router, "乙项目")
    b = router.ingest(event("B_FIRST"))
    worker.tick()
    wait_for(lambda: len(engine.calls) == 1)
    worker.tick()
    assert [c[0]["job_id"] for c in engine.calls] == [a["job_id"]]
    engine.gate.set()
    drained(worker)
    drained(worker)
    drained(worker)
    assert len(engine.calls) == 3
    assert [c[0]["job_id"] for c in engine.calls] == [
        a["job_id"], second["job_id"], b["job_id"]
    ]
    assert store.rows("SELECT count(*) AS n FROM jobs WHERE state='completed'")[0]["n"] == 3
    assert not store.rows("SELECT * FROM outbox WHERE dedupe_key LIKE 'progress:%'")
    assert len(store.rows("SELECT * FROM outbox WHERE kind='result'")) == 3


def test_missing_auth_keeps_jobs_queued_then_continues_once_ready(execution):
    _, store, router, _, engine, worker = execution
    select(router)
    task = router.ingest(event("等登录后处理"))
    engine.state = "login_required"
    worker.tick()
    worker.tick()
    assert not engine.calls
    assert store.one("SELECT * FROM jobs WHERE id=?", (task["job_id"],))["state"] == "queued"
    assert not store.rows("SELECT * FROM outbox WHERE dedupe_key LIKE 'engine-wait:%'")
    engine.state = "ready"
    worker.tick()
    drained(worker)
    assert len(engine.calls) == 1


def test_uncertain_execution_is_not_retried(execution):
    _, store, router, _, engine, worker = execution
    select(router)
    task = router.ingest(event("不可重复的模型提交"))
    engine.exception = ExecutionUncertain("test uncertainty")
    worker.tick()
    drained(worker)
    worker.tick()
    assert len(engine.calls) == 1
    assert store.one("SELECT * FROM jobs WHERE id=?", (task["job_id"],))["state"] == "uncertain"
    assert not store.rows("SELECT * FROM outbox WHERE kind='result'")
    notices = store.rows("SELECT dedupe_key FROM outbox WHERE job_id=?", (task["job_id"],))
    assert {row["dedupe_key"] for row in notices} == {
        "started:" + task["job_id"], "failed:" + task["job_id"]
    }


def test_global_queue_order_survives_restart(tmp_path):
    settings = Settings(
        workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant", owner_id="owner"
    )
    store = ProjectStore(settings.data_dir, "owner", 1024 * 1024)
    for pid, name in ((P, "甲项目"), (Q, "乙项目")):
        path = tmp_path / name
        path.mkdir()
        store.register(pid, name, str(path))
    router = ProjectRouter(store)
    select(router)
    first = router.ingest(event("先执行甲"))
    select(router, "乙项目")
    second = router.ingest(event("再执行乙"))
    store.close()

    restored = ProjectStore(settings.data_dir, "owner", 1024 * 1024)
    restored.recover()
    engine = RecordingEngine()
    worker = ProjectExecution(settings, restored, NativeBridge(restored), engine)
    try:
        worker.tick()
        drained(worker)
        drained(worker)
        assert [call[0]["job_id"] for call in engine.calls] == [
            first["job_id"], second["job_id"]
        ]
    finally:
        worker.stop()
        restored.close()


def test_pending_file_blocks_later_message_in_other_project(execution):
    _, store, router, _, engine, worker = execution
    select(router)
    router.ingest(event(files=["资料.txt"]))
    select(router, "乙项目")
    message = router.ingest(event("分析资料"))
    worker.tick()
    assert not engine.calls
    file = store.one("SELECT * FROM files")
    store.save_file(file, b"data")
    worker.tick()
    drained(worker)
    assert engine.calls[0][0]["job_id"] == message["job_id"]


def test_source_mutation_during_model_execution_prevents_result(execution):
    _, store, router, _, engine, worker = execution
    select(router)
    file = ready_file(store, router)
    task = router.ingest(event("根据这份文件回答"))
    engine.mutate = lambda: store.file_path(file).write_bytes(b"changed")
    worker.tick()
    drained(worker)
    assert store.one("SELECT * FROM jobs WHERE id=?", (task["job_id"],))["state"] == "uncertain"
    assert not store.rows("SELECT * FROM outbox WHERE kind='result'")


def test_shutdown_interrupts_running_work_without_sending_a_result(execution):
    _, store, router, _, engine, worker = execution
    select(router)
    router.ingest(event("进行中的任务"))
    engine.gate = threading.Event()
    worker.tick()
    wait_for(lambda: len(engine.calls) == 1)
    worker.stop()
    assert {r["state"] for r in store.rows("SELECT state FROM jobs")} == {"uncertain"}
    assert not store.rows("SELECT * FROM outbox WHERE kind='result'")

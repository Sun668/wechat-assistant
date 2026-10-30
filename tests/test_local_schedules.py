"""Subscription schedules simulated without loading launchd or sending real WeChat."""

import json
import plistlib
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_clawbot_projects import P, Transport, event

from wechat_assistant.projects.api import create_app
from wechat_assistant.projects.bridge import NativeBridge
from wechat_assistant.projects.delivery import OwnerDelivery
from wechat_assistant.projects.execution import ProjectExecution
from wechat_assistant.projects.local_engine import LocalAnswer
from wechat_assistant.projects.local_schedules import (
    LaunchdScheduler,
    LocalScheduleBridge,
    ScheduleUnavailable,
)
from wechat_assistant.projects.router import ProjectRouter
from wechat_assistant.projects.schedule_spec import ScheduleSpec
from wechat_assistant.projects.storage import ProjectStore

BEIJING = ZoneInfo("Asia/Shanghai")


class FakeLauncher:
    def __init__(self):
        self.registrations = []
        self.disabled = []
        self.fail = False

    def ensure(self, plan_id, spec, trigger_token, callback_port):
        self.registrations.append((plan_id, spec))
        if self.fail:
            raise ScheduleUnavailable("launchd unavailable")

    def disable(self, plan_id):
        self.disabled.append(plan_id)


class FakeEngine:
    def readiness(self):
        return "ready"

    def subscription_auth(self):
        return True

    def run(self, job, prompt, bind, progress, cancelled):
        if job["kind"] in ("schedule_request", "intent_classification"):
            assert "用户原文" in prompt
            if "每隔" in job["text"]:
                proposal = {"intent": "create", "kind": "interval", "time": None,
                            "date": None, "weekday": None,
                            "body": "检查项目并汇报进展", "clarification": None}
            elif "1分钟" in job["text"]:
                proposal = {"intent": "create", "kind": "once", "time": "00:00",
                            "date": "2020-01-01", "weekday": None,
                            "body": "搜索最新财经新闻并发给我", "clarification": None}
            else:
                proposal = {"intent": "create", "kind": "daily", "time": "09:00",
                            "date": None, "weekday": None, "body": "检查项目并汇报进展",
                            "clarification": None}
            thread = "intent-" + job["job_id"]
            bind(thread)
            progress("started")
            return LocalAnswer(thread, json.dumps(proposal, ensure_ascii=False))
        thread = "thread-" + job["job_id"]
        bind(thread)
        progress("started")
        assert job["kind"] == "scheduled_message"
        assert "已触发的定时任务" in prompt
        assert "本次定时工作" in prompt
        assert "网关未识别" not in prompt
        assert "本轮是普通消息" not in prompt
        return LocalAnswer(thread, "项目检查完成")


@pytest.fixture
def gateway(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(tmp_path / ".wechat-assistant", "owner", 1024 * 1024)
    store.register(P, "甲项目", str(project))
    settings = SimpleNamespace(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant",
                               codex_binary="codex", execution_backend="codex_shared",
                               )
    launcher = FakeLauncher()
    schedules = LocalScheduleBridge(store, settings, launcher)
    yield store, ProjectRouter(store), NativeBridge(store), schedules, launcher, settings
    store.close()


def create_schedule(gateway):
    store, router, bridge, schedules, _, settings = gateway
    router.ingest(event("选择项目 甲项目"))
    raw = event("帮我每天早上九点检查这个项目并汇报进展")
    router.ingest(raw)
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    return raw, store.one("SELECT * FROM local_schedules")


def test_owner_natural_language_registers_native_subscription_schedule(gateway):
    store, router, _, _, launcher, _ = gateway
    raw, plan = create_schedule(gateway)
    assert plan["source_event"] == raw["message_id"]
    assert plan["project_directory"] == str(store.project_dir(P))
    assert plan["state"] == "active"
    assert launcher.registrations[0][1].calendar() == {"Hour": 9, "Minute": 0}
    assert router.ingest(raw)["status"] == "duplicate"
    assert len(launcher.registrations) == 1


def test_schedule_classification_keeps_existing_conversation_thread(gateway):
    store, router, bridge, schedules, _, settings = gateway
    router.ingest(event("选择项目 甲项目"))
    ordinary = router.ingest(event("先讨论项目"))
    claimed = bridge.claim(P)
    bridge.bind_thread(P, claimed["job_id"], claimed["claim_token"], "existing-thread")
    bridge.complete(P, claimed["job_id"], claimed["claim_token"], "已讨论",
                    thread_id="existing-thread")
    store.set_value("task_engine:" + ordinary["task_id"], "codex_shared")
    scheduled = router.ingest(event("帮我每天早上九点检查这个项目并汇报进展"))
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    assert store.get_value("active_task:" + P) == ordinary["task_id"]
    assert store.one("SELECT task_id FROM jobs WHERE id=?", (scheduled["job_id"],))["task_id"] != ordinary["task_id"]
    assert store.one("SELECT thread_id FROM tasks WHERE id=?", (ordinary["task_id"],))["thread_id"] == "existing-thread"


def test_relative_minute_request_routes_and_registers_once(gateway):
    store, router, bridge, schedules, launcher, settings = gateway
    router.ingest(event("选择项目 甲项目"))
    raw = event("创建个定时任务，1分钟之后再向我发一次财经新闻")
    result = router.ingest(raw)
    assert store.one("SELECT kind FROM jobs WHERE id=?", (result["job_id"],))["kind"] == "message"
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    plan = store.one("SELECT * FROM local_schedules")
    assert plan["kind"] == "once"
    assert plan["state"] == "active"
    assert plan["source_event"] == raw["message_id"]
    assert launcher.registrations[0][1].date == plan["day"]
    assert schedules.fire(plan["id"], datetime.fromisoformat(plan["starts_at"])).startswith("J")
    assert store.one("SELECT state FROM local_schedules")["state"] == "completed"
    assert launcher.disabled == [plan["id"]]


def test_due_slot_runs_once_and_returns_through_owner_outbox(gateway):
    store, _, bridge, schedules, _, settings = gateway
    _, plan = create_schedule(gateway)
    due = datetime(2026, 10, 31, 9, 0, tzinfo=BEIJING)
    job_id = schedules.fire(plan["id"], due)
    assert job_id.startswith("J")
    assert schedules.fire(plan["id"], due) == "duplicate"
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    transport = Transport()
    OwnerDelivery(store, transport).flush()
    assert any("项目检查完成" in text for text in transport.sent)
    assert len(store.rows("SELECT * FROM local_schedule_firings")) == 1


def test_concurrent_launchd_firings_enqueue_one_job(gateway):
    store, _, _, schedules, _, _ = gateway
    _, plan = create_schedule(gateway)
    due = datetime(2026, 10, 31, 9, 0, tzinfo=BEIJING)
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(schedules.fire(plan["id"], due)))
               for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(value.startswith("J") for value in outcomes) == 1
    assert outcomes.count("duplicate") == 4
    assert len(store.rows("SELECT * FROM local_schedule_firings")) == 1


def test_non_owner_and_changed_project_cannot_fire(gateway):
    store, router, _, schedules, _, _ = gateway
    router.ingest(event("选择项目 甲项目"))
    raw = event("每天九点提醒我")
    raw["from_user_id"] = "other"
    assert router.ingest(raw)["status"] == "ignored"
    assert not store.rows("SELECT * FROM local_schedules")
    _, plan = create_schedule(gateway)
    with store.transaction() as db:
        db.execute("UPDATE local_schedules SET project_directory='/wrong' WHERE id=?",
                   (plan["id"],))
    assert schedules.fire(plan["id"], datetime(2026, 10, 31, 9, 0, tzinfo=BEIJING)) == "project_changed"
    assert not store.rows("SELECT * FROM local_schedule_firings")


def test_launcher_failure_is_reported_without_active_plan(gateway):
    store, router, bridge, schedules, launcher, settings = gateway
    launcher.fail = True
    router.ingest(event("选择项目 甲项目"))
    router.ingest(event("每天九点帮我检查项目"))
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    assert store.one("SELECT state FROM local_schedules")["state"] == "failed"
    assert "定时任务未创建" in store.one(
        "SELECT body FROM outbox WHERE kind='result' ORDER BY id DESC LIMIT 1"
    )["body"]


def test_api_key_login_cannot_create_subscription_schedule(gateway):
    store, router, bridge, schedules, launcher, settings = gateway
    router.ingest(event("选择项目 甲项目"))
    router.ingest(event("每天九点帮我检查项目"))
    engine = FakeEngine()
    engine.subscription_auth = lambda: False
    worker = ProjectExecution(settings, store, bridge, engine, schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    assert not launcher.registrations
    assert not store.rows("SELECT * FROM local_schedules")
    assert store.one("SELECT state FROM jobs WHERE kind='message'")["state"] == "queued"
    assert not store.rows(
        "SELECT * FROM outbox WHERE dedupe_key LIKE 'subscription-auth-wait:%'"
    )


def test_restart_restores_active_launchd_registration(gateway):
    store, _, _, schedules, launcher, _ = gateway
    _, plan = create_schedule(gateway)
    store.recover()
    schedules.restore()
    assert len(launcher.registrations) == 2
    assert launcher.registrations[-1][0] == plan["id"]
    assert store.get_value("schedule_launcher_status") == "ready"


def test_once_and_weekday_calendar_validation():
    once = ScheduleSpec("once", "09:00", "检查", "2026-11-01")
    assert once.calendar() == {"Month": 11, "Day": 1, "Hour": 9, "Minute": 0}
    assert once.due_slot(datetime(2026, 11, 1, 9, 0, tzinfo=BEIJING))
    assert once.due_slot(datetime(2027, 11, 1, 9, 0, tzinfo=BEIJING)) is None
    assert ScheduleSpec("weekdays", "09:00", "检查").calendar() == [
        {"Weekday": day, "Hour": 9, "Minute": 0} for day in range(1, 6)
    ]
    proposal = {"intent": "create", "kind": "once", "time": "09:00",
                "date": "2026-10-29", "weekday": None, "body": "检查", "clarification": None}
    with pytest.raises(ValueError, match="未来"):
        ScheduleSpec.from_model(json.dumps(proposal), datetime(2026, 10, 30, tzinfo=timezone.utc))


def test_relative_minute_anchors_to_actual_creation_time():
    proposal = {"intent": "create", "kind": "once", "time": "00:00",
                "date": "2020-01-01", "weekday": None, "body": "发财经新闻", "clarification": None}
    current = datetime(2026, 10, 30, 12, 34, 20, tzinfo=BEIJING)
    spec = ScheduleSpec.from_model(json.dumps(proposal), current,
                                   "创建个定时任务，1分钟之后再向我发一次财经新闻")
    assert (spec.date, spec.clock) == ("2026-10-30", "12:35")
    assert spec.starts_at == "2026-10-30T12:35:20+08:00"
    assert spec.due_slot(datetime(2026, 10, 30, 12, 35, 19, tzinfo=BEIJING)) is None
    assert spec.due_slot(datetime(2026, 10, 30, 12, 35, 20, tzinfo=BEIJING))


@pytest.mark.parametrize(("phrase", "seconds"), [
    ("3天后", 259200), ("两小时后", 7200), ("1天2小时30分钟后", 95400),
    ("过半小时", 1800), ("1.5周后", 907200), ("45秒后", 45),
    ("两个小时后", 7200), ("一个半小时后", 5400), ("两小时半后", 9000),
])
def test_relative_delays_parse_multiple_units(phrase, seconds):
    from wechat_assistant.projects.schedule_spec import relative_delay

    assert relative_delay(phrase).seconds == seconds


def test_month_and_year_relative_dates_and_recurrence():
    from wechat_assistant.projects.schedule_spec import relative_delay

    current = datetime(2026, 1, 31, 9, 0, tzinfo=BEIJING)
    assert relative_delay("1个月后").after(current) == datetime(2026, 2, 28, 9, 0, tzinfo=BEIJING)
    assert relative_delay("一年后").after(current) == datetime(2027, 1, 31, 9, 0, tzinfo=BEIJING)
    proposal = {"intent": "create", "kind": "interval", "time": None,
                "date": None, "weekday": None, "body": "检查", "clarification": None}
    spec = ScheduleSpec.from_model(json.dumps(proposal), current, "每隔1个月检查")
    assert spec.starts_at == "2026-02-28T09:00:00+08:00"
    assert spec.interval_months == 1
    assert spec.due_slot(datetime(2026, 3, 28, 9, 0, tzinfo=BEIJING))


def test_monthly_and_yearly_calendar_cycles():
    monthly = {"intent": "create", "kind": "monthly", "time": "09:30",
               "date": "15", "weekday": None, "body": "汇报", "clarification": None}
    spec = ScheduleSpec.from_model(json.dumps(monthly))
    assert spec.calendar() == {"Day": 15, "Hour": 9, "Minute": 30}
    assert spec.due_slot(datetime(2026, 11, 15, 9, 30, tzinfo=BEIJING))
    yearly = {**monthly, "kind": "yearly", "date": "02-29"}
    spec = ScheduleSpec.from_model(json.dumps(yearly))
    assert spec.calendar() == {"Month": 2, "Day": 29, "Hour": 9, "Minute": 30}
    assert spec.due_slot(datetime(2028, 2, 29, 9, 30, tzinfo=BEIJING))
    assert spec.due_slot(datetime(2027, 3, 1, 9, 30, tzinfo=BEIJING)) is None


def test_interval_schedule_survives_restart_and_dedupes(gateway):
    store, router, bridge, schedules, launcher, settings = gateway
    router.ingest(event("选择项目 甲项目"))
    raw = event("每隔2天3小时检查项目并汇报进展")
    result = router.ingest(raw)
    assert store.one("SELECT kind FROM jobs WHERE id=?", (result["job_id"],))["kind"] == "message"
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    plan = store.one("SELECT * FROM local_schedules")
    assert plan["kind"] == "interval"
    assert plan["interval_seconds"] == 183600
    first = datetime.fromisoformat(plan["starts_at"])
    assert schedules.fire(plan["id"], first).startswith("J")
    assert schedules.fire(plan["id"], first) == "duplicate"
    schedules.restore()
    assert launcher.registrations[-1][1].starts_at == plan["starts_at"]
    second = first + timedelta(seconds=183600)
    assert schedules.fire(plan["id"], second) == "busy"
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    assert schedules.fire(plan["id"], second).startswith("J")
    assert len(store.rows("SELECT * FROM local_schedule_firings")) == 2
    worker = ProjectExecution(settings, store, bridge, FakeEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    transport = Transport()
    OwnerDelivery(store, transport).flush()
    assert any("项目检查完成" in text for text in transport.sent)


def test_interval_first_fire_can_have_separate_delay():
    proposal = {"intent": "create", "kind": "interval", "time": None,
                "date": None, "weekday": None, "body": "检查", "clarification": None}
    current = datetime(2026, 10, 30, 12, 0, tzinfo=BEIJING)
    spec = ScheduleSpec.from_model(json.dumps(proposal), current,
                                   "1小时后开始，每隔3天检查")
    assert spec.starts_at == "2026-10-30T13:00:00+08:00"
    assert spec.interval_seconds == 259200
    assert spec.due_slot(current) is None
    assert spec.due_slot(datetime(2026, 11, 2, 13, 0, tzinfo=BEIJING))


def test_colloquial_half_hour_interval_routes(gateway):
    store, router, _, _, _, _ = gateway
    router.ingest(event("选择项目 甲项目"))
    result = router.ingest(event("每隔一个半小时检查一次项目"))
    assert store.one("SELECT kind FROM jobs WHERE id=?", (result["job_id"],))["kind"] == "message"
    proposal = {"intent": "create", "kind": "interval", "time": None,
                "date": None, "weekday": None, "body": "检查项目", "clarification": None}
    spec = ScheduleSpec.from_model(json.dumps(proposal),
                                   datetime(2026, 10, 30, 12, 0, tzinfo=BEIJING),
                                   "每隔一个半小时检查一次项目")
    assert spec.interval_seconds == 5400


def test_afternoon_clock_request_uses_schedule_route(gateway):
    store, router, _, _, _, _ = gateway
    router.ingest(event("选择项目 甲项目"))
    result = router.ingest(event("下午两点的时候，随便给我发一个消息"))
    assert store.one("SELECT kind FROM jobs WHERE id=?", (result["job_id"],))["kind"] == "message"


def test_model_can_create_schedule_without_a_gateway_phrase_case(gateway):
    store, router, bridge, schedules, _, settings = gateway
    router.ingest(event("选择项目 甲项目"))
    raw = event("明天下午两点的时候，随便给我发一个消息")
    routed = router.ingest(raw)

    class ClockEngine(FakeEngine):
        def run(self, job, prompt, bind, progress, cancelled):
            if job["kind"] == "intent_classification":
                day = (datetime.now(BEIJING) + timedelta(days=1)).date().isoformat()
                proposal = {"intent": "create", "kind": "once", "time": "14:00",
                            "date": day, "weekday": None, "body": "随便发一个消息",
                            "clarification": None}
                return LocalAnswer("intent-" + job["job_id"], json.dumps(proposal))
            return super().run(job, prompt, bind, progress, cancelled)

    worker = ProjectExecution(settings, store, bridge, ClockEngine(), schedules)
    worker._execute(bridge.claim(P))
    worker.stop()
    plan = store.one("SELECT * FROM local_schedules")
    assert plan["kind"] == "once" and plan["clock"] == "14:00"
    assert plan["source_event"] == raw["message_id"]
    assert store.get_value("intent_classified:" + routed["job_id"]) == "create"


def test_launchd_adapter_writes_workspace_plist_and_bootstraps(monkeypatch, tmp_path):
    import wechat_assistant.projects.local_schedules as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    settings = SimpleNamespace(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant")
    launcher = LaunchdScheduler(settings)
    calls = []

    def command(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] == "print" else 0)

    monkeypatch.setattr(launcher, "_command", command)
    launcher.ensure("sch_abcdef123456", ScheduleSpec("daily", "09:00", "检查"),
                    "test-scope-token", 8765)
    path = launcher.root / "sch_abcdef123456.plist"
    config = plistlib.loads(path.read_bytes())
    assert config["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    assert config["ProgramArguments"][-1] == "http://127.0.0.1:8765/schedule-fire/sch_abcdef123456"
    assert config["ProgramArguments"][0] == "/usr/bin/curl"
    assert config["StandardErrorPath"].startswith(str(settings.data_dir))
    assert [call[0] for call in calls] == ["print", "bootstrap"]


def test_launchd_relative_adapter_uses_interval_and_unloads(monkeypatch, tmp_path):
    import wechat_assistant.projects.local_schedules as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    settings = SimpleNamespace(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant")
    launcher = LaunchdScheduler(settings)
    calls = []

    def command(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0 if args[0] != "print" else
                                           (1 if "bootstrap" not in [x[0] for x in calls] else 0))

    monkeypatch.setattr(launcher, "_command", command)
    start = datetime.now(BEIJING) + timedelta(days=2)
    spec = ScheduleSpec("interval", start.strftime("%H:%M"), "检查", starts_at=start.isoformat(),
                        interval_seconds=7200)
    launcher.ensure("sch_abcdef123456", spec, "test-scope-token", 8765)
    config = plistlib.loads((launcher.root / "sch_abcdef123456.plist").read_bytes())
    assert config["StartInterval"] == 60
    assert "StartCalendarInterval" not in config
    launcher.disable("sch_abcdef123456")
    assert any(call[0] == "bootout" for call in calls)
    old_start = datetime.now(BEIJING) - timedelta(days=2)
    old_spec = ScheduleSpec("interval", old_start.strftime("%H:%M"), "检查",
                            starts_at=old_start.isoformat(), interval_seconds=7200)
    launcher.ensure("sch_abcdef123457", old_spec, "test-scope-token", 8765)
    old_config = plistlib.loads((launcher.root / "sch_abcdef123457.plist").read_bytes())
    assert old_config["StartInterval"] == 60


def test_launchd_replaces_legacy_python_trigger(monkeypatch, tmp_path):
    import wechat_assistant.projects.local_schedules as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    settings = SimpleNamespace(workspace=tmp_path, data_dir=tmp_path / ".wechat-assistant")
    launcher = LaunchdScheduler(settings)
    plan_id = "sch_abcdef123456"
    path = launcher.root / (plan_id + ".plist")
    path.write_bytes(plistlib.dumps({"Label": launcher.label(plan_id),
                                    "ProgramArguments": ["python", "-m", "old"]}))
    calls = []

    def command(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launcher, "_command", command)
    launcher.ensure(plan_id, ScheduleSpec("daily", "09:00", "检查"), "scope-token", 8765)
    assert [call[0] for call in calls] == ["print", "bootout", "bootstrap"]
    config = plistlib.loads(path.read_bytes())
    assert config["ProgramArguments"][0] == "/usr/bin/curl"


def test_scoped_http_trigger_requires_plan_token(gateway):
    store, _, _, schedules, _, _ = gateway
    _, plan = create_schedule(gateway)
    client = TestClient(create_app(SimpleNamespace(schedules=schedules), background=False))
    path = "/schedule-fire/" + plan["id"]
    assert client.post(path).status_code == 401
    assert client.post(path, headers={"X-Schedule-Token": "wrong"}).status_code == 401
    assert client.post(path, headers={"X-Schedule-Token": plan["trigger_token"]}).status_code == 200
    due = datetime(2026, 10, 31, 9, 0, tzinfo=BEIJING)
    # The real HTTP callback uses the current clock; check the scoped token
    # with an explicit clock through the pure scheduling boundary.
    assert schedules.fire_authorized(plan["id"], plan["trigger_token"], due).startswith("J")
    assert len(store.rows("SELECT * FROM local_schedule_firings")) == 1


def test_existing_schedule_table_gains_relative_columns(tmp_path):
    root = tmp_path / ".wechat-assistant" / "clawbot-projects"
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "gateway.sqlite")
    db.execute("""CREATE TABLE local_schedules (
        id TEXT PRIMARY KEY,project_id TEXT,source_event TEXT,request_job TEXT,
        body TEXT,schedule_text TEXT,project_directory TEXT,kind TEXT,clock TEXT,
        day TEXT,weekday INTEGER,state TEXT,created_at TEXT)""")
    db.close()
    store = ProjectStore(tmp_path / ".wechat-assistant", "owner", 1024 * 1024)
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(local_schedules)")}
    assert {"starts_at", "interval_seconds", "interval_months"} <= columns
    store.close()

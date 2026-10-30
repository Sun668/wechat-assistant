"""macOS launchd schedules that enqueue owner-authorized native Codex jobs."""

import os
import plistlib
import secrets
import subprocess
import sys
from datetime import datetime
from math import ceil

from ..workspace_io import utc_now
from .schedule_spec import ZONE, ScheduleSpec
from .storage import directory


class ScheduleUnavailable(RuntimeError):
    """The launchd schedule was not registered."""


class ScheduleUncertain(RuntimeError):
    """launchd registration needs operator reconciliation."""


class LaunchdScheduler:
    def __init__(self, settings):
        self.settings = settings
        self.root = directory(settings.data_dir / "clawbot-projects" / "schedules")

    @staticmethod
    def label(plan_id):
        if not plan_id.startswith("sch_") or not all(c in "0123456789abcdef" for c in plan_id[4:]):
            raise ValueError("定时任务 ID 无效")
        return "com.openai.clawbot.schedule." + plan_id[4:]

    def _command(self, *args):
        try:
            return subprocess.run(["launchctl", *args], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScheduleUncertain("launchd 状态无法确认") from exc

    def ensure(self, plan_id, spec, trigger_token, callback_port):
        if sys.platform != "darwin":
            raise ScheduleUnavailable("订阅定时任务需要 macOS launchd")
        if not trigger_token or not 1 <= callback_port <= 65535:
            raise ScheduleUnavailable("定时回调配置无效")
        label = self.label(plan_id)
        domain = f"gui/{os.getuid()}"
        path = self.root / (plan_id + ".plist")
        output_log = self.root / (plan_id + ".out.log")
        error_log = self.root / (plan_id + ".err.log")
        if any(item.is_symlink() for item in (path, output_log, error_log)):
            raise ScheduleUnavailable("计划配置或日志不能是符号链接")
        config = {
            "Label": label,
            "ProgramArguments": [
                "/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "15",
                "--request", "POST", "--header", "X-Schedule-Token: " + trigger_token,
                f"http://127.0.0.1:{callback_port}/schedule-fire/{plan_id}",
            ],
            "WorkingDirectory": str(self.settings.workspace),
            "RunAtLoad": False,
            "StandardOutPath": str(output_log),
            "StandardErrorPath": str(error_log),
        }
        if spec.starts_at is not None:
            remaining = (datetime.fromisoformat(spec.starts_at) - datetime.now(ZONE)).total_seconds()
            config["StartInterval"] = (
                60 if spec.kind == "interval" else max(1, min(60, ceil(remaining)))
            )
        else:
            config["StartCalendarInterval"] = spec.calendar()
        current = plistlib.loads(path.read_bytes()) if path.exists() else None
        path.write_bytes(plistlib.dumps(config))
        path.chmod(0o600)
        if self._command("print", domain + "/" + label).returncode == 0:
            if current == config:
                return
            removed = self._command("bootout", domain + "/" + label)
            if removed.returncode != 0 and self._command("print", domain + "/" + label).returncode == 0:
                raise ScheduleUncertain("旧版定时触发器未能卸载")
        result = self._command("bootstrap", domain, str(path))
        if result.returncode != 0 and self._command("print", domain + "/" + label).returncode != 0:
            raise ScheduleUnavailable("launchd 未接受定时任务，请检查当前用户图形会话")

    def disable(self, plan_id):
        target = f"gui/{os.getuid()}/{self.label(plan_id)}"
        if self._command("print", target).returncode == 0:
            result = self._command("bootout", target)
            if result.returncode != 0 and self._command("print", target).returncode == 0:
                raise ScheduleUncertain("一次性任务已入队，但 launchd 触发器未能卸载")


class LocalScheduleBridge:
    def __init__(self, store, settings, launcher=None, callback_port=8765):
        self.store = store
        self.launcher = launcher or LaunchdScheduler(settings)
        self.callback_port = callback_port

    def register(self, spec, project_id, source_event, request_job):
        if not self.store.is_owner_source_event(source_event):
            raise ValueError("定时任务缺少绑定人原始授权")
        directory_path = str(self.store.codex_project_dir(project_id))
        plan_id = "sch_" + secrets.token_hex(12)
        trigger_token = secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            db.execute(
                """INSERT INTO local_schedules
                (id,project_id,source_event,request_job,body,schedule_text,
                 project_directory,kind,clock,day,weekday,state,created_at,
                 starts_at,interval_seconds,interval_months,trigger_token)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'installing',?,?,?,?,?)""",
                (plan_id, project_id, source_event, request_job, spec.body,
                 spec.description(), directory_path, spec.kind, spec.clock,
                 spec.date, spec.weekday, utc_now(), spec.starts_at,
                 spec.interval_seconds, spec.interval_months, trigger_token),
            )
        try:
            self.launcher.ensure(plan_id, spec, trigger_token, self.callback_port)
        except ScheduleUnavailable:
            with self.store.transaction() as db:
                db.execute("UPDATE local_schedules SET state='failed' WHERE id=?", (plan_id,))
            raise
        except Exception:
            with self.store.transaction() as db:
                db.execute("UPDATE local_schedules SET state='uncertain' WHERE id=?", (plan_id,))
            raise
        with self.store.transaction() as db:
            db.execute("UPDATE local_schedules SET state='active' WHERE id=?", (plan_id,))
            self.store.audit("local_schedule_created", {
                "id": plan_id, "project_id": project_id,
                "source_event": source_event, "request_job": request_job,
            })
        return plan_id

    def restore(self):
        """Register saved plans after service startup, without firing them."""
        failed = 0
        if hasattr(self.launcher, "disable"):
            for row in self.store.rows("SELECT id FROM local_schedules WHERE state='completed'"):
                try:
                    self.launcher.disable(row["id"])
                except (ScheduleUncertain, OSError):
                    self.store.audit("local_schedule_disable_uncertain", {"id": row["id"]})
        for row in self.store.rows("SELECT * FROM local_schedules WHERE state='active'"):
            try:
                token = row["trigger_token"] or secrets.token_urlsafe(32)
                if not row["trigger_token"]:
                    with self.store.transaction() as db:
                        db.execute("UPDATE local_schedules SET trigger_token=? WHERE id=?",
                                   (token, row["id"]))
                self.launcher.ensure(row["id"], self._spec(row), token, self.callback_port)
            except (ScheduleUnavailable, ScheduleUncertain, OSError):
                failed += 1
                if self.store.is_owner_source_event(row["source_event"]):
                    with self.store.transaction():
                        self.store.enqueue(
                            "schedule-launcher-failed:" + row["id"],
                            f"【{self.store.project(row['project_id'])['name']}】"
                            f"定时任务 {row['id']} 未能重新注册到本机 launchd；请检查服务状态。",
                            row["source_event"], row["project_id"],
                        )
        self.store.set_value("schedule_launcher_status", "ready" if not failed else f"failed:{failed}")

    @staticmethod
    def _spec(row):
        return ScheduleSpec(row["kind"], row["clock"], row["body"],
                            row["day"], row["weekday"], row["starts_at"],
                            row["interval_seconds"], row["interval_months"])

    def fire_authorized(self, plan_id, trigger_token, current=None):
        row = self.store.one("SELECT trigger_token FROM local_schedules WHERE id=?", (plan_id,))
        if (not isinstance(trigger_token, str) or not row["trigger_token"]
                or not secrets.compare_digest(trigger_token, row["trigger_token"])):
            raise ValueError("定时触发器凭据无效")
        return self.fire(plan_id, current)

    def fire(self, plan_id, current=None):
        """Called by launchd; enqueue once for an exact due slot, never send WeChat."""
        current = (current or datetime.now(ZONE)).astimezone(ZONE)
        completed = False
        with self.store.transaction() as db:
            row = self.store.one("SELECT * FROM local_schedules WHERE id=?", (plan_id,))
            if row["state"] != "active" or not self.store.is_owner_source_event(row["source_event"]):
                return "inactive"
            try:
                if str(self.store.codex_project_dir(row["project_id"])) != row["project_directory"]:
                    return "project_changed"
            except ValueError:
                return "project_changed"
            slot = self._spec(row).due_slot(current)
            if slot is None:
                return "not_due"
            firing_id = plan_id + ":" + slot.isoformat()
            if self.store.rows("SELECT 1 FROM local_schedule_firings WHERE id=?", (firing_id,)):
                return "duplicate"
            if row["kind"] == "interval" and self.store.rows(
                """SELECT 1 FROM local_schedule_firings f
                   JOIN jobs j ON j.id=f.job_id
                   WHERE f.schedule_id=? AND j.state!='completed' LIMIT 1""",
                (plan_id,),
            ):
                return "busy"
            task_id = self.store.new_task(row["project_id"])
            job_id = self.store.new_job(row["project_id"], row["source_event"],
                                        "scheduled_message", row["body"], task_id)
            db.execute("INSERT INTO local_schedule_firings VALUES(?,?,?,?,?)",
                       (firing_id, plan_id, job_id, slot.isoformat(), utc_now()))
            self.store.enqueue("schedule-fired:" + firing_id,
                               f"【{self.store.project(row['project_id'])['name']}】"
                               f"定时任务 {plan_id} 已触发，正在排队执行（{job_id}）。",
                               row["source_event"], row["project_id"], task_id, job_id)
            if row["kind"] == "once":
                db.execute("UPDATE local_schedules SET state='completed' WHERE id=?", (plan_id,))
                completed = True
        if completed and hasattr(self.launcher, "disable"):
            try:
                self.launcher.disable(plan_id)
            except (ScheduleUncertain, OSError):
                self.store.audit("local_schedule_disable_uncertain", {"id": plan_id})
        return job_id

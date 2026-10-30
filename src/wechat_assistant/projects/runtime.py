"""ClawBot composition root with independent receive, execute and delivery loops."""

import asyncio
import json
import logging
import secrets

from ..adapters.ilink import ILink
from ..models import NotSent
from .bridge import NativeBridge
from .delivery import OwnerDelivery
from .local_schedules import LocalScheduleBridge
from .router import ProjectRouter
from .storage import ProjectStore

log = logging.getLogger(__name__)


class ProjectRuntime:
    def __init__(self, settings, transport=None, engine=None, port=8765):
        self.settings = settings
        if settings.execution_backend != "codex_shared":
            raise ValueError("微信助理仅支持 codex_shared 执行入口")
        if settings.data_dir != settings.workspace.resolve() / ".wechat-assistant":
            raise ValueError("所有运行数据必须归属于指定工作区的 .wechat-assistant")
        if not settings.owner_id or settings.owner_id == "self":
            raise ValueError("请先绑定 ClawBot，并将 owner_id 配置为绑定人")
        self.store = ProjectStore(settings.data_dir, settings.owner_id, settings.file_max_bytes)
        self.router, self.bridge = ProjectRouter(self.store), NativeBridge(self.store)
        self.schedules = LocalScheduleBridge(self.store, settings, callback_port=port)
        from .execution import ProjectExecution
        from .local_engine import create_engine

        self.execution = ProjectExecution(
            settings, self.store, self.bridge, engine or create_engine(settings), self.schedules
        )
        self.ilink = None
        if transport is None:
            credential = settings.data_dir / "credentials" / "ilink.json"
            if not credential.is_file() or credential.is_symlink():
                raise ValueError("请先使用 ilink-login 在当前工作区绑定 ClawBot")
            self.ilink = ILink(
                credential, self.store, settings.owner_id, event_sink=self.router.ingest
            )
            from ..adapters.ilink_media import ILinkMedia

            self.ilink.media = ILinkMedia(self.ilink._post, settings.file_max_bytes)
        self.transport = transport or self.ilink
        self.delivery = OwnerDelivery(self.store, self.transport)
        token_path = self.store.root / "operator-token"
        if token_path.is_symlink():
            raise ValueError("访问令牌不能是符号链接")
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(32))
            token_path.chmod(0o600)
        self.api_token = token_path.read_text().strip()
        self.running, self.tasks, self.lock_file = False, [], None

    async def start(self):
        import fcntl

        lock_path = self.settings.data_dir / "runtime.lock"
        if lock_path.is_symlink():
            raise ValueError("工作区锁不能是符号链接")
        self.lock_file = lock_path.open("a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError("工作区已有实例；请先停止旧服务") from None
        self.store.recover()
        await asyncio.to_thread(self.schedules.restore)
        self.store.set_value("execution_backend", self.settings.execution_backend)
        # Keep the owner's current project across restarts if its native
        # project directory is still the registered primary directory.
        active_project = self.store.get_value("active_project")
        if active_project:
            try:
                self.store.codex_project_dir(active_project)
            except (ValueError, OSError):
                self.store.set_value("active_project", "")
                self.store.audit(
                    "active_project_unavailable_on_restart", {"project_id": active_project}
                )
        self.running = True
        self.tasks = [
            asyncio.create_task(self._loop(self.delivery.flush, 0.25)),
            asyncio.create_task(self._loop(self.download_next, 0.25)),
        ]
        if self.ilink:
            self.tasks.append(asyncio.create_task(self._loop(self.collect, 1)))
        self.tasks.append(asyncio.create_task(self._loop(self.execution.tick, 0.5)))
        log.info("ClawBot 项目入口已启动；联系人监听、代回复和本地模型已停用")
        log.info("本地执行入口：%s", self.settings.execution_backend)

    async def _loop(self, operation, seconds):
        while self.running:
            try:
                await asyncio.to_thread(operation)
            except Exception as exc:
                # Raw network payloads, context tokens and file URLs stay private.
                log.warning("ClawBot 后台操作未完成：%s", type(exc).__name__)
            await asyncio.sleep(seconds)

    def collect(self):
        try:
            self.ilink.poll()
            self.ilink.acknowledge_poll()
            self.store.set_value("collector_status", "ok")
        except Exception as exc:
            self.store.set_value("collector_status", "error:" + type(exc).__name__)
            raise

    def download_next(self):
        if not self.ilink or not self.ilink.media:
            return False
        with self.store.transaction() as db:
            ahead = self.store.rows(
                "SELECT * FROM jobs WHERE state!='completed' ORDER BY seq LIMIT 1"
            )
            if not ahead or ahead[0]["kind"] != "file_receive":
                return False
            rows = self.store.rows(
                "SELECT * FROM files WHERE id=? AND state='pending'", (ahead[0]["file_id"],)
            )
            if not rows:
                return False
            file = rows[0]
            db.execute("UPDATE files SET state='downloading' WHERE id=?", (file["id"],))
        try:
            self.delivery.flush()
            content = self.ilink.media.download(json.loads(file["media"]))
            self.store.save_file(file, content)
            body = f"【{self.store.project(file['project_id'])['name']}】《{file['name']}》已保存到项目目录。\n{self.store.file_path(file)}"
        except Exception:
            with self.store.transaction() as db:
                db.execute("UPDATE files SET state='failed' WHERE id=?", (file["id"],))
            body = f"【{self.store.project(file['project_id'])['name']}】《{file['name']}》处理失败：下载未完成，队列已暂停；可发送“重试文件《{file['name']}》”。"
        with self.store.transaction():
            self.store.enqueue(
                "download:" + file["id"],
                body,
                file["source_key"].rsplit(":", 1)[0],
                file["project_id"],
            )
        return True

    def health(self):
        counts = self.store.rows("SELECT state,count(*) AS count FROM jobs GROUP BY state")
        return {
            "mode": "clawbot_projects",
            "running": self.running,
            "wechat_listener": False,
            "third_party_replies": False,
            "model_backend": self.settings.execution_backend,
            "native_executor": "codex_shared_local",
            "authentication": "native_codex_login",
            "schedule_launcher_status": self.store.get_value("schedule_launcher_status", "not_checked"),
            "schedule_counts": self.store.rows(
                "SELECT state,count(*) AS count FROM local_schedules GROUP BY state"
            ),
            "execution_readiness": self.store.get_value("execution_readiness", "not_checked"),
            "execution_last_completed_job": self.store.get_value("execution_last_completed_job"),
            "project_context": "codex_native_thread_and_host_memories",
            "wechat_storage_role": "transport_with_codex_bridge",
            "cloud_project_memory": "not_used",
            "file_storage": "local_project_directory",
            "cloud_file_upload_required": False,
            "execution_environment": "local",
            "model_inference": "openai_cloud",
            "cloud_source_upload": False,
            "project_count": len([p for p in self.store.projects() if p.get("source_path")]),
            "jobs": counts,
            "collector": self.store.get_value("collector_status", "waiting"),
        }

    def reconcile_outbox(self, outbox_id, outcome):
        if outcome not in ("sent", "not_sent"):
            raise ValueError("需要人工核对为 sent 或 not_sent")
        with self.store.transaction() as db:
            row = self.store.one("SELECT * FROM outbox WHERE id=?", (outbox_id,))
            if row["state"] not in ("uncertain", "failed"):
                raise ValueError("只有发送不明或失败记录可以核对")
            state = "sent" if outcome == "sent" else "pending"
            db.execute("UPDATE outbox SET state=? WHERE id=?", (state, outbox_id))
            self.store.audit("owner_reconciled_send", {"id": outbox_id, "outcome": outcome})

    async def stop(self):
        self.running = False
        try:
            await asyncio.to_thread(self.execution.stop)
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.ilink:
                self.ilink.close()
            self.store.close()
        finally:
            if self.lock_file:
                self.lock_file.close()

    def assert_owner_destination(self, owner_id):
        if owner_id != self.store.owner_id:
            raise NotSent("ClawBot 只向绑定的操作者返回结果")

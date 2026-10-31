"""Project routing and local project files; no cloud file-upload queue."""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..workspace_io import read_regular, safe_name
from ..workspace_io import utc_now as now
from .project_paths import is_excluded_project_path


def directory(path: Path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("项目状态目录不能包含符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class ProjectStore:
    def __init__(self, data_dir: Path, owner_id: str, max_bytes: int, upload_config=None):
        self.root = directory(data_dir / "clawbot-projects")
        self.owner_id, self.max_bytes = owner_id, max_bytes
        self.upload_config = upload_config
        self.lock = threading.RLock()
        path = self.root / "gateway.sqlite"
        if path.is_symlink():
            raise ValueError("项目数据库不能是符号链接")
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_events(
          channel TEXT,external_id TEXT,raw TEXT,received_at TEXT,
          PRIMARY KEY(channel,external_id));
        CREATE TABLE IF NOT EXISTS projects(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,folder TEXT,source_path TEXT,
          upload_path TEXT,name_override TEXT);
        CREATE TABLE IF NOT EXISTS ingress(
          external_id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id),created_at TEXT);
        CREATE TABLE IF NOT EXISTS tasks(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,
          project_id TEXT NOT NULL REFERENCES projects(id),thread_id TEXT UNIQUE,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
          source_key TEXT UNIQUE,name TEXT NOT NULL,state TEXT NOT NULL,media TEXT,
          size INTEGER,sha256 TEXT,created_at TEXT NOT NULL,relative_path TEXT,storage_root TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,
          project_id TEXT NOT NULL REFERENCES projects(id),task_id TEXT REFERENCES tasks(id),
          source_event TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',
          file_id TEXT REFERENCES files(id),state TEXT NOT NULL DEFAULT 'queued',
          claim_token TEXT,error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY,dedupe_key TEXT UNIQUE,project_id TEXT REFERENCES projects(id),
          task_id TEXT REFERENCES tasks(id),job_id TEXT REFERENCES jobs(id),kind TEXT NOT NULL,
          destination TEXT NOT NULL,body TEXT NOT NULL,marker TEXT NOT NULL,
          source_event TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',external_id TEXT,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_files(
          job_id TEXT REFERENCES jobs(id),file_id TEXT REFERENCES files(id),
          sha256 TEXT NOT NULL,PRIMARY KEY(job_id,file_id));
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY,action TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT);
        CREATE TABLE IF NOT EXISTS local_schedules(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
          source_event TEXT NOT NULL UNIQUE,request_job TEXT NOT NULL REFERENCES jobs(id),
          body TEXT NOT NULL,schedule_text TEXT NOT NULL,project_directory TEXT NOT NULL,
          kind TEXT NOT NULL,clock TEXT NOT NULL,day TEXT,weekday INTEGER,
          state TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL,
          starts_at TEXT,interval_seconds INTEGER,interval_months INTEGER,
          trigger_token TEXT);
        CREATE TABLE IF NOT EXISTS local_schedule_firings(
          id TEXT PRIMARY KEY,schedule_id TEXT NOT NULL REFERENCES local_schedules(id),
          job_id TEXT NOT NULL REFERENCES jobs(id),due_at TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
        for table, column in (
            ("projects", "folder"),
            ("projects", "source_path"),
            ("projects", "upload_path"),
            ("projects", "name_override"),
            ("files", "relative_path"),
            ("files", "storage_root"),
            ("local_schedules", "starts_at"),
            ("local_schedules", "interval_seconds"),
            ("local_schedules", "interval_months"),
            ("local_schedules", "trigger_token"),
        ):
            if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        with self.transaction() as db:
            for project in self.projects():
                if not project["folder"]:
                    db.execute(
                        "UPDATE projects SET folder=? WHERE id=?",
                        (self.folder_name(project["id"], project["name"]), project["id"]),
                    )

    @contextmanager
    def transaction(self):
        with self.lock:
            nested = self.db.in_transaction
            savepoint = "sp_" + secrets.token_hex(8)
            self.db.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                if nested:
                    self.db.execute(f"ROLLBACK TO {savepoint}")
                    self.db.execute(f"RELEASE {savepoint}")
                else:
                    self.db.rollback()
                raise
            else:
                self.db.execute(f"RELEASE {savepoint}") if nested else self.db.commit()

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        if not rows:
            raise ValueError("记录不存在或不属于指定项目")
        return rows[0]

    def get_value(self, key, default=""):
        rows = self.rows("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_value(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, value))

    def remember_source_event(self, channel, external_id, raw):
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO source_events VALUES(?,?,?,?)",
                (channel, external_id, json.dumps(raw, ensure_ascii=False), now()),
            )

    def is_owner_source_event(self, external_id):
        rows = self.rows(
            "SELECT raw FROM source_events WHERE channel='ilink' AND external_id=?",
            (external_id,),
        )
        if len(rows) != 1:
            return False
        try:
            raw = json.loads(rows[0]["raw"])
        except (TypeError, ValueError):
            return False
        return (
            isinstance(raw, dict)
            and raw.get("from_user_id") == self.owner_id
            and not raw.get("group_id")
            and str(raw.get("message_id")) == external_id
        )

    def audit(self, action, detail):
        self.db.execute(
            "INSERT INTO audit(action,detail,created_at) VALUES(?,?,?)",
            (action, json.dumps(detail, ensure_ascii=False), now()),
        )

    def register(self, project_id, name, source_path=None, upload_path=None):
        import re
        import uuid

        native_id = bool(re.fullmatch(r"g-p-[A-Za-z0-9-]{8,100}", project_id))
        if not native_id:
            try:
                uuid.UUID(project_id)
            except ValueError:
                raise ValueError("需要客户端返回的真实项目 ID") from None
            if not source_path:
                raise ValueError("本地项目必须提供客户端登记的资料目录")
        if source_path is not None:
            if is_excluded_project_path(source_path):
                raise ValueError("项目主目录属于已排除的 ChatGPT 目录，请选择其他本地项目")
            path = Path(source_path).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("本地项目资料目录不存在")
            source_path = str(path)
        if upload_path is not None:
            path = Path(upload_path).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("文件接收目录不存在")
            upload_path = str(path)
        for value in (source_path, upload_path):
            if value:
                self.validate_project_root(project_id, Path(value))
        previous = self.rows("SELECT source_path FROM projects WHERE id=?", (project_id,))
        if (
            previous
            and source_path
            and previous[0]["source_path"] != source_path
            and self.rows("SELECT 1 FROM tasks WHERE project_id=? LIMIT 1", (project_id,))
        ):
            raise ValueError("项目已有会话，不能更换 Codex 主目录；请先核对会话归属")
        if not name.strip() or len(name) > 100 or any(c in name for c in "\n\r[]"):
            raise ValueError("项目名称无效")
        with self.transaction() as db:
            db.execute(
                """INSERT INTO projects(id,name,created_at,folder,source_path,upload_path) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=COALESCE(projects.name_override,excluded.name),
                source_path=COALESCE(excluded.source_path,projects.source_path),
                upload_path=COALESCE(excluded.upload_path,projects.upload_path)""",
                (
                    project_id,
                    name.strip(),
                    now(),
                    self.folder_name(project_id, name.strip()),
                    source_path,
                    upload_path,
                ),
            )
            catalog = self.get_value("project_catalog_ids", None)
            if catalog is not None:
                ids = json.loads(catalog)
                if project_id not in ids:
                    self.set_value("project_catalog_ids", json.dumps(ids + [project_id]))
        if self.project(project_id).get("source_path"):
            self.project_dir(project_id)

    def validate_project_root(self, project_id, path):
        state = self.root.parent
        if path == state or path in state.parents or state in path.parents:
            raise ValueError("项目资料目录不能包含运行状态，也不能位于运行状态目录内")
        for other in self.rows(
            "SELECT source_path,upload_path FROM projects WHERE id!=?", (project_id,)
        ):
            for value in (other["source_path"], other["upload_path"]):
                if value:
                    root = Path(value)
                    if path == root or path in root.parents or root in path.parents:
                        raise ValueError("不同项目的本地目录不能重叠")

    def rename(self, project_id, name):
        self.project(project_id)
        if not name.strip() or len(name) > 100 or any(c in name for c in "\n\r[]"):
            raise ValueError("项目名称无效")
        with self.transaction() as db:
            db.execute(
                "UPDATE projects SET name=?,name_override=? WHERE id=?",
                (name.strip(), name.strip(), project_id),
            )

    @staticmethod
    def folder_name(project_id, name):
        name = safe_name(name)
        while len(name.encode()) > 180:
            name = name[:-1]
        return name + "--" + hashlib.sha256(project_id.encode()).hexdigest()[:12]

    def managed_project_dir(self, project_id):
        project = self.project(project_id)
        folder = project["folder"]
        if not folder or safe_name(folder) != folder:
            raise ValueError("项目目录名无效")
        return directory(self.root / "projects" / folder)

    def project_dir(self, project_id):
        return self.codex_project_dir(project_id)

    def receive_directory(self, project_id):
        primary = self.codex_project_dir(project_id)
        return (self.upload_config.receive_directory() or primary) if self.upload_config else primary

    def upload_path(self):
        return self.upload_config.get() if self.upload_config else ""

    def set_upload_path(self, value):
        if self.upload_config is None:
            raise ValueError("上传路径配置入口未启用")
        return self.upload_config.set(value)

    def file_location_label(self, row):
        project = self.project(row["project_id"])
        return (
            "接收目录" if row.get("storage_root")
            and row["storage_root"] != project.get("source_path") else "项目目录"
        )

    def codex_project_dir(self, project_id):
        """Use the client's first (primary) folder for local Codex work."""
        import uuid

        try:
            uuid.UUID(project_id)
        except ValueError:
            raise ValueError("Codex 执行需要客户端登记的本地项目 ID") from None
        project = self.project(project_id)
        source = project.get("source_path")
        if not source:
            raise ValueError("项目缺少客户端登记的第一个本地目录")
        if is_excluded_project_path(source):
            raise ValueError("项目主目录属于已排除的 ChatGPT 目录，请选择其他本地项目")
        path = Path(source)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Codex 项目主目录暂不可用")
        if str(path.resolve(strict=True)) != source:
            raise ValueError("Codex 项目主目录已变化，请重新导入客户端项目")
        self.validate_project_root(project_id, path)
        return directory(path)

    def available_name(self, project_id, value, exclude_id=None, storage_root=None):
        name = safe_name(value)
        folder = storage_root if storage_root is not None else self.receive_directory(project_id)
        existing = {
            row["name"].casefold()
            for row in self.rows(
                "SELECT id,name FROM files WHERE project_id=? OR storage_root=?",
                (project_id, str(folder)),
            )
            if row["id"] != exclude_id
        }
        candidate = name
        stem, suffix = Path(name).stem, Path(name).suffix
        while len(stem.encode()) > 180:
            stem = stem[:-1]
        counter = 1
        while (
            candidate.casefold() in existing
            or (folder / candidate).exists()
            or (folder / candidate).is_symlink()
        ):
            counter += 1
            candidate = f"{stem} ({counter}){suffix}"
        return candidate

    def projects(self):
        return self.rows("SELECT * FROM projects ORDER BY name,id")

    def project(self, value=None):
        value = value or self.get_value("active_project")
        rows = self.rows("SELECT * FROM projects WHERE id=? OR name=?", (value, value))
        if len(rows) != 1:
            raise ValueError("请先发送“项目列表”，再用“切换项目 项目名”；同名项目请使用完整 ID")
        return rows[0]

    def file_path(self, row):
        if row["name"] != safe_name(row["name"]):
            raise ValueError("文件名无效")
        if row.get("storage_root"):
            if row.get("relative_path") != row["name"]:
                raise ValueError("文件路径超出项目目录")
            root = Path(row["storage_root"])
            if not root.is_absolute():
                raise ValueError("项目文件目录必须为绝对路径")
            folder = directory(root)
        elif row.get("relative_path"):
            if row["relative_path"] != "files/" + row["name"]:
                raise ValueError("文件路径超出项目目录")
            folder = directory(self.managed_project_dir(row["project_id"]) / "files")
        else:
            # Existing v0.2 file snapshots remain readable during in-place upgrades.
            key = hashlib.sha256(row["project_id"].encode()).hexdigest()
            folder = directory(self.root / "projects" / key / "files" / row["id"])
        return folder / row["name"]

    def file_content(self, row):
        content = read_regular(self.file_path(row), self.max_bytes)
        if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("文件内容变化，原文件快照已失效")
        return content

    def save_file(self, row, content):
        if len(content) > self.max_bytes:
            raise ValueError("文件超过大小限制")
        path = self.file_path(row)
        self.write_content(path, content)
        with self.transaction() as db:
            db.execute(
                "UPDATE files SET state='ready',size=?,sha256=? WHERE id=?",
                (len(content), hashlib.sha256(content).hexdigest(), row["id"]),
            )
            db.execute(
                "UPDATE jobs SET state='completed' WHERE file_id=? AND kind='file_receive'",
                (row["id"],),
            )
            self.enqueue(
                "download:" + row["id"],
                f"【{self.project(row['project_id'])['name']}】《{row['name']}》"
                f"已保存到{self.file_location_label(row)}。\n{path}",
                row["source_key"].rsplit(":", 1)[0],
                row["project_id"],
            )

    @staticmethod
    def write_content(path, content):
        temporary = path.with_name(".download-" + secrets.token_hex(12))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # An operator-created file or a simultaneous write must never be overwritten.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if read_regular(path, len(content)) != content:
                    raise ValueError("项目目录已存在不同内容的同名文件，本次不会覆盖") from None
        finally:
            temporary.unlink(missing_ok=True)

    def verify_job_files(self, job_id, project_id):
        for snapshot in self.rows("SELECT * FROM job_files WHERE job_id=?", (job_id,)):
            file = self.one(
                "SELECT * FROM files WHERE id=? AND project_id=?", (snapshot["file_id"], project_id)
            )
            if file["sha256"] != snapshot["sha256"]:
                raise ValueError("任务资料快照已失效")
            self.file_content(file)

    def enqueue(self, key, text, event, project_id=None, task_id=None, job_id=None, kind="notice"):
        if not event:
            raise ValueError("发送必须绑定操作者请求")
        self.db.execute(
            """INSERT OR IGNORE INTO outbox
          (dedupe_key,project_id,task_id,job_id,kind,destination,body,marker,source_event,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                project_id,
                task_id,
                job_id,
                kind,
                self.owner_id,
                text,
                secrets.token_hex(16),
                event,
                now(),
            ),
        )

    def new_task(self, project_id):
        cursor = self.db.execute(
            "INSERT INTO tasks(project_id,created_at) VALUES(?,?)", (project_id, now())
        )
        task_id = f"T{cursor.lastrowid:06d}"
        self.db.execute("UPDATE tasks SET id=? WHERE seq=?", (task_id, cursor.lastrowid))
        return task_id

    def new_job(self, project_id, event, kind, body="", task_id=None, file_id=None):
        cursor = self.db.execute(
            """INSERT INTO jobs
          (project_id,task_id,source_event,kind,body,file_id,created_at) VALUES(?,?,?,?,?,?,?)""",
            (project_id, task_id, event, kind, body, file_id, now()),
        )
        job_id = f"J{cursor.lastrowid:06d}"
        self.db.execute("UPDATE jobs SET id=? WHERE seq=?", (job_id, cursor.lastrowid))
        return job_id

    def recover(self):
        with self.transaction() as db:
            # Queued work is durable. A claimed job is retried only if no
            # writable Codex turn could have begun, or a complete native
            # answer can be committed without another model submission.
            interrupted = self.rows("SELECT * FROM jobs WHERE state='claimed'")
            for job in interrupted:
                job_id = job["id"]
                work = self.root / "engine" / "jobs" / job_id
                answer = work / "answer.json"
                thread = self.rows("SELECT thread_id FROM tasks WHERE id=?", (job["task_id"],))
                has_thread = bool(thread and thread[0]["thread_id"])
                schedule_exists = bool(self.rows(
                    "SELECT 1 FROM local_schedules WHERE request_job=? LIMIT 1", (job_id,)
                ))
                main_started = self.get_value("main_started:" + job_id) == "true"
                complete_answer = False
                if main_started and has_thread and not schedule_exists:
                    try:
                        value = json.loads(read_regular(answer, 400000))
                        complete_answer = (
                            isinstance(value, dict)
                            and set(value) == {"reply"}
                            and isinstance(value["reply"], str)
                            and 0 < len(value["reply"].strip()) <= 100000
                        )
                    except (OSError, ValueError, TypeError):
                        pass
                if complete_answer:
                    self.set_value("resume_only:" + job_id, "true")
                    db.execute(
                        "UPDATE jobs SET state='queued',claim_token=NULL,error='' WHERE id=?",
                        (job_id,),
                    )
                    self.audit("restart_result_recovered", {"job_id": job_id})
                    continue
                classification_started = self.get_value("classification_started:" + job_id) == "true"
                legacy_prompt = (work / "prompt.txt").exists() and not (
                    main_started or classification_started
                )
                safe_to_retry = not schedule_exists and not legacy_prompt and (
                    not main_started or job["kind"] == "schedule_request"
                )
                if safe_to_retry:
                    if (
                        job["kind"] == "message"
                        and self.get_value("intent_classified:" + job_id) not in ("", "ordinary")
                    ):
                        db.execute("DELETE FROM kv WHERE key=?", ("intent_classified:" + job_id,))
                    db.execute(
                        "UPDATE jobs SET state='queued',claim_token=NULL,error='' WHERE id=?",
                        (job_id,),
                    )
                    self.audit("restart_job_requeued", {"job_id": job_id})
                    continue
                db.execute(
                    "UPDATE jobs SET state='uncertain',error='执行中断，请核对原生会话' WHERE id=?",
                    (job_id,),
                )
                self.enqueue(
                    "failed:" + job_id,
                    f"【{self.project(job['project_id'])['name']}】"
                    f"{job['task_id'] or job_id} 处理失败：服务重启时执行状态不明；"
                    "队列已暂停，请核对原生会话。",
                    job["source_event"], job["project_id"], job["task_id"], job_id,
                )
            db.execute("UPDATE files SET state='pending' WHERE state='downloading'")
            db.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
            db.execute("UPDATE local_schedules SET state='uncertain' WHERE state='installing'")
            # v0.2 cloud-file uploads are now local receive operations.
            db.execute("UPDATE jobs SET kind='file_receive' WHERE kind='file_sync'")
            db.execute("""UPDATE jobs SET state='completed' WHERE kind='file_receive'
              AND file_id IN (SELECT id FROM files WHERE state='ready')""")

    def close(self):
        self.db.close()

"""Project-scoped task claims, exact session binding and validated result commit.

Execution adapters use this deterministic boundary; it has no model or network
dependency and never infers successful execution from queue state alone.
"""

import hashlib
import secrets


class NativeBridge:
    def __init__(self, store):
        self.store = store

    def claim(self, project_id):
        self.store.project(project_id)
        with self.store.transaction() as db:
            job = self.store.rows(
                "SELECT * FROM jobs WHERE project_id=? AND state!='completed' ORDER BY seq LIMIT 1",
                (project_id,),
            )
            if not job or job[0]["state"] != "queued":
                return None
            job = job[0]
            if job["kind"] not in ("message", "schedule_request", "scheduled_message"):
                return None
            token = secrets.token_urlsafe(32)
            db.execute(
                "UPDATE jobs SET state='claimed',claim_token=? WHERE id=?", (token, job["id"])
            )
            self.store.audit("native_claim", {"project_id": project_id, "job_id": job["id"]})
            task = (
                self.store.one(
                    "SELECT * FROM tasks WHERE id=? AND project_id=?", (job["task_id"], project_id)
                )
                if job["task_id"]
                else None
            )
            files = (
                self.store.rows(
                    "SELECT id,project_id,name,size,sha256,relative_path,storage_root FROM files WHERE project_id=? AND state='ready'",
                    (project_id,),
                ) if job["kind"] in ("message", "scheduled_message") else []
            )
            for snapshot in files:
                snapshot["path"] = str(self.store.file_path(snapshot))
                db.execute(
                    "INSERT OR IGNORE INTO job_files VALUES(?,?,?)",
                    (job["id"], snapshot["id"], snapshot["sha256"]),
                )
            return {
                "job_id": job["id"],
                "project_id": project_id,
                "project_name": self.store.project(project_id)["name"],
                "kind": job["kind"],
                "source_event": job["source_event"],
                "task_id": job["task_id"],
                "text": job["body"],
                "thread_id": task["thread_id"] if task else None,
                "claim_token": token,
                "files": files,
                "project_directory": str(self.store.project_dir(project_id)),
                "source_directory": self.store.project(project_id).get("source_path"),
                "file_directories": sorted({str(self.store.file_path(f).parent) for f in files}),
                "resume_only": self.store.get_value("resume_only:" + job["id"]) == "true",
            }

    def owned_job(self, project_id, job_id, token):
        job = self.store.one("SELECT * FROM jobs WHERE id=? AND project_id=?", (job_id, project_id))
        if (
            job["state"] != "claimed"
            or not job["claim_token"]
            or not secrets.compare_digest(job["claim_token"], token)
        ):
            raise ValueError("任务已完成、已中断或执行凭据不匹配")
        return job

    def bind_thread(self, project_id, job_id, token, thread_id):
        if not thread_id.strip() or len(thread_id) > 200:
            raise ValueError("原生会话 ID 无效")
        with self.store.transaction() as db:
            job = self.owned_job(project_id, job_id, token)
            task = self.store.one(
                "SELECT * FROM tasks WHERE id=? AND project_id=?", (job["task_id"], project_id)
            )
            if task["thread_id"] and task["thread_id"] != thread_id:
                raise ValueError("不能改变任务所属原生会话")
            elsewhere = self.store.rows(
                "SELECT id FROM tasks WHERE thread_id=? AND id!=?", (thread_id, task["id"])
            )
            if elsewhere:
                raise ValueError("原生会话已绑定其他任务")
            db.execute("UPDATE tasks SET thread_id=? WHERE id=?", (thread_id, task["id"]))

    def complete(self, project_id, job_id, token, text="", thread_id=None):
        with self.store.transaction() as db:
            job = self.owned_job(project_id, job_id, token)
            project = self.store.project(project_id)
            prefix = f"【{project['name']}】"
            task = self.store.one(
                "SELECT * FROM tasks WHERE id=? AND project_id=?", (job["task_id"], project_id)
            )
            if not thread_id or thread_id != task["thread_id"]:
                raise ValueError("原生会话未绑定或不匹配")
            if not text.strip() or len(text) > 100000:
                raise ValueError("结果为空或过长")
            self.store.verify_job_files(job_id, project_id)
            parts = [text[i : i + 2800] for i in range(0, len(text), 2800)]
            parts = [
                prefix + f" {job['task_id']} ({i + 1}/{len(parts)})\n" + part
                for i, part in enumerate(parts)
            ]
            parts[-1] += f"\n\n✅ 本轮任务已完成（{job_id}）"
            for index, part in enumerate(parts):
                self.store.enqueue(
                    f"result:{job_id}:{index}",
                    part,
                    job["source_event"],
                    project_id,
                    job["task_id"],
                    job_id,
                    "result",
                )
            db.execute("UPDATE jobs SET state='completed' WHERE id=?", (job_id,))
            self.store.set_value("resume_only:" + job_id, "false")
            self.store.audit(
                "native_completed",
                {
                    "project_id": project_id,
                    "job_id": job_id,
                    "thread_id": thread_id,
                    "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            )

    def fail(self, project_id, job_id, token, uncertain=True):
        with self.store.transaction() as db:
            job = self.owned_job(project_id, job_id, token)
            state = "uncertain" if uncertain else "failed"
            db.execute(
                "UPDATE jobs SET state=?,error=? WHERE id=?",
                (state, "原生项目执行未完成，请人工核查", job_id),
            )
            self.store.enqueue(
                "failed:" + job_id,
                f"【{self.store.project(project_id)['name']}】{job['task_id'] or job_id} 处理失败：执行状态未能确认；队列已暂停，请核对原生会话。",
                job["source_event"],
                project_id,
                job["task_id"],
                job_id,
            )

    def reconcile(self, project_id, job_id, outcome):
        """Operator only. Never called by the model or a project worker."""
        if outcome not in ("not_started", "resume_bound", "discard"):
            raise ValueError("仅支持核实未执行、恢复已绑定会话，或放弃过期工作单")
        with self.store.transaction() as db:
            job = self.store.one(
                "SELECT * FROM jobs WHERE id=? AND project_id=?", (job_id, project_id)
            )
            if job["state"] not in ("uncertain", "failed"):
                raise ValueError("只有中断任务需要人工核对")
            if outcome == "discard":
                active = self.store.rows(
                    "SELECT id FROM local_schedules WHERE request_job=? "
                    "AND state IN ('active','installing','uncertain')", (job_id,)
                )
                if active:
                    raise ValueError("工作单仍有关联的有效定时计划，不能放弃")
                db.execute(
                    "UPDATE jobs SET state='completed',claim_token=NULL,error=? WHERE id=?",
                    ("操作者已放弃恢复过期工作单", job_id),
                )
                if self.store.get_value("active_task:" + project_id) == job["task_id"]:
                    self.store.set_value("active_task:" + project_id, "")
                self.store.audit("operator_discarded_uncertain", {"job_id": job_id})
                return
            if outcome == "resume_bound":
                task = self.store.one("SELECT * FROM tasks WHERE id=?", (job["task_id"],))
                if not task["thread_id"]:
                    raise ValueError("没有已绑定会话可恢复")
            self.store.set_value(
                "resume_only:" + job_id, "true" if outcome == "resume_bound" else "false"
            )
            db.execute(
                "UPDATE jobs SET state='queued',claim_token=NULL,error='' WHERE id=?", (job_id,)
            )
            self.store.audit("operator_reconciled", {"job_id": job_id, "outcome": outcome})

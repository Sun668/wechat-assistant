"""Authenticated owner commands and immutable project routing. No model decisions."""

import json
import re
import secrets

from ..workspace_io import utc_now as now
from .project_paths import is_excluded_project_path

HELP = (
    "先发“项目列表”，再发“选择项目 1”，之后直接提问或发文件。\n"
    "设置收件目录后，也可不选项目直接发文件。\n"
    "\n项目\n"
    "项目列表：刷新列表\n"
    "选择项目 1 / 切换项目 名称\n"
    "当前项目 / 项目目录 / 退出项目\n"
    "[项目名] 请求内容：指定项目\n"
    "\n文件\n"
    "文件列表：查看已收文件\n"
    "发送文件《文件名》：取回文件\n"
    "重试文件《文件名》：重试下载\n"
    "\n任务与会话\n"
    "任务列表：查看状态\n"
    "新对话：另开会话\n"
    "继续 T000001 要求：续问\n"
    "\n收件目录\n"
    "/upload-path：查看\n"
    "/upload-path 绝对路径：设置\n"
    "/upload-path reset：恢复项目目录\n"
    "\n/help：查看帮助"
)

CHINESE_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def project_ordinal(value):
    """Read an explicit menu number without guessing from a project name."""
    if value.isdecimal():
        return int(value)
    match = re.fullmatch(r"第?([一二两兩三四五六七八九十]+)(?:个|號|号)?", value)
    if not match:
        return None
    number = match[1]
    if number in CHINESE_DIGITS:
        return CHINESE_DIGITS[number]
    tens = re.fullmatch(r"([一二两兩三四五六七八九])?十([一二两兩三四五六七八九])?", number)
    if not tens:
        return None
    return (CHINESE_DIGITS[tens[1]] if tens[1] else 1) * 10 + (
        CHINESE_DIGITS[tens[2]] if tens[2] else 0
    )


def decode(raw):
    text, quote = [], None
    for item in raw.get("item_list", []):
        if item.get("type") == 1:
            value = item.get("text_item", {}).get("text", "")
            if isinstance(value, str):
                text.append(value)
        ref = item.get("ref_msg")
        if isinstance(ref, dict):
            message = ref.get("message_item") or {}
            reference = ref.get("svr_id")
            if type(reference) not in (str, int) or reference in ("", "0", 0):
                reference = message.get("msg_id")
            quote = {
                "id": str(reference)
                if type(reference) in (str, int) and reference not in ("", "0", 0)
                else None,
                "text": message.get("text_item", {}).get("text") or ref.get("title") or "",
            }
    return "\n".join(text).strip(), quote


class ProjectRouter:
    def __init__(self, store):
        self.store = store

    def available_projects(self):
        projects = [
            p for p in self.store.projects()
            if p.get("source_path") and not is_excluded_project_path(p["source_path"])
        ]
        catalog = self.store.get_value("project_catalog_ids", None)
        if catalog is not None:
            available = set(json.loads(catalog))
            projects = [p for p in projects if p["id"] in available]
        return projects

    def project_menu(self):
        projects = self.available_projects()
        self.store.set_value("project_choices", json.dumps([p["id"] for p in projects]))

        def description(project):
            try:
                self.store.codex_project_dir(project["id"])
            except ValueError:
                return "目录未就绪"
            return "Codex 本地目录"

        return "请先选择工作项目，之后的消息和文件将持续归入该项目。\n" + (
            "\n".join(
                f"{i + 1}. {p['name']} · {description(p)} · {p['id']}"
                for i, p in enumerate(projects)
            )
            + "\n发送“选择项目 序号”或“选择项目 完整项目名”；序号可用中文数字，空格可省略。"
            if projects
            else "当前没有已登记且可用的本地项目。请检查 Codex 登录和项目主目录后，再发送“项目列表”。"
        )

    def selected_project(self, value):
        ordinal = project_ordinal(value)
        if ordinal is not None:
            choices = json.loads(self.store.get_value("project_choices", "[]"))
            index = ordinal - 1
            if not 0 <= index < len(choices):
                raise ValueError("序号无效，请重新发送“项目列表”")
            project = self.store.project(choices[index])
        else:
            project = self.store.project(value)
        catalog = self.store.get_value("project_catalog_ids", None)
        if catalog is not None and project["id"] not in json.loads(catalog):
            raise ValueError("此项目已不在最新可用列表，请重新发送“项目列表”")
        self.store.codex_project_dir(project["id"])
        return project

    def ingest(self, raw, project_menu_notice=None):
        """Only the owner-filtering iLink adapter supplies events here."""
        if raw.get("from_user_id") != self.store.owner_id or raw.get("group_id"):
            return {"status": "ignored"}
        event = str(raw.get("message_id", ""))
        if not event or event == "None":
            raise ValueError("消息缺少精确 ID")
        with self.store.transaction():
            if self.store.rows("SELECT 1 FROM ingress WHERE external_id=?", (event,)):
                return {"status": "duplicate"}
            if self.store.rows("SELECT 1 FROM outbox WHERE external_id=?", (event,)):
                return {"status": "echo"}
            self.store.remember_source_event("ilink", event, raw)
            # Invalid commands are durably rejected, not endlessly redelivered.
            try:
                with self.store.transaction():
                    project_id, result = self._route(event, raw, project_menu_notice)
            except ValueError as exc:
                project_id, result = None, {"status": "rejected", "detail": str(exc)}
                detail = str(exc)
                if not self.store.get_value("active_project"):
                    detail += "\n" + self.project_menu()
                self.store.enqueue("reject:" + event, detail, event)
            self.store.db.execute("INSERT INTO ingress VALUES(?,?,?)", (event, project_id, now()))
            return result

    def _quote(self, quote):
        if not quote:
            return None
        # Full random marker or exact server message ID, never a title or task-number guess.
        rows = self.store.rows("SELECT * FROM outbox WHERE state IN ('sent','uncertain')")
        matched = [
            r
            for r in rows
            if (quote["id"] and r["external_id"] == quote["id"])
            or (r["marker"] and f"【CB:{r['marker']}】" in quote["text"])
        ]
        if len(matched) == 1 and matched[0]["project_id"]:
            return matched[0]
        if quote["id"]:
            files = self.store.rows(
                "SELECT * FROM files WHERE substr(source_key,1,?)=?",
                (len(quote["id"]) + 1, quote["id"] + ":"),
            )
            projects = {f["project_id"] for f in files}
            if len(projects) == 1:
                if None in projects:
                    raise ValueError("引用的文件尚未归属项目；请先选择项目再发送处理请求")
                return {"project_id": projects.pop(), "task_id": None}
        raise ValueError("引用未能精确定位，请引用完整回复，或用“继续 T000001 修改要求”。")

    def _route(self, event, raw, project_menu_notice=None):
        text, quote = decode(raw)
        files = [
            (i, item.get("file_item") or {})
            for i, item in enumerate(raw.get("item_list", []))
            if item.get("type") == 4
        ]
        result = {"status": "accepted"}
        project = None
        task_id = None
        handled = False
        if re.match(r"^/upload-path(?:\s|$)", text):
            if files or quote:
                raise ValueError("请单独发送 /upload-path 指令，不要附带文件或引用")
            value = text[len("/upload-path"):].strip()
            if value:
                value = "" if value in ('reset', '""', "''") else value
                value = self.store.set_upload_path(value)
                self.store.audit("upload_path_changed", {"event": event, "path": value})
            else:
                value = self.store.upload_path()
            body = (
                "默认上传目录：\n" + value if value else
                "默认上传目录为空：沿用所选项目的第一个本地目录。"
            )
            self.store.enqueue("command:" + event, body + "\n仅影响后续新收件，在途和已收文件不迁移。", event)
            return None, {"status": "command"}
        if text in ("/help", "帮助", "help", "项目列表", "选择项目", "切换项目", "当前项目", "退出项目"):
            if text in ("项目列表", "选择项目", "切换项目"):
                body = self.project_menu()
                if text == "项目列表" and project_menu_notice:
                    body = project_menu_notice + "\n" + body
            elif text == "退出项目":
                self.store.set_value("active_project", "")
                body = self.project_menu()
            elif text == "当前项目":
                current = self.store.project()
                body = (
                    "当前项目："
                    + current["name"]
                    + "\n项目目录："
                    + str(self.store.project_dir(current["id"]))
                )
                if current.get("source_path"):
                    body += "\n本地既有资料：" + current["source_path"]
            else:
                body = HELP
            self.store.enqueue("command:" + event, body, event)
            handled = True
        switch = re.fullmatch(r"(?:选择项目\s*|切换项目\s*|切换到\s*)(.+)", text)
        if not switch and not self.store.get_value("active_project"):
            if project_ordinal(text) is not None or self.store.rows(
                "SELECT 1 FROM projects WHERE id=? OR name=?", (text, text)
            ):
                switch = re.fullmatch(r"选择项目\s+(.+)", "选择项目 " + text)
        if switch:
            project = self.selected_project(switch[1].strip())
            self.store.set_value("active_project", project["id"])
            self.store.enqueue(
                "command:" + event,
                f"当前项目：{project['name']}。后续消息和文件归入此项目。",
                event,
                project["id"],
            )
            handled = True
        explicit = re.fullmatch(r"\[([^\]]+)\]\s*(.*)", text, re.DOTALL)
        if explicit:
            project = self.store.project(explicit[1].strip())
            text = explicit[2].strip()
            self.store.set_value("active_project", project["id"])
        target = self._quote(quote)
        continuation = re.fullmatch(r"继续\s+(T\d{6,})\s+(.+)", text, re.DOTALL)
        if continuation:
            task = self.store.one("SELECT * FROM tasks WHERE id=?", (continuation[1],))
            if target and target["task_id"] != task["id"]:
                raise ValueError("引用与指定任务冲突")
            target = {"project_id": task["project_id"], "task_id": task["id"]}
            text = continuation[2].strip()
        if target:
            if project and project["id"] != target["project_id"]:
                raise ValueError("引用任务与指定项目冲突，请分别发送")
            project = self.store.project(target["project_id"])
            task_id = target["task_id"]
        if not files and handled:
            return project["id"] if project else None, {"status": "command"}
        if project is None and not self.store.get_value("active_project"):
            if files:
                self._receive_files(event, files, None, "【收件箱】")
                if text and not handled:
                    self.store.enqueue(
                        "unassigned-text:" + event,
                        "文件已登记到收件箱；附带文字未提交给 Codex。请先选择项目再单独发送处理请求。",
                        event,
                    )
                return None, {"status": "accepted", "project_id": None}
            if text == "文件列表":
                self._list_files(event, None, "【收件箱】")
                return None, {"status": "command"}
            retry = re.fullmatch(r"重试文件\s*《(.+)》", text)
            if retry:
                self._retry_file(event, None, "【收件箱】", retry[1])
                return None, {"status": "command"}
        project = project or self.store.project()
        pid = project["id"]
        self.store.codex_project_dir(pid)
        prefix = f"【{project['name']}】"
        if text == "新对话":
            self.store.set_value("active_task:" + pid, "")
            memory_note = "Codex 本地记忆按当前主机设置使用。"
            self.store.enqueue(
                "command:" + event,
                prefix + " 已开启新对话；下一条消息会建立新会话。项目文件保留。" + memory_note,
                event,
                pid,
            )
            handled = True
        if text == "文件列表":
            self._list_files(event, pid, prefix)
            handled = True
        if text == "项目目录":
            self.store.enqueue(
                "command:" + event, prefix + "\n" + str(self.store.project_dir(pid)), event, pid
            )
            handled = True
        retry = re.fullmatch(r"重试文件\s*《(.+)》", text)
        if retry:
            self._retry_file(event, pid, prefix, retry[1])
            handled = True
        elif text == "任务列表":
            rows = self.store.rows(
                "SELECT j.id,j.task_id,j.kind,j.state FROM jobs j WHERE project_id=? ORDER BY seq DESC LIMIT 30",
                (pid,),
            )
            self.store.enqueue(
                "command:" + event,
                prefix
                + "\n"
                + (
                    "\n".join(
                        f"{r['task_id'] or r['id']} · {r['kind']} · {r['state']}" for r in rows
                    )
                    or "暂无任务"
                ),
                event,
                pid,
            )
            handled = True
        send_file = re.fullmatch(r"发送文件\s*[《\"]?(.+?)[》\"]?", text)
        if send_file:
            name = send_file[1].strip()
            rows = self.store.rows("SELECT * FROM files WHERE project_id=? AND name=?", (pid, name))
            if len(rows) != 1:
                raise ValueError("此项目找不到唯一的同名文件，请使用完整且唯一的文件名")
            file = rows[0]
            if file["state"] != "ready":
                raise ValueError("文件尚未就绪")
            self.store.file_content(file)
            payload = json.dumps({"id": file["id"], "sha256": file["sha256"]})
            self.store.enqueue("file-send:" + event, payload, event, pid, kind="file")
            handled = True
        if files:
            self._receive_files(event, files, pid, prefix)
        if text and not handled:
            if len(text) > 20000:
                raise ValueError("单条问题过长，请改为文件发送")
            requested_task = task_id is not None
            task_id = task_id or self.store.get_value("active_task:" + pid)
            if task_id:
                task = self.store.one(
                    "SELECT * FROM tasks WHERE id=? AND project_id=?", (task_id, pid)
                )
                if (
                    task["thread_id"]
                    and self.store.get_value("task_engine:" + task_id) != "codex_shared"
                ):
                    if requested_task:
                        raise ValueError(
                            "该任务属于旧执行入口，不能续用；请直接发送新问题建立本地 Codex 会话"
                        )
                    task_id = None
            if not task_id:
                task_id = self.store.new_task(pid)
            self.store.set_value("active_task:" + pid, task_id)
            ahead = self.store.rows(
                "SELECT id,state FROM jobs WHERE state!='completed' ORDER BY seq LIMIT 1"
            )
            job_id = self.store.new_job(pid, event, "message", text, task_id)
            notice = (
                f"已收到，正在排队；前一项 {ahead[0]['id']} 完成后会执行。"
                if ahead and ahead[0]["state"] in ("queued", "claimed")
                else (
                    f"已收到；队列因 {ahead[0]['id']} 执行状态待核对而暂停，本条已保留。"
                    if ahead
                    else "已收到。"
                )
            )
            self.store.enqueue(
                "started:" + job_id,
                prefix + f" {task_id} " + notice,
                event,
                pid,
                task_id,
                job_id,
            )
            result.update(task_id=task_id, job_id=job_id)
        result["project_id"] = pid
        return pid, result

    def _list_files(self, event, pid, prefix):
        rows = self.store.rows(
            "SELECT name,state,project_id,storage_root FROM files WHERE project_id IS ? ORDER BY created_at",
            (pid,),
        )
        body = "\n".join(
            f"《{r['name']}》 · " + (
                "已保存到" + self.store.file_location_label(r) if r["state"] == "ready" else r["state"]
            ) for r in rows
        )
        self.store.enqueue("command:" + event, prefix + "\n" + (body or "尚无收件记录"), event, pid)

    def _retry_file(self, event, pid, prefix, name):
        matches = self.store.rows("SELECT * FROM files WHERE project_id IS ? AND name=?", (pid, name))
        if len(matches) != 1 or matches[0]["state"] != "failed":
            raise ValueError("没有唯一的下载失败文件")
        self.store.db.execute("UPDATE files SET state='pending' WHERE id=?", (matches[0]["id"],))
        self.store.db.execute(
            "UPDATE outbox SET dedupe_key=dedupe_key || ? WHERE dedupe_key=?",
            (":" + event, "download:" + matches[0]["id"]),
        )
        self.store.enqueue("retry:" + event, prefix + " 文件已排队重新下载。", event, pid)

    def _receive_files(self, event, files, pid, prefix):
        root = self.store.receive_directory(pid)
        for index, item in files:
            ahead = self.store.rows(
                "SELECT id,state FROM jobs WHERE state!='completed' ORDER BY seq LIMIT 1"
            )
            fid = "f-" + secrets.token_hex(16)
            name = self.store.available_name(
                pid, item.get("file_name") or "attachment.bin", storage_root=root
            )
            self.store.db.execute(
                """INSERT INTO files
              (id,project_id,source_key,name,state,media,created_at,relative_path,storage_root) VALUES(?,?,?,?,'pending',?,?,?,?)""",
                (fid, pid, f"{event}:{index}", name, json.dumps(item), now(), name, str(root)),
            )
            job_id = self.store.new_job(pid, event, "file_receive", file_id=fid)
            receipt = (
                f"已收到《{name}》，正在排队；前一项 {ahead[0]['id']} 完成后保存。"
                if ahead and ahead[0]["state"] in ("queued", "claimed")
                else (
                    f"已收到《{name}》；队列因 {ahead[0]['id']} 待核对而暂停，文件已保留。"
                    if ahead
                    else f"已收到《{name}》。"
                )
            )
            self.store.enqueue(
                "file-received:" + fid,
                prefix + " " + receipt,
                event,
                pid,
                job_id=job_id,
            )

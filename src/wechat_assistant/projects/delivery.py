"""A single owner-only writer; never retries an uncertain write."""

import json
import threading

from ..models import FilePayload, NotSent


class OwnerDelivery:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport
        self.lock = threading.Lock()

    def source_preview(self, event):
        rows = self.store.rows(
            "SELECT raw FROM source_events WHERE channel='ilink' AND external_id=?", (event,)
        )
        if not rows:
            return ""
        raw = json.loads(rows[0]["raw"])
        if not isinstance(raw, dict):
            return ""
        for item in raw.get("item_list", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == 1 and isinstance(item.get("text_item"), dict):
                value = item["text_item"].get("text")
                if isinstance(value, str) and value.strip():
                    preview = " ".join(value.split())
                    return preview[:80] + ("…" if len(preview) > 80 else "")
            if item.get("type") == 4 and isinstance(item.get("file_item"), dict):
                name = item["file_item"].get("file_name")
                if isinstance(name, str) and name.strip():
                    return "文件《" + name[:70] + "》"
        return ""

    def flush(self):
        if self.transport is None or not self.transport.can_notify():
            return
        with self.lock:
            while True:
                with self.store.transaction() as db:
                    rows = self.store.rows(
                        "SELECT * FROM outbox WHERE state='pending' ORDER BY id LIMIT 1"
                    )
                    if not rows:
                        return
                    row = rows[0]
                    if row["destination"] != self.store.owner_id or not row["source_event"]:
                        raise ValueError("发送目标或操作者请求不匹配")
                    if not self.store.is_owner_source_event(row["source_event"]):
                        db.execute("UPDATE outbox SET state='failed' WHERE id=?", (row["id"],))
                        continue
                    db.execute("UPDATE outbox SET state='sending' WHERE id=?", (row["id"],))
                try:
                    client_id = "cb-" + row["marker"]
                    if row["kind"] == "file":
                        payload = json.loads(row["body"])
                        file = self.store.one(
                            "SELECT * FROM files WHERE id=? AND project_id=?",
                            (payload["id"], row["project_id"]),
                        )
                        if file["sha256"] != payload["sha256"]:
                            raise NotSent("文件授权快照已失效")
                        try:
                            content = self.store.file_content(file)
                        except (ValueError, OSError) as exc:
                            raise NotSent("文件授权快照已失效") from exc
                        receipt = self.transport.send_file(
                            row["destination"],
                            FilePayload(
                                file_id=file["id"],
                                name=file["name"],
                                size=file["size"],
                                sha256=file["sha256"],
                            ),
                            content,
                            client_id,
                        )
                    else:
                        if row["kind"] == "result":
                            try:
                                self.store.verify_job_files(row["job_id"], row["project_id"])
                            except (OSError, ValueError) as exc:
                                raise NotSent("任务资料快照已失效") from exc
                        body = row["body"]
                        relates_to_request = row["kind"] == "result" or row[
                            "dedupe_key"
                        ].startswith(
                            ("started:", "file-received:", "download:", "failed:",
                             "project-path-failed:")
                        )
                        if relates_to_request:
                            preview = self.source_preview(row["source_event"])
                            if preview:
                                body = f"↪ 对应你的消息：{preview}\n" + body
                        receipt = self.transport.send(
                            row["destination"], body + f"\n【CB:{row['marker']}】", client_id
                        )
                    state = "sent" if receipt.verified else "uncertain"
                    external_id = receipt.external_id
                except NotSent:
                    state, external_id = "failed", None
                except Exception:
                    state, external_id = "uncertain", None
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE outbox SET state=?,external_id=? WHERE id=?",
                        (state, external_id, row["id"]),
                    )
                # A failed/uncertain write is quarantined; other independent notices continue.

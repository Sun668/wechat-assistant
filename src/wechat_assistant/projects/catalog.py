"""Apply an authenticated native catalog atomically, retaining historical records."""

import json
import uuid
from pathlib import Path

from .project_paths import is_excluded_project_path


class ProjectCatalog:
    def __init__(self, store, source):
        self.store, self.source = store, source

    def refresh(self):
        # Fetch before opening a database transaction; transport must not block writers.
        pages = self.source.list_projects()
        with self.store.transaction():
            self.store.audit("native_project_catalog_source", {"pages": pages})
        if not isinstance(pages, list) or not pages:
            raise ValueError("项目列表响应无效")
        records, seen = [], set()
        for page in pages:
            for record in page["data"]:
                if not isinstance(record, dict):
                    raise ValueError("项目记录无效")
                pid, name, roots = record.get("id"), record.get("name"), record.get("roots")
                uuid.UUID(pid)
                if pid in seen or not isinstance(name, str) or not isinstance(roots, list):
                    raise ValueError("项目记录重复或无效")
                seen.add(pid)
                if not roots:
                    continue
                path = roots[0].get("path")
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise ValueError("项目主目录无效")
                if is_excluded_project_path(path):
                    continue
                records.append((record, path))
        with self.store.transaction():
            ids, skipped = [], 0
            for record, path in records:
                native_id, name = record["id"], record["name"]
                alias_key = "native_project_alias:" + native_id
                pid = self.store.get_value(alias_key)
                if not pid:
                    # Keep exact existing desktop IDs and their threads when the native
                    # catalog first identifies the same unique primary directory.
                    previous = self.store.rows(
                        "SELECT id FROM projects WHERE source_path=?", (str(Path(path).resolve()),)
                    )
                    metadata = record.get("metadata") or {}
                    pid = metadata.get("codex.localProjectId") or (
                        previous[0]["id"] if len(previous) == 1 else native_id
                    )
                # A different native identity reusing an old directory must not
                # inherit the old project's exact threads and file history.
                if self.store.rows(
                    "SELECT 1 FROM kv WHERE key LIKE 'native_project_alias:%' AND value=? AND key!=?",
                    (pid, alias_key),
                ):
                    skipped += 1
                    continue
                try:
                    with self.store.transaction():
                        self.store.register(pid, name, path)
                except (ValueError, OSError):
                    skipped += 1
                    continue
                if pid in ids:
                    raise ValueError("项目身份对应关系冲突")
                self.store.set_value(alias_key, pid)
                ids.append(pid)
            self.store.set_value("project_catalog_ids", json.dumps(ids))
            self.store.set_value("project_catalog_status", "ready")
            if self.store.get_value("active_project") not in ids:
                self.store.set_value("active_project", "")
            self.store.audit("native_project_catalog_refreshed", {"ids": ids, "skipped": skipped})
        return "已刷新本地项目列表。" + (
            f"（{skipped} 个项目目录不可用或映射需核对，未列出）" if skipped else ""
        )


def refresh_initial_catalog(settings, source=None):
    """Operator-invoked initialization shares the same catalog path as WeChat."""
    from ..adapters.codex_projects import CodexProjects
    from .storage import ProjectStore

    store = ProjectStore(settings.data_dir, settings.owner_id, settings.file_max_bytes)
    try:
        try:
            return ProjectCatalog(store, source or CodexProjects(settings)).refresh()
        except Exception as exc:
            with store.transaction():
                store.set_value("project_catalog_status", "failed")
                store.audit("native_project_catalog_failed", {
                    "source": "initialization", "error": type(exc).__name__,
                })
            return "项目刷新失败，保留上次保存的列表。请检查 Codex 登录或接口支持后，在微信重发“项目列表”。"
    finally:
        store.close()

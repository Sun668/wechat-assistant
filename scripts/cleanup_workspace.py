"""Remove only known v0.1 runtime artifacts from an explicitly selected workspace.

Default is a preview. --apply requires the shared runtime lock. User uploads and
raw history are retained; project data and ClawBot credentials are never deleted.
No network, login changes or global-path discovery.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

OBSOLETE = (
    "cache",
    "venv",
    "tmp",
    "vectors",
    "tasks",
    "backups",
    "codex",
    "web-wechat",
    "checkpoints.sqlite",
    "checkpoints.sqlite-wal",
    "checkpoints.sqlite-shm",
    "config.toml",
    "config.before-pipeline-20261030-074729.toml",
    "api-token",
    "assistant.log",
    "assistant.log.1",
    "assistant.log.2",
    "assistant.log.3",
    ".DS_Store",
)


def allocated_bytes(root):
    seen, total = set(), 0
    for base, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if not (Path(base) / d).is_symlink()]
        for name in files:
            file = Path(base) / name
            info = file.lstat()
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                seen.add(identity)
                total += info.st_blocks * 512
    return total


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def cleanup(workspace, apply=False):
    workspace = Path(workspace).expanduser().resolve(strict=True)
    state = workspace / ".wechat-assistant"
    if state.is_symlink() or not state.is_dir():
        raise ValueError("工作区运行目录不存在或是符号链接")
    report = {
        "applied": apply,
        "before_bytes": allocated_bytes(state),
        "remove": [n for n in OBSOLETE if (state / n).exists()],
        "retained_uploads": [],
        "archive": None,
    }
    if not apply:
        return report
    os.umask(0o077)
    lock_path = state / "runtime.lock"
    if lock_path.is_symlink():
        raise ValueError("工作区锁不能是符号链接")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # All validations happen before removing anything.
        for name in (
            *OBSOLETE,
            "attachments",
            "archive",
            "retained-files",
            "assistant.sqlite",
            "assistant.sqlite-wal",
            "assistant.sqlite-shm",
            "clawbot-projects",
        ):
            if (state / name).is_symlink():
                raise ValueError("清理目标不能是符号链接：" + name)
        old_db = state / "assistant.sqlite"
        if old_db.exists():
            new_db = state / "clawbot-projects" / "gateway.sqlite"
            if not new_db.is_file() or new_db.is_symlink():
                raise ValueError("需先初始化新版项目入口，保留腾讯同步游标后才能清理旧数据")
            with sqlite3.connect(new_db.as_uri() + "?mode=ro", uri=True) as db:
                if not db.execute("SELECT 1 FROM kv WHERE key='transport_cutover_done'").fetchone():
                    raise ValueError("旧腾讯同步游标尚未完成交接，未删除任何数据")
            archive = state / "archive"
            archive.mkdir(mode=0o700, exist_ok=True)
            target = archive / "legacy-history.sqlite"
            if target.exists() or target.is_symlink():
                raise ValueError("旧历史归档已经存在，不覆盖")
            source = sqlite3.connect(old_db.as_uri() + "?mode=ro", uri=True)
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("旧历史归档未通过完整性校验，停止清理")
            finally:
                destination.close()
                source.close()
            target.chmod(0o600)
            report["archive"] = str(target.relative_to(state))
        incoming = state / "attachments"
        retained = state / "retained-files"
        if incoming.exists():
            uploads = []
            for base, dirs, files in os.walk(incoming, followlinks=False):
                if any((Path(base) / d).is_symlink() for d in dirs):
                    raise ValueError("旧附件目录包含符号链接，停止清理")
                for name in files:
                    path = Path(base) / name
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("旧附件包含非常规文件，停止清理")
                    if name != ".DS_Store":
                        uploads.append(path)
            retained.mkdir(mode=0o700, exist_ok=True)
            for path in uploads:
                target = retained / path.name
                if target.exists():
                    if target.is_symlink() or digest(target) != digest(path):
                        target = retained / (
                            path.stem[:50] + "--" + digest(path)[:12] + path.suffix
                        )
                if target.exists():
                    if target.is_symlink() or digest(target) != digest(path):
                        raise ValueError("保留文件存在冲突，停止清理")
                else:
                    shutil.copyfile(path, target, follow_symlinks=False)
                    target.chmod(0o600)
                if digest(target) != digest(path):
                    raise ValueError("用户上传文件复制校验失败，停止清理")
                report["retained_uploads"].append(str(target.relative_to(state)))
        for name in report["remove"]:
            target = state / name
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if incoming.exists():
            shutil.rmtree(incoming)
        if old_db.exists():
            for name in ("assistant.sqlite", "assistant.sqlite-wal", "assistant.sqlite-shm"):
                (state / name).unlink(missing_ok=True)
        report["after_bytes"] = allocated_bytes(state)
        report["freed_bytes"] = report["before_bytes"] - report["after_bytes"]
        target = state / "cleanup-report.json"
        if target.is_symlink():
            raise ValueError("清理报告不能是符号链接")
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        target.chmod(0o600)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="仅清理指定工作区内已淘汰的微信助理数据")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(cleanup(args.workspace, args.apply), ensure_ascii=False, indent=2))

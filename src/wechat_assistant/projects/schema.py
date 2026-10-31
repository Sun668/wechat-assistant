"""Transactional upgrades of receipt ownership; never invent a native project."""

import os
import re
import secrets
import sqlite3
from contextlib import closing


def allow_unassigned_receipts(store):
    db = store.db

    def required(table):
        return any(r[1] == "project_id" and r[3] for r in db.execute(f"PRAGMA table_info({table})"))

    if not any(required(table) for table in ("files", "jobs")):
        return
    backups = store.root / "backups"
    if backups.is_symlink():
        raise ValueError("数据库备份目录不能是符号链接")
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups / ("before-unassigned-receipts-" + secrets.token_hex(8) + ".sqlite")
    os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with closing(sqlite3.connect(backup)) as snapshot:
        db.backup(snapshot)
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        with store.transaction():
            for table in ("files", "jobs"):
                if not required(table):
                    continue
                sql = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                temporary = table + "_unassigned_upgrade"
                sql, count = re.subn(
                    r"(\bproject_id\s+TEXT)\s+NOT\s+NULL\b", r"\1", sql, count=1, flags=re.I
                )
                if count != 1:
                    raise ValueError("无法识别旧收件表结构，数据库未升级")
                sql, count = re.subn(
                    r'^(CREATE TABLE\s+)(?:IF NOT EXISTS\s+)?["`]?'+table+r'["`]?',
                    r'\1'+temporary, sql, count=1, flags=re.I,
                )
                if count != 1:
                    raise ValueError("无法识别旧收件表名，数据库未升级")
                extras = [r[0] for r in db.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
                    "AND sql IS NOT NULL", (table,),
                )]
                sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
                db.execute(sql)
                db.execute(f"INSERT INTO {temporary} SELECT * FROM {table}")
                db.execute(f"DROP TABLE {table}")
                db.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
                for extra in extras:
                    db.execute(extra)
                if sequence is not None:
                    db.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                    db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, sequence[0]))
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("收件表升级校验失败，数据库未升级")
            store.audit("unassigned_receipts_schema_upgraded", {"backup": str(backup)})
    finally:
        db.execute("PRAGMA foreign_keys=ON")

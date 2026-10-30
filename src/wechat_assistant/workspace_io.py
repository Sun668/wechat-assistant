"""Filesystem primitives shared by transports and project storage."""

import os
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str) -> str:
    name = unicodedata.normalize("NFC", str(value)).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    name = name.strip(" .") or "attachment.bin"
    suffix = Path(name).suffix[:20]
    stem = name[: -len(suffix)] if suffix else name
    while len((stem + suffix).encode()) > 220:
        stem = stem[:-1]
    return stem + suffix


def read_regular(path: Path, max_bytes: int) -> bytes:
    """Never follow a symlink or read a device, FIFO, or unbounded file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("只接受大小限制以内的普通文件")
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("文件超过工作区文件大小限制")
    return content

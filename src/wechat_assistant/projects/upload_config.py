"""Local upload-directory configuration; no transport or model dependencies."""

import fcntl
import os
import tempfile
from pathlib import Path

import tomlkit

from .storage import directory


def default_config_path():
    return Path(__file__).resolve().parents[3] / "Config.toml"


class UploadConfig:
    def __init__(self, path, data_dir):
        self.path = Path(path)
        self.data_dir = Path(data_dir)

    @staticmethod
    def _no_symlinks(path):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("上传目录和配置文件路径不能包含符号链接")

    def _read(self):
        self._no_symlinks(self.path)
        try:
            return tomlkit.parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return tomlkit.document()
        except (OSError, ValueError):
            raise ValueError("无法读取 Config.toml，请检查文件权限和 TOML 格式") from None

    def _normalize(self, value):
        if not isinstance(value, str):
            raise ValueError("default_upload_path 必须为路径字符串或空字符串")
        value = value.strip()
        if not value:
            return ""
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("上传路径不能包含控制字符")
        try:
            path = Path(value).expanduser()
        except RuntimeError:
            raise ValueError("无法解析上传路径中的用户主目录") from None
        if not path.is_absolute():
            raise ValueError("上传路径必须为绝对路径，或以 ~/ 开头")
        self._no_symlinks(path)
        path = path.resolve()
        native = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()
        for protected in (self.data_dir.resolve(), native):
            if path == protected or protected in path.parents:
                raise ValueError("上传目录不能位于微信运行状态或 Codex 原生状态目录内")
        if ".git" in path.parts:
            raise ValueError("上传目录不能位于 Git 元数据目录内")
        if path.exists() and not path.is_dir():
            raise ValueError("上传路径必须是目录")
        return str(path)

    def get(self):
        return self._normalize(self._read().get("default_upload_path", ""))

    def receive_directory(self):
        try:
            value = self.get()
            if not value:
                return None
            return directory(Path(value))
        except OSError:
            raise ValueError("无法创建或访问上传目录，请检查路径和权限") from None

    def set(self, value):
        value = self._normalize(value)
        root = directory(self.data_dir / "clawbot-projects")
        lock_path = root / "upload-config.lock"
        self._no_symlinks(lock_path)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            document = self._read()
            if value:
                try:
                    target = directory(Path(value))
                    with tempfile.TemporaryFile(dir=target):
                        pass
                except OSError:
                    raise ValueError("上传目录不可写，请检查路径和权限") from None
            document["default_upload_path"] = value
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=".upload-config-", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(tomlkit.dumps(document))
                    stream.flush()
                    os.fsync(stream.fileno())
                self._no_symlinks(self.path)
                os.replace(temporary, self.path)
            except OSError:
                raise ValueError("无法写入 Config.toml，上传路径未修改") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return value

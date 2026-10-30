"""macOS user-session supervision; definitions and logs stay in the workspace."""

import fcntl
import hashlib
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time

from .storage import directory


class MacService:
    def __init__(self, settings):
        self.settings = settings
        digest = hashlib.sha256(str(settings.workspace).encode()).hexdigest()[:12]
        self.label = "local.clawbot-projects." + digest
        self.target = f"gui/{os.getuid()}/{self.label}"
        self.root = settings.data_dir / "clawbot-projects" / "service"
        self.plist = self.root / (self.label + ".plist")

    def definition(self, port):
        if not 1 <= port <= 65535:
            raise ValueError("端口必须在 1–65535 之间")
        return {
            "Label": self.label,
            "ProgramArguments": [
                sys.executable,
                "-B",
                "-m",
                "wechat_assistant.projects.cli",
                "--workspace",
                str(self.settings.workspace),
                "run",
                "--port",
                str(port),
            ],
            "WorkingDirectory": str(self.settings.workspace),
            "EnvironmentVariables": {
                "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
                "TMPDIR": str(self.settings.data_dir / "tmp"),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "ExitTimeOut": 60,
            "Umask": 0o077,
            "StandardOutPath": str(self.root / "stdout.log"),
            "StandardErrorPath": str(self.root / "stderr.log"),
        }

    def _launchctl(self, *args):
        if sys.platform != "darwin":
            raise ValueError("常驻服务命令仅支持 macOS")
        return subprocess.run(
            ["/bin/launchctl", *args],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def status(self):
        result = self._launchctl("print", self.target)
        status = {"label": self.label, "registered": result.returncode == 0}
        if result.returncode == 0:
            # Do not return the full service environment or inherited credentials.
            for field in ("state", "pid", "last exit code"):
                match = re.search(r"^\s*" + field + r" = (.+)$", result.stdout, re.MULTILINE)
                if match:
                    status[field.replace(" ", "_")] = match[1].strip()
        return status

    def start(self, port=8765):
        if self.settings.owner_id == "self":
            raise ValueError("请先绑定 ClawBot")
        definition = self.definition(port)
        current = self.status()
        if current["registered"]:
            return current
        directory(self.settings.data_dir)
        lock_path = self.settings.data_dir / "runtime.lock"
        if lock_path.is_symlink():
            raise ValueError("工作区锁不能是符号链接")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("工作区已有前台实例，请先停止它，再启动常驻服务") from None
        directory(self.root)
        directory(self.settings.data_dir / "tmp")
        for path in (self.plist, self.root / "stdout.log", self.root / "stderr.log"):
            if path.is_symlink():
                raise ValueError("服务配置和日志不能是符号链接")
        # The plist contains paths only, never tokens or a copied auth file.
        with self.plist.open("wb") as stream:
            plistlib.dump(definition, stream)
        self.plist.chmod(0o600)
        result = self._launchctl("bootstrap", f"gui/{os.getuid()}", str(self.plist))
        if result.returncode:
            raise ValueError(f"launchd 注册失败（退出码 {result.returncode}），请检查用户会话")
        return self.status()

    def stop(self):
        if not self.status()["registered"]:
            return {"label": self.label, "registered": False}
        result = self._launchctl("bootout", self.target)
        if result.returncode:
            raise ValueError(f"launchd 停止失败（退出码 {result.returncode}）")
        return {"label": self.label, "registered": False}


class AppBackgroundService(MacService):
    """Detached child of the authorized app, without launchd's separate TCC grant.

    A separate supervisor restarts the runtime; the runtime's own lock still
    enforces the single writer. No TCC identity or permission is changed.
    """

    @property
    def pid_file(self):
        return self.root / "manager.json"

    def status(self):
        try:
            if self.pid_file.is_symlink():
                raise ValueError("守护进程记录不能是符号链接")
            record = json.loads(self.pid_file.read_text())
            pid = record["pid"]
            if (
                type(pid) is not int
                or pid <= 1
                or record["workspace"] != str(self.settings.workspace)
            ):
                raise ValueError("守护进程记录不匹配")
            result = subprocess.run(
                ["/bin/ps", "-ww", "-p", str(pid), "-o", "uid=,command="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            fields = result.stdout.strip().split(maxsplit=1)
            alive = (
                result.returncode == 0
                and len(fields) == 2
                and fields[0] == str(os.getuid())
                and " -m wechat_assistant.projects.service " in fields[1]
                and fields[1].endswith("--workspace " + str(self.settings.workspace))
            )
            return {
                "registered": bool(alive),
                "mode": "app_background",
                "pid": pid if alive else None,
            }
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return {"registered": False, "mode": "app_background"}

    def start(self, port=8765):
        if self.settings.owner_id == "self":
            raise ValueError("请先绑定 ClawBot")
        self.definition(port)  # Validate the port without evaluating any shell text.
        current = self.status()
        if current["registered"]:
            return current
        directory(self.settings.data_dir)
        lock_path = self.settings.data_dir / "runtime.lock"
        if lock_path.is_symlink():
            raise ValueError("工作区锁不能是符号链接")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("工作区已有实例，请先停止它") from None
        directory(self.root)
        output = self.root / "background.log"
        if output.is_symlink() or self.pid_file.is_symlink():
            raise ValueError("守护进程记录和日志不能是符号链接")
        command = [
            sys.executable,
            "-B",
            "-m",
            "wechat_assistant.projects.service",
            "--port",
            str(port),
            "--workspace",
            str(self.settings.workspace),
        ]
        with output.open("ab") as log:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                start_new_session=True,
                cwd=self.settings.workspace,
            )
        return {
            "registered": False,
            "mode": "app_background",
            "state": "starting",
            "pid": child.pid,
        }

    def stop(self):
        current = self.status()
        if current["registered"]:
            os.kill(current["pid"], signal.SIGTERM)
        return {
            "mode": "app_background",
            "state": "stopping" if current["registered"] else "stopped",
        }

    def supervise(self, port):
        directory(self.root)
        lock_path = self.root / "manager.lock"
        if lock_path.is_symlink() or self.pid_file.is_symlink():
            raise ValueError("守护进程记录不能是符号链接")
        stopping = threading.Event()
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, lambda *_: stopping.set())
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            record = {"pid": os.getpid(), "workspace": str(self.settings.workspace), "port": port}
            self.pid_file.write_text(json.dumps(record, ensure_ascii=False))
            self.pid_file.chmod(0o600)
            try:
                while not stopping.is_set():
                    command = [
                        sys.executable,
                        "-B",
                        "-m",
                        "wechat_assistant.projects.cli",
                        "--workspace",
                        str(self.settings.workspace),
                        "run",
                        "--port",
                        str(port),
                    ]
                    child = subprocess.Popen(
                        command, stdin=subprocess.DEVNULL, start_new_session=True
                    )
                    try:
                        while child.poll() is None and not stopping.wait(0.25):
                            pass
                    finally:
                        if child.poll() is None:
                            child.send_signal(signal.SIGINT)
                            deadline = time.monotonic() + 55
                            while child.poll() is None and time.monotonic() < deadline:
                                time.sleep(0.1)
                            if child.poll() is None:
                                child.kill()
                        child.wait()
                    if not stopping.is_set():
                        print("runtime exited; retrying in 10 seconds", flush=True)
                        stopping.wait(10)
            finally:
                self.pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    import argparse

    from .cli import settings_for

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    os.umask(0o077)
    AppBackgroundService(settings_for(args.workspace)).supervise(args.port)

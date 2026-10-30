"""Official local Codex execution with the host's native login and session state."""

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..workspace_io import read_regular
from .storage import directory


class EngineUnavailable(RuntimeError):
    """Nothing was submitted; work remains queued."""


class ExecutionUncertain(RuntimeError):
    """A request may have been submitted; never retry automatically."""


@dataclass(frozen=True)
class LocalAnswer:
    thread_id: str
    text: str


class LocalEngine(Protocol):
    def readiness(self) -> str: ...
    def subscription_auth(self) -> bool: ...
    def run(
        self,
        job: dict,
        prompt: str,
        bind: Callable[[str], None],
        progress: Callable[[str], None],
        cancelled: threading.Event,
    ) -> LocalAnswer: ...


def resolve_binary(configured):
    candidate = shutil.which(configured)
    if candidate:
        return str(Path(candidate).resolve())
    if configured != "codex":
        raise EngineUnavailable("配置的 Codex 可执行文件不可用")
    for base in (Path("/Applications"), Path.home() / "Applications"):
        candidate = (
            base / "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        )
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise EngineUnavailable("未找到官方 Codex CLI")


class SharedCodexEngine:
    """Run official Codex against the same host state as the desktop client."""

    def __init__(self, settings):
        self.settings = settings
        self.root = directory(settings.data_dir / "clawbot-projects" / "engine")
        self.tmp = directory(self.root / "tmp")
        self._checked_at, self._state = 0.0, "not_checked"
        self._check_lock = threading.Lock()

    def environment(self):
        allowed = {
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "SHELL",
            "LANG",
            "LC_ALL",
            "TZ",
            "SYSTEMROOT",
            "COMSPEC",
            "PATHEXT",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(TMPDIR=str(self.tmp), DO_NOT_TRACK="1", LANGSMITH_TRACING="false")
        # The native Codex host owns login, sessions and memories. Never copy
        # authentication into the WeChat workspace or create another Codex home.
        env.pop("CODEX_HOME", None)
        native_home = os.environ.get("CODEX_HOME")
        if native_home:
            home = Path(native_home).expanduser().resolve(strict=True)
            if home == self.settings.data_dir or self.settings.data_dir in home.parents:
                raise ValueError("Codex 原生状态目录不能位于微信工作区")
            env["CODEX_HOME"] = str(home)
        return env

    def readiness(self):
        with self._check_lock:
            if time.monotonic() - self._checked_at < 5:
                return self._state
            try:
                result = subprocess.run(
                    [resolve_binary(self.settings.codex_binary), "login", "status"],
                    env=self.environment(),
                    cwd=self.root,
                    capture_output=True,
                    timeout=10,
                )
                self._state = "ready" if result.returncode == 0 else "login_required"
            except EngineUnavailable:
                self._state = "binary_missing"
            except (OSError, subprocess.TimeoutExpired):
                self._state = "check_failed"
            self._checked_at = time.monotonic()
            return self._state

    def subscription_auth(self):
        """Fail closed for scheduled work if the CLI is using API-key billing."""
        try:
            result = subprocess.run(
                [resolve_binary(self.settings.codex_binary), "login", "status"],
                env=self.environment(), cwd=self.root, capture_output=True, timeout=10,
            )
        except (EngineUnavailable, OSError, subprocess.TimeoutExpired):
            return False
        status = result.stdout + result.stderr
        return result.returncode == 0 and b"Logged in using ChatGPT" in status

    def command(self, job, output, schema):
        command = [
            resolve_binary(self.settings.codex_binary),
            "--cd",
            job["project_directory"],
        ]
        command += (
            ["--sandbox", "read-only"]
            if job.get("kind") in ("schedule_request", "intent_classification")
            else ["--approve-for-me"]
        )
        memories = "false" if job.get("kind") == "intent_classification" else "true"
        command += [
            "-c",
            'web_search="disabled"',
            "-c",
            f"features.memories={memories}",
            "-c",
            f"memories.use_memories={memories}",
            "-c",
            f"memories.generate_memories={memories}",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--output-last-message",
            str(output),
            "--output-schema",
            str(schema),
        ]
        if job.get("thread_id"):
            command += ["resume", job["thread_id"]]
        return command + ["-"]

    def run(self, job, prompt, bind, progress, cancelled):
        if self.readiness() != "ready":
            raise EngineUnavailable("工作区 Codex 尚未登录或不可用")
        if not re.fullmatch(r"J\d{6,}", job["job_id"]):
            raise ValueError("工作单编号无效")
        work = directory(self.root / "jobs" / job["job_id"])
        output, schema = work / "answer.json", work / "answer-schema.json"
        for path in (output, schema, work / "prompt.txt", work / "stderr.log"):
            if path.is_symlink():
                raise ValueError("执行产物不能是符号链接")
        if job.get("resume_only"):
            if not job.get("thread_id"):
                raise ExecutionUncertain("没有已绑定会话可恢复")
            try:
                text = self.read_answer(output)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise ExecutionUncertain("尚无可恢复的完整结果；不会重新提交同一问题") from exc
            bind(job["thread_id"])
            return LocalAnswer(job["thread_id"], text)
        output.unlink(missing_ok=True)
        schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"reply": {"type": "string"}},
                    "required": ["reply"],
                }
            )
        )
        (work / "prompt.txt").write_text(prompt)
        events = queue.Queue(maxsize=128)
        process = None
        error_stream = (work / "stderr.log").open("wb")
        try:
            try:
                process = subprocess.Popen(
                    self.command(job, output, schema),
                    cwd=job["project_directory"],
                    env=self.environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=error_stream,
                    start_new_session=True,
                )
            except OSError as exc:
                raise EngineUnavailable("Codex 进程未启动，未提交问题") from exc

            def write_input():
                try:
                    process.stdin.write(prompt.encode())
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

            def read_events():
                try:
                    while True:
                        line = process.stdout.readline(1024 * 1024 + 1)
                        if not line:
                            break
                        if len(line) > 1024 * 1024:
                            events.put({"oversized": True})
                            break
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(event, dict) and event.get("type") in {
                            "thread.started",
                            "turn.completed",
                            "turn.failed",
                            "error",
                        }:
                            events.put(event)
                finally:
                    events.put(None)

            writer = threading.Thread(target=write_input, daemon=True)
            reader = threading.Thread(target=read_events, daemon=True)
            writer.start()
            reader.start()
            thread_id = job.get("thread_id")
            deadline = time.monotonic() + self.settings.execution_timeout_seconds
            ended = False
            while not ended or process.poll() is None:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise ExecutionUncertain("执行中断或超时，提交结果待人工核对")
                try:
                    event = events.get(timeout=0.2)
                except queue.Empty:
                    event = {}
                if event is None:
                    ended = True
                    continue
                if event.get("oversized"):
                    raise ExecutionUncertain("模型事件超过限制，结果待核对")
                if event.get("type") == "thread.started":
                    received = event.get("thread_id")
                    if not isinstance(received, str) or not received or len(received) > 200:
                        raise ExecutionUncertain("未取得可靠会话标识")
                    if thread_id and thread_id != received:
                        raise ExecutionUncertain("执行会话与任务绑定不符")
                    thread_id = received
                    bind(thread_id)
                    progress("started")
            if process.wait() != 0 or not thread_id:
                raise ExecutionUncertain("Codex 未返回可确认的结果，请人工核对")
            try:
                text = self.read_answer(output)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise ExecutionUncertain("结果格式未通过校验，请人工核对") from exc
            return LocalAnswer(thread_id, text)
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if process.stdout:
                    process.stdout.close()
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
            error_stream.close()
            # Error text stays private, bounded and is never forwarded to ClawBot.
            error_path = work / "stderr.log"
            if error_path.stat().st_size > 12000:
                with error_path.open("rb") as stream:
                    stream.seek(-12000, os.SEEK_END)
                    tail = stream.read()
                error_path.write_bytes(tail)

    @staticmethod
    def read_answer(path):
        result = json.loads(read_regular(path, 400000))
        text = result["reply"]
        if (
            set(result) != {"reply"}
            or not isinstance(text, str)
            or not 0 < len(text.strip()) <= 100000
        ):
            raise ValueError("invalid reply")
        return text


def create_engine(settings):
    return SharedCodexEngine(settings)

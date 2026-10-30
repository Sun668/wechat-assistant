"""Interactive setup adapter; never launches the transport or executes a model."""

import argparse
import fcntl
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from wechat_assistant.ilink_commands import run_ilink_login
from wechat_assistant.projects.cli import settings_for
from wechat_assistant.projects.local_engine import (
    EngineUnavailable,
    SharedCodexEngine,
    resolve_binary,
)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def prepare(workspace):
    settings = settings_for(workspace)
    root = settings.data_dir / "clawbot-projects"
    root.mkdir(mode=0o700, exist_ok=True)
    lock_path = settings.data_dir / "setup.lock"
    if lock_path.is_symlink():
        raise ValueError("准备锁不能是符号链接")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("此工作区已有环境准备正在运行") from None
        print(f"工作区已初始化：{settings.workspace}", flush=True)
        SharedCodexEngine(settings).environment()  # Reject workspace-scoped Codex state.
        try:
            binary = resolve_binary(settings.codex_binary)
        except EngineUnavailable:
            if settings.codex_binary != "codex":
                raise ValueError("engine.toml 指定的 Codex 不可用，请修复路径") from None
            print("安装官方 Codex CLI……", flush=True)
            with tempfile.TemporaryDirectory(dir=settings.data_dir / "tmp") as temporary:
                installer = Path(temporary) / "install.sh"
                run("curl", "--fail", "--silent", "--show-error", "--location",
                    "https://chatgpt.com/codex/install.sh", "-o", str(installer))
                run("sh", str(installer))
            binary = resolve_binary("codex")
        run(binary, "--version")
        # Use the host's normal Codex state. Do not copy or create workspace auth.
        status = subprocess.run([binary, "login", "status"], capture_output=True, text=True)
        if status.returncode != 0:
            print("请在浏览器中完成 Codex 的 ChatGPT 登录。", flush=True)
            run(binary, "login")
            status = subprocess.run([binary, "login", "status"], capture_output=True, text=True)
        if status.returncode != 0 or "Logged in using ChatGPT" not in status.stdout + status.stderr:
            raise ValueError("需要 ChatGPT 登录；请核对 codex login status 后重新准备")
        print("Codex 登录检查通过。", flush=True)
        if settings.owner_id != "self":
            print("当前工作区已有微信绑定，跳过扫码。", flush=True)
        else:
            args = argparse.Namespace(check=False, qr_only=True, verify_code=False)
            run_ilink_login(settings, args)
            qr = settings.data_dir / "credentials" / "ilink-login-qr.png"
            print("请用手机微信扫描打开的二维码并确认绑定。", flush=True)
            opened = subprocess.run(["open", str(qr)])
            if opened.returncode:
                print(f"图片未能打开，请手动查看：{qr}", flush=True)
            args.check, args.qr_only = True, False
            run_ilink_login(settings, args)
        if settings_for(workspace).owner_id == "self":
            raise ValueError("微信绑定尚未完成")
        print("\n环境准备完成。尚未启动收发服务。\n"
              "首次使用还需导入客户端的本地项目列表；随后双击 start-wechat-assistant.command。",
              flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        prepare(sys.argv[1])
    except KeyboardInterrupt:
        print("\n准备已取消。", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Never dump credential objects, HTTP headers or login subprocess output.
        from wechat_assistant.models import NotSent

        message = str(exc) if isinstance(exc, (ValueError, NotSent)) else type(exc).__name__
        print(f"环境准备失败：{message}", file=sys.stderr)
        sys.exit(1)

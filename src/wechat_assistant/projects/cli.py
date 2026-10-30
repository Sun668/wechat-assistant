"""ClawBot project entry point; scoped runtime and explicit model backend."""

import argparse
import json
import os
import tomllib
from pathlib import Path

import httpx

from ..config import Settings
from .storage import ProjectStore, directory


def settings_for(workspace):
    workspace = Path(workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("工作区必须是目录")
    data = directory(workspace / ".wechat-assistant")
    ignore = data / ".gitignore"
    if ignore.is_symlink():
        raise ValueError("工作区忽略配置不能是符号链接")
    if not ignore.exists() or ignore.read_text() != "*\n":
        ignore.write_text("*\n")
    credential = data / "credentials" / "ilink.json"
    owner = "self"
    if credential.exists():
        if credential.is_symlink() or credential.parent.is_symlink():
            raise ValueError("ClawBot 凭证不能是符号链接")
        owner = json.loads(credential.read_text())["ilink_user_id"]
    options = {}
    config_path = data / "clawbot-projects" / "engine.toml"
    if config_path.exists():
        if config_path.is_symlink():
            raise ValueError("执行配置不能是符号链接")
        options = tomllib.loads(config_path.read_text())
        if set(options) - {
            "execution_backend",
            "codex_binary",
            "execution_timeout_seconds",
        }:
            raise ValueError("执行配置包含不支持的字段")
    return Settings(
        workspace=workspace,
        data_dir=data,
        owner_id=owner,
        **options,
    )


def main():
    parser = argparse.ArgumentParser(description="ClawBot 项目入口：先选项目，再对话和传文件")
    parser.add_argument("--workspace", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("doctor")
    commands.add_parser("engine-status", help="检查 Codex 本地执行入口与登录状态")
    fire = commands.add_parser("schedule-fire", help=argparse.SUPPRESS)
    fire.add_argument("plan_id")
    commands.add_parser(
        "verify-local", help="真实验证本地文件读取、引用和同一会话续聊，不发 ClawBot 消息"
    )
    login = commands.add_parser("ilink-login")
    login.add_argument("--qr-only", action="store_true")
    login.add_argument("--check", action="store_true")
    login.add_argument("--verify-code", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--port", type=int, default=8765)
    service = commands.add_parser("service-start", help="启动独立后台守护进程，不依赖临时终端会话")
    service.add_argument("--port", type=int, default=8765)
    service.add_argument(
        "--launchd", action="store_true", help="使用 launchd；需单独具有文稿目录访问权限"
    )
    stop = commands.add_parser("service-stop", help="停止当前工作区的常驻服务")
    stop.add_argument("--launchd", action="store_true")
    service_status = commands.add_parser("service-status", help="查看当前工作区的守护进程状态")
    service_status.add_argument("--launchd", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--port", type=int, default=8765)
    imp = commands.add_parser(
        "projects-import", help="导入客户端 list_projects 的 JSON，登记本地资料项目与项目名称"
    )
    imp.add_argument("file")
    imp.add_argument("--port", type=int, default=8765)
    imp.add_argument("--offline", action="store_true", help="仅在服务停止时导入")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        settings = settings_for(args.workspace)
        if args.command == "schedule-fire":
            from .local_schedules import LocalScheduleBridge

            if settings.owner_id == "self":
                raise ValueError("ClawBot 尚未绑定所有者")
            store = ProjectStore(settings.data_dir, settings.owner_id, settings.file_max_bytes)
            try:
                result = LocalScheduleBridge(store, settings).fire(args.plan_id)
            finally:
                store.close()
            print(result)
            return
        if args.command.startswith("service-"):
            from .service import AppBackgroundService, MacService

            service = (
                MacService(settings)
                if getattr(args, "launchd", False)
                else AppBackgroundService(settings)
            )
            if args.command == "service-start":
                result = service.start(args.port)
            elif args.command == "service-stop":
                result = service.stop()
            else:
                result = service.status()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "verify-local":
            from .verification import verify_local

            print(json.dumps(verify_local(settings), ensure_ascii=False, indent=2))
            return
        if args.command == "init":
            from .catalog import refresh_initial_catalog

            directory(settings.data_dir / "clawbot-projects")
            print(f"ClawBot 项目工作区：{settings.workspace}\n不启用微信监听或本地模型。")
            print(refresh_initial_catalog(settings))
            return
        if args.command in ("doctor", "engine-status"):
            from .local_engine import create_engine

            engine = create_engine(settings)
            print(
                json.dumps(
                    {
                        "clawbot_bound": settings.owner_id != "self",
                        "wechat_listener": False,
                        "execution_backend": settings.execution_backend,
                        "local_engine": engine.readiness(),
                        "codex_state_directory": str(
                            Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
                        ),
                        "codex_cli_required": True,
                        "authentication": "native_codex_login",
                        "schedule_driver": "macos_launchd",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.command == "ilink-login":
            from ..ilink_commands import run_ilink_login

            run_ilink_login(settings, args)
            return
        if args.command == "run":
            import uvicorn

            from ..logging_setup import configure_logging
            from .api import create_app
            from .runtime import ProjectRuntime

            configure_logging(settings.data_dir)
            uvicorn.run(
                create_app(ProjectRuntime(settings, port=args.port)),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
            )
            return
        projects = []
        if args.command == "projects-import":
            payload = json.loads(Path(args.file).read_text())
            projects = payload.get("projects", []) if isinstance(payload, dict) else payload
            projects = [p for p in projects if p.get("projectKind") == "local"]
            if args.offline:
                import fcntl

                with (settings.data_dir / "runtime.lock").open("a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    store = ProjectStore(
                        settings.data_dir, settings.owner_id, settings.file_max_bytes
                    )
                    try:
                        with store.transaction():
                            for project in projects:
                                store.register(
                                    project["projectId"],
                                    project["label"],
                                    project.get("path"),
                                )
                    finally:
                        store.close()
                print(f"已导入 {len(projects)} 个项目；尚未选择当前项目。")
                return
        root = settings.data_dir / "clawbot-projects"
        operator_token = (root / "operator-token").read_text().strip()
        with httpx.Client(
            base_url=f"http://127.0.0.1:{args.port}",
            trust_env=False,
            timeout=30,
            headers={"Authorization": "Bearer " + operator_token},
        ) as client:
            if args.command == "status":
                response = client.get("/health")
                response.raise_for_status()
                print(json.dumps(response.json(), ensure_ascii=False, indent=2))
            elif args.command == "projects-import":
                for project in projects:
                    response = client.post(
                        "/projects",
                        json={
                            "project_id": project["projectId"],
                            "name": project["label"],
                            "source_path": project.get("path"),
                        },
                    )
                    response.raise_for_status()
                print(f"已刷新 {len(projects)} 个原生项目；现有任务归属不变。")
    except (ValueError, OSError, httpx.HTTPError) as exc:
        # Never print HTTP headers, URLs containing secrets, or raw credential objects.
        detail = (
            str(exc)
            if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
            else type(exc).__name__
        )
        parser.exit(2, f"操作未完成：{detail}\n")


if __name__ == "__main__":
    main()

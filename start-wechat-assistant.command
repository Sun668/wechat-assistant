#!/bin/sh
# 双击启动；也可传入其他工作区的绝对路径。
set -eu

finish() {
    result=$?
    trap - EXIT
    if [ "$result" -ne 0 ]; then
        printf '\n启动未完成，请查看上方提示。\n'
    fi
    if [ -t 0 ]; then
        printf '\n按回车关闭窗口……'
        read -r answer || true
    fi
    exit "$result"
}
trap finish EXIT

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORKSPACE_DIR=${1:-"$HOME/Documents/wechat-assistant-workspace"}
# Finder 启动终端时也能找到常见安装位置的 uv。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    printf '未找到 uv，请先安装 uv 后重新启动。\n' >&2
    exit 1
fi
if [ ! -d "$WORKSPACE_DIR" ]; then
    printf '工作区不存在：%s\n请先按 docs/installation.md 初始化工作区。\n' "$WORKSPACE_DIR" >&2
    exit 1
fi

workspace() {
    sh "$PROJECT_DIR/scripts/workspace.sh" "$WORKSPACE_DIR" "$@"
}

printf '检查工作区：%s\n' "$WORKSPACE_DIR"
workspace engine-status
printf '\n启动后台服务……\n'
workspace service-start
printf '\n等待服务就绪……\n'
attempt=0
until workspace status; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 10 ]; then
        printf '\n服务未就绪，请查看日志：%s/.wechat-assistant/clawbot-projects/service/background.log\n' "$WORKSPACE_DIR" >&2
        exit 1
    fi
    sleep 1
done
workspace service-status
printf '\n状态已显示。后台服务会在关闭窗口后继续运行。\n在已绑定的 ClawBot 聊天中发送“项目列表”，再发送“选择项目 1”。\n'

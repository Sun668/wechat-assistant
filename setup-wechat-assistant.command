#!/bin/sh
# 首次环境准备：不启动微信收发服务。
set -eu
umask 077
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
INSTALLER=""
finish() {
    result=$?
    trap - EXIT
    [ -z "$INSTALLER" ] || rm -f "$INSTALLER"
    if [ "$result" -ne 0 ]; then
        printf '\n准备未完成，请查看上方错误；修复后可重新运行。\n'
    fi
    if [ -t 0 ]; then
        printf '\n按回车关闭窗口……'
        read -r answer || true
    fi
    exit "$result"
}
trap finish EXIT
if [ "$(uname -s)" != Darwin ]; then
    printf '此准备脚本仅支持 macOS。\n' >&2
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    printf '安装 uv（官方安装器）……\n'
    INSTALLER=$(mktemp)
    curl --fail --silent --show-error --location https://astral.sh/uv/install.sh -o "$INSTALLER"
    UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$INSTALLER"
fi
uv --version
if ! CONFIG_PYTHON=$(uv python find '>=3.11,<3.14'); then
    printf '安装 Python 3.13……\n'
    uv python install 3.13
    CONFIG_PYTHON=$(uv python find '>=3.11,<3.14')
fi
if [ "$#" -gt 0 ]; then
    WORKSPACE_DIR=$1
else
    WORKSPACE_DIR=$("$CONFIG_PYTHON" "$PROJECT_DIR/scripts/read_workspace.py" "$PROJECT_DIR/Config.toml")
fi
mkdir -p "$WORKSPACE_DIR"
WORKSPACE_DIR=$(CDPATH= cd -- "$WORKSPACE_DIR" && pwd)
STATE_DIR="$WORKSPACE_DIR/.wechat-assistant"
mkdir -p "$STATE_DIR/cache/uv" "$STATE_DIR/tmp"
export UV_CACHE_DIR="$STATE_DIR/cache/uv"
export UV_PROJECT_ENVIRONMENT="$STATE_DIR/venv"
export TMPDIR="$STATE_DIR/tmp"
printf '\n安装并核对项目依赖：%s\n' "$WORKSPACE_DIR"
uv sync --project "$PROJECT_DIR" --locked --no-dev --python "$CONFIG_PYTHON"
uv pip check --python "$STATE_DIR/venv/bin/python"
"$STATE_DIR/venv/bin/python" "$PROJECT_DIR/scripts/prepare_workspace.py" "$WORKSPACE_DIR"

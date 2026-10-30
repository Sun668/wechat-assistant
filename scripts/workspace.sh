#!/bin/sh
# Usage: sh scripts/workspace.sh /absolute/workspace init|ilink-login|projects-import|run|...
# App environment, dependencies and download caches all belong to this workspace.
set -eu
if [ "$#" -lt 2 ]; then
    echo 'Usage: workspace.sh WORKSPACE COMMAND [ARGS...]' >&2
    exit 2
fi
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORKSPACE_DIR=$(CDPATH= cd -- "$1" && pwd)
shift
STATE_DIR="$WORKSPACE_DIR/.wechat-assistant"
umask 077
mkdir -p "$STATE_DIR/cache/uv" "$STATE_DIR/tmp"
export UV_CACHE_DIR="$STATE_DIR/cache/uv"
export UV_PROJECT_ENVIRONMENT="$STATE_DIR/venv"
export TMPDIR="$STATE_DIR/tmp"
exec uv run --project "$PROJECT_DIR" --locked \
    clawbot-projects --workspace "$WORKSPACE_DIR" "$@"

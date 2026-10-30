"""Read launcher configuration without application dependencies."""

import sys
import tomllib
from pathlib import Path


def read_workspace(config_path: Path) -> Path:
    with config_path.open("rb") as source:
        value = tomllib.load(source).get("workspace")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Config.toml 的 workspace 必须是非空路径字符串")
    if any(character in value for character in "\n\r\0"):
        raise ValueError("workspace 路径不能包含换行或空字符")
    workspace = Path(value).expanduser()
    if not workspace.is_absolute():
        workspace = config_path.resolve().parent / workspace
    return workspace.resolve()


if __name__ == "__main__":
    try:
        print(read_workspace(Path(sys.argv[1])))
    except (OSError, ValueError) as exc:
        print(f"无法读取工作区配置：{exc}", file=sys.stderr)
        sys.exit(1)

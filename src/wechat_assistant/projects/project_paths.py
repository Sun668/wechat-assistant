"""Local project-directory exclusions shared by import, menus and execution."""

from pathlib import Path


def is_excluded_project_path(value):
    if not value:
        return False
    try:
        path = Path(value).expanduser()
        managed = Path.home() / "Documents" / "ChatGPT"
        candidates = (
            (path.absolute(), managed.absolute()),
            (path.resolve(), managed.resolve()),
        )
    except (OSError, RuntimeError):
        return True
    # Check the displayed path and its resolved target, including directory aliases.
    for candidate, root in candidates:
        if ".chatgpt-projects" in candidate.parts or candidate == root or root in candidate.parents:
            return True
    return False

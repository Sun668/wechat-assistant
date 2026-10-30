"""ClawBot settings. There are no WeChat-monitor or automatic-reply switches."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: Path
    data_dir: Path
    owner_id: str = "self"
    file_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    execution_backend: Literal["codex_shared"] = "codex_shared"
    codex_binary: str = "codex"
    execution_timeout_seconds: float = Field(default=240, ge=1, le=1800)

    @model_validator(mode="after")
    def workspace_storage(self):
        self.workspace = self.workspace.resolve(strict=True)
        if not self.workspace.is_dir() or self.data_dir != self.workspace / ".wechat-assistant":
            raise ValueError("所有运行数据必须位于指定工作区的 .wechat-assistant")
        return self

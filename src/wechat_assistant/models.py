"""Transport records, independent of network and project storage."""

from pydantic import BaseModel, ConfigDict


class Receipt(BaseModel):
    external_id: str
    # Server acknowledgement; this is not a recipient read receipt.
    verified: bool


class FilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: str
    version: int = 1
    revision: int = 1
    name: str
    size: int
    sha256: str


class SendUncertain(RuntimeError):
    """A write may have happened. Never retry automatically."""


class NotSent(RuntimeError):
    """Adapter proved that no chat message was sent."""

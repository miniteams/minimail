"""Settings (`MINIMAIL_*` env vars or `.env`)."""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_msal_cache() -> Path:
    # Shared with miniteams: one sign-in serves both tools (FOCI family refresh token).
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "miniteams" / "msal_cache.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MINIMAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tenant_id: str
    # Teams first-party client: the account miniteams signs in with. Its Graph `.default` already
    # carries Mail.ReadWrite / Calendars.ReadWrite, so no consent is ever needed.
    client_id: str = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
    msal_cache: Path = Field(default_factory=_default_msal_cache)
    graph_url: str = "https://graph.microsoft.com/v1.0"
    data_dir: Path = Path("data")
    # Security: the only place MCP `get_attachments` writes to; unset = `<data_dir>/exports`.
    export_dir: Path | None = None
    log_level: str = "INFO"
    # `sync --live` cadences, in seconds.
    inbox_every: float = Field(default=30.0, gt=0)
    folders_every: float = Field(default=300.0, gt=0)
    tree_every: float = Field(default=900.0, gt=0)
    reconcile_every: float = Field(default=86400.0, gt=0)
    # Graph's per-app-per-mailbox quota is shared with the user's own Teams client (same app id).
    max_rps: float = Field(default=8.0, gt=0)

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def exports(self) -> Path:
        return self.export_dir or self.data_dir / "exports"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mail.db"

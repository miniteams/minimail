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
    log_level: str = "INFO"

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mail.db"

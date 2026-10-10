"""Runtime configuration, loaded from environment variables and an optional .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    plaid_client_id: str = ""
    plaid_secret: SecretStr = SecretStr("")
    plaid_env: Literal["sandbox", "production"] = "sandbox"
    plaid_country_codes: list[str] = Field(default_factory=lambda: ["US"])

    findata_data_dir: Path = Path("~/.findata")
    findata_host: str = "127.0.0.1"
    findata_port: int = 8765

    @property
    def data_dir(self) -> Path:
        return self.findata_data_dir.expanduser()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "findata.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "token.key"

    @property
    def api_token_path(self) -> Path:
        return self.data_dir / "api.token"

    def require_plaid_credentials(self) -> None:
        if not self.plaid_client_id or not self.plaid_secret.get_secret_value():
            raise RuntimeError(
                "PLAID_CLIENT_ID and PLAID_SECRET are not set. Copy .env.example to .env "
                "and add your keys from https://dashboard.plaid.com/developers/keys"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()

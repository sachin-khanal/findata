"""Where the daemon's files live, and the minimal settings a front end needs to find them.

`ClientSettings` deliberately declares no Plaid fields and does not import `findata.config`,
so front ends never load (or hold) the Plaid secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class Locations:
    data_dir: Path

    @property
    def socket(self) -> Path:
        return self.data_dir / "daemon.sock"

    @property
    def lock(self) -> Path:
        return self.data_dir / "daemon.lock"

    @property
    def pid(self) -> Path:
        return self.data_dir / "daemon.pid"

    @property
    def log(self) -> Path:
        return self.data_dir / "daemon.log"

    @property
    def port_file(self) -> Path:
        return self.data_dir / "daemon.port"

    @property
    def credentials_dir(self) -> Path:
        return self.data_dir / "credentials"

    @property
    def api_token(self) -> Path:
        return self.data_dir / "api.token"


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    findata_data_dir: Path = Path("~/.findata")
    findata_host: str = "127.0.0.1"
    findata_port: int = 8765

    @property
    def data_dir(self) -> Path:
        return self.findata_data_dir.expanduser()

    @property
    def locations(self) -> Locations:
        return Locations(self.data_dir)

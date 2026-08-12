"""Every tunable value the deployment owns.

Nothing in this project reads os.environ directly. If a value might differ between
one person's setup and another's, it belongs here so that moving to a different
InfluxDB or database is a .env edit rather than a code change.

Keep this in sync with .env.example — the docs role checks that they match.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- app ---
    secret_key: str = "dev-only-insecure-key-change-me"
    app_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # Timezone of the *CPAP machine*, not the server. .BYS files store naive local
    # wall-clock, so this is the only thing that makes their timestamps meaningful.
    device_tz: str = "Asia/Bangkok"

    # --- relational store: users, devices, ingest ledger ---
    database_url: str = "sqlite:///./data/app.db"

    # Writable directory for things that belong to this instance rather than to the
    # image: the SQLite file and the setup token. Mounted as a volume in compose.
    data_dir: str = "./data"

    # Seed account, applied only while no accounts exist. Setting both lets a
    # deployment come up ready to sign in to, with no token to copy and no /setup
    # page. Changing them afterwards does nothing — see the warning in main.py.
    init_username: str = ""
    init_password: str = ""

    # --- InfluxDB ---
    influx_url: str = "http://influxdb:8086"
    influx_token: str = ""
    influx_org: str = "pap"
    influx_bucket: str = "pap"

    # --- upload limits ---
    max_upload_mb: int = 64
    max_files_per_request: int = 200
    max_zip_uncompressed_mb: int = 256
    max_zip_entries: int = 5000

    # --- links shown in the UI ---
    grafana_url: str = "http://localhost:3000"

    @field_validator("device_tz")
    @classmethod
    def _tz_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"DEVICE_TZ={value!r} is not a known timezone; use an IANA name "
                f"such as Asia/Bangkok"
            ) from exc
        return value

    @property
    def device_zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.device_tz)

    @property
    def cookies_secure(self) -> bool:
        """Only mark session cookies Secure when the app is actually served over TLS,
        otherwise a plain-http deployment silently loses its login."""
        return self.app_base_url.lower().startswith("https://")

    @property
    def setup_token_path(self) -> Path:
        # Resolved so the log prints a path that can be pasted into a command; inside
        # the container the working directory is not where the reader is standing.
        return (Path(self.data_dir) / "setup-token").resolve()

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_zip_uncompressed_bytes(self) -> int:
        return self.max_zip_uncompressed_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()

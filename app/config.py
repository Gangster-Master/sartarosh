"""Runtime configuration (ARCHITECTURE.md §5.1).

Business values live in the ``settings`` table (app/services/settings.py); only
deployment values are read from the environment / ``.env``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CONSENT_VERSION: int = 1  # bump when the privacy text changes -> users are asked again
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    bot_token: SecretStr | None = None  # BOT_TOKEN; required by main.py only
    database_url: str = "sqlite+aiosqlite:///data/bot.db"  # relative -> PROJECT_ROOT
    data_dir: Path = Path("data")  # relative -> PROJECT_ROOT
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")
    shop_tz: str = "Asia/Tashkent"  # SHOP_TZ, never TZ
    default_lang: str = "uz"
    log_level: str = "INFO"
    redis_url: str | None = None
    scheduler_tick_seconds: int = 30
    throttle_per_minute: int = 30
    db_echo: bool = False

    @property
    def invalid_admin_ids(self) -> list[str]:
        """ADMIN_IDS entries that are not integers (logged at startup)."""
        bad = []
        for part in self.admin_ids_raw.replace(";", ",").replace(" ", ",").split(","):
            part = part.strip()
            if part:
                try:
                    int(part)
                except ValueError:
                    bad.append(part)
        return bad

    @property
    def admin_ids(self) -> frozenset[int]:
        out: set[int] = set()
        for part in self.admin_ids_raw.replace(";", ",").replace(" ", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except ValueError:
                continue
        return frozenset(out)

    @property
    def data_path(self) -> Path:
        """``data_dir`` resolved against PROJECT_ROOT and created if missing."""
        p = self.data_dir if self.data_dir.is_absolute() else PROJECT_ROOT / self.data_dir
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_config() -> Config:
    return Config()

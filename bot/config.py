from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_TZ = "Asia/Phnom_Penh"
DEFAULT_REMIND_AT = "21:00"
DEFAULT_CURRENCY = "USD"
DEFAULT_KHR_RATE = 4100
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    allowed_user_ids: frozenset[int]
    tz: str


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return frozenset(ids)


def load_settings() -> Settings:
    load_dotenv(_ENV_FILE)
    token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "sqlite:///data.db").strip()
    tz = os.getenv("TZ", DEFAULT_TZ).strip() or DEFAULT_TZ
    allowed = _parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""))
    return Settings(
        bot_token=token,
        database_url=database_url,
        allowed_user_ids=allowed,
        tz=tz,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()

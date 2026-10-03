"""Load the bot's .env before modules read their settings."""

import os
import math
from pathlib import Path

from dotenv import load_dotenv


APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env", override=True)
SESSION_DIR = APP_DIR / "data"


def env_ids(name: str) -> set[int]:
    """Read comma-separated Telegram IDs; blank means no access."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    try:
        return {int(part.strip()) for part in raw.split(",")}
    except ValueError:
        raise ValueError(f"{name} must contain comma-separated numeric IDs") from None


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def telegram_credentials() -> tuple[str, int, str]:
    """Validate credentials before creating a Telegram client."""
    values = {name: os.getenv(name, "").strip()
              for name in ("BOT_TOKEN", "API_ID", "API_HASH")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"Missing required .env settings: {', '.join(missing)}")
    return values["BOT_TOKEN"], env_int("API_ID", 0), values["API_HASH"]


def env_interval() -> float:
    raw = os.getenv("PROGRESS_UPDATE_INTERVAL", "").strip() or "8.0"
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("PROGRESS_UPDATE_INTERVAL must be a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("PROGRESS_UPDATE_INTERVAL must be a positive number")
    return value


ALLOWED_CHATS = env_ids("ALLOWED_CHATS")
ADMIN_IDS = env_ids("ADMIN_IDS")
MAX_CONCURRENT_JOBS = env_int("MAX_CONCURRENT_JOBS", 1)
PROGRESS_UPDATE_INTERVAL = env_interval()

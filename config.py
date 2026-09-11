"""Config via env."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    poll_interval: float = 10.0
    max_trackings_per_chat: int = 5
    live_period: int = 86400  # max Telegram, biar minim renew
    min_move_meters: float = 15.0
    request_timeout: float = 12.0


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN kosong. Isi di .env (lihat .env.example).")
    return Config(
        bot_token=token,
        poll_interval=float(os.getenv("POLL_INTERVAL", "10")),
        max_trackings_per_chat=int(os.getenv("MAX_TRACKINGS_PER_CHAT", "5")),
        live_period=int(os.getenv("LIVE_PERIOD", "86400")),
        min_move_meters=float(os.getenv("MIN_MOVE_METERS", "15")),
    )

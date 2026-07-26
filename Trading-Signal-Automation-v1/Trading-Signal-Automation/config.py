"""Central configuration — all settings and secret-loading in one place. Secrets live in .env."""
import os
from dotenv import load_dotenv

# Default is the production .env (auto-discovered). SIGNAL_ENV_FILE points at an alternate file
# (e.g. .env.test) for an isolated test run; we load it EXPLICITLY and fail loudly if it is
# missing, rather than silently falling back to the production .env.
_env_file = os.environ.get("SIGNAL_ENV_FILE")
if _env_file:
    if not os.path.exists(_env_file):
        raise FileNotFoundError(
            f"SIGNAL_ENV_FILE={_env_file!r} was set but that file does not exist. "
            "Refusing to fall back to the production .env."
        )
    load_dotenv(_env_file, override=True)
else:
    load_dotenv()

# --- Secrets (loaded from .env) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# taapi keys in priority order; the backup is used only when the primary fails (e.g. runs out
# of daily credit). With no backup set, TAAPI_SECRETS is just [primary] and nothing changes.
TAAPI_SECRET = os.environ.get("TAAPI_SECRET")
TAAPI_SECRET_BACKUP = os.environ.get("TAAPI_SECRET_BACKUP")
TAAPI_SECRETS = [s for s in (TAAPI_SECRET, TAAPI_SECRET_BACKUP) if s]

# --- Strategy settings ---
TIMEFRAME = "15m"   # candle size
FAST_MA = 9         # fast EMA length
SLOW_MA = 21        # slow EMA length

# Instruments to monitor.
# `fallback_symbol` is the SAME pair on Binance's keyless data host, used if taapi is down --
# same exchange + symbol is what makes the fallback safe.
INSTRUMENTS = [
    {
        "name": "BTC/USDT",
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "fallback_symbol": "BTCUSDT",
    },
]

# taapi's free tier allows ~1 request every 15 seconds; looping over INSTRUMENTS must respect
# this or it starts getting 429s.
TAAPI_MIN_SECONDS_BETWEEN_REQUESTS = 15

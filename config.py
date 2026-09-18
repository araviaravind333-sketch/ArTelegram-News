"""Central configuration.

Every secret is read from the process environment via ``os.getenv`` only.
Nothing is ever hardcoded, and nothing is ever logged in full: use
``masked()`` when you need to show a value for debugging.

Local development may place values in a ``.env`` file (git-ignored); it is
loaded by :func:`load_dotenv` below without any third-party dependency.
GitHub Actions injects the same names from repository Secrets.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import pytz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC

#: Canonical category order. The first entry is synthesised by the analyzer
#: from the highest-scoring items across every other bucket.
CATEGORIES: tuple[str, ...] = (
    "High-Virality Instagram Picks",
    "India News",
    "World News",
    "Business News",
    "Sports News",
    "Technology News",
    "Health News",
    "Unreported News",
    "Current Affairs",
)

#: Categories the classifier may assign to a raw article (i.e. every category
#: except the synthesised picks bucket).
ASSIGNABLE_CATEGORIES: tuple[str, ...] = CATEGORIES[1:]


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or malformed."""


# ---------------------------------------------------------------------------
# .env loading (local dev only - CI uses real environment variables)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path | None = None) -> None:
    """Populate ``os.environ`` from a ``.env`` file if one exists.

    Existing environment variables always win, so CI secrets are never
    shadowed by a stale checked-out file.
    """
    env_path = path or (BASE_DIR / ".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------

def get(name: str, default: str | None = None) -> str | None:
    """Return an environment variable, treating empty strings as unset."""
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def get_int(name: str, default: int) -> int:
    value = get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:  # pragma: no cover - operator error
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def get_bool(name: str, default: bool = False) -> bool:
    value = get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def require(name: str) -> str:
    """Return a mandatory environment variable or raise :class:`ConfigError`."""
    value = get(name)
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name!r}. "
            f"Set it as a GitHub Secret (Settings -> Secrets and variables -> "
            f"Actions) or in a local .env file."
        )
    return value


def require_all(names: Iterable[str]) -> dict[str, str]:
    """Validate a group of variables, reporting *all* missing names at once."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = get(name)
        if value:
            resolved[name] = value
        else:
            missing.append(name)
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(sorted(missing))
            + ". Configure them as GitHub Secrets or in a local .env file."
        )
    return resolved


def masked(value: str | None, keep: int = 4) -> str:
    """Render a secret safe for logs: ``123456...ab12`` -> ``1234...ab12``."""
    if not value:
        return "<unset>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 6}{value[-keep:]}"


# ---------------------------------------------------------------------------
# Secret names (grouped so each workflow validates only what it needs)
# ---------------------------------------------------------------------------

TELEGRAM_SECRETS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
INSTAGRAM_SECRETS = ("INSTA_ACCESS_TOKEN", "INSTA_USER_ID")


def telegram_credentials() -> tuple[str, str]:
    creds = require_all(TELEGRAM_SECRETS)
    return creds["TELEGRAM_BOT_TOKEN"], creds["TELEGRAM_CHAT_ID"]


def instagram_credentials() -> tuple[str, str]:
    creds = require_all(INSTAGRAM_SECRETS)
    return creds["INSTA_ACCESS_TOKEN"], creds["INSTA_USER_ID"]


def anthropic_api_key() -> str | None:
    """Optional. When absent the deterministic engine is used instead."""
    return get("ANTHROPIC_API_KEY")


def youtube_api_key() -> str | None:
    """Optional. When absent, Top Picks ranking skips the YouTube-trending
    signal and falls back to Instagram history + corroboration alone."""
    return get("YOUTUBE_API_KEY")


# ---------------------------------------------------------------------------
# Tunables (safe defaults; overridable via env, none of them secret)
# ---------------------------------------------------------------------------

GRAPH_API_VERSION = get("GRAPH_API_VERSION", "v21.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

CLAUDE_MODEL = get("CLAUDE_MODEL", "claude-opus-5")

MIN_ITEMS_PER_CATEGORY = get_int("MIN_ITEMS_PER_CATEGORY", 7)
#: Hard ceiling per category. Without it a 24h scan produces 800+ rows, which
#: is a data dump rather than an editorial briefing. Only the highest-scoring
#: stories survive the cut.
MAX_ITEMS_PER_CATEGORY = get_int("MAX_ITEMS_PER_CATEGORY", 10)
#: The "High-Virality Instagram Picks" summary section must clear this many
#: stories on its own, drawn from across all 8 categories.
MIN_PICKS = get_int("MIN_PICKS", 10)
REELS_AUDIT_COUNT = get_int("REELS_AUDIT_COUNT", 10)

FEED_TIMEOUT_SECONDS = get_int("FEED_TIMEOUT_SECONDS", 20)
FEED_MAX_WORKERS = get_int("FEED_MAX_WORKERS", 12)
FEED_ENTRIES_PER_SOURCE = get_int("FEED_ENTRIES_PER_SOURCE", 60)

DEFAULT_SCAN_HOURS = get_int("DEFAULT_SCAN_HOURS", 24)
FAST_SCAN_MINUTES = get_int("FAST_SCAN_MINUTES", 60)

# --- Hourly news pulse ------------------------------------------------------
#: How often the pulse workflow runs. Must divide 60 evenly for a stable cron.
PULSE_MINUTES = get_int("PULSE_MINUTES", 60)
#: Feeds lag: a story published at 12:50 may not appear in the RSS until
#: 13:05. The pulse therefore looks back further than its own interval and
#: relies on the seen-store, not the window, to decide what is genuinely new.
PULSE_LOOKBACK_MINUTES = get_int("PULSE_LOOKBACK_MINUTES", 90)
#: Most stories to put in a single pulse message, highest score first. Raised
#: from the 20-minute cadence's default of 6, since an hour accumulates more
#: genuinely new stories; anything scoring high enough but still over this
#: cap rolls into the next hour's pulse rather than being dropped.
PULSE_MAX_ITEMS = get_int("PULSE_MAX_ITEMS", 10)
#: Stories scoring below this are held back rather than posted as filler.
PULSE_MIN_SCORE = get_int("PULSE_MIN_SCORE", 45)
#: How long a story stays in the seen-store before it may resurface.
SEEN_TTL_HOURS = get_int("SEEN_TTL_HOURS", 48)

OUTPUT_DIR = BASE_DIR / (get("OUTPUT_DIR", "output") or "output")
DATA_DIR = BASE_DIR / "data"
BENCHMARK_FILE = DATA_DIR / "benchmarks.json"
#: Ledger of stories already posted, so the hourly pulse never repeats one.
SEEN_FILE = DATA_DIR / "seen.json"

#: A browser User-Agent is required, not cosmetic: several publishers (PIB
#: India among them) return 403 to an obvious bot string while serving the
#: same public RSS to a normal browser UA.
USER_AGENT = get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

#: Telegram long-poll budget for the listener workflow, in seconds.
LISTENER_RUNTIME_SECONDS = get_int("LISTENER_RUNTIME_SECONDS", 260)
LISTENER_POLL_TIMEOUT = get_int("LISTENER_POLL_TIMEOUT", 25)


def ensure_directories() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

"""Cross-run memory of stories already posted to Telegram.

Each scan run is a fresh process, so without a persisted store the 20-minute
pulse would re-post the same stories every time. This module keeps a small
JSON ledger of what has already gone out, keyed two ways per story:

* the canonical URL, and
* the normalised title fingerprint,

so a story that resurfaces under a different URL (a syndicated copy, an
updated permalink, a Google News redirect vs. the publisher's own link) is
still recognised as already seen.

Entries older than the TTL are pruned on every load, which keeps the file
small and lets a genuinely developing story resurface after a couple of days
rather than being suppressed forever.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import config
from scrapers.rss_collector import NewsItem, canonical_url, title_fingerprint

log = logging.getLogger(__name__)


def _keys(item: NewsItem) -> list[str]:
    """Every identity a story might reappear under."""
    keys = []
    url = canonical_url(item.link)
    if url:
        keys.append(f"u:{url}")
    fingerprint = title_fingerprint(item.title)
    if fingerprint:
        keys.append(f"t:{fingerprint}")
    return keys


def load(ttl_hours: int | None = None) -> dict[str, str]:
    """Load the ledger, dropping anything past its TTL."""
    ttl = ttl_hours or config.SEEN_TTL_HOURS
    path = config.SEEN_FILE
    if not path.is_file():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Seen-store unreadable (%s); starting fresh: %s", path, exc)
        return {}

    if not isinstance(raw, dict):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl)
    store: dict[str, str] = {}
    for key, stamp in raw.items():
        if not isinstance(key, str) or not isinstance(stamp, str):
            continue
        try:
            seen_at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        if seen_at >= cutoff:
            store[key] = stamp

    log.info(
        "Seen-store loaded: %d live entries (%d expired and pruned)",
        len(store), len(raw) - len(store),
    )
    return store


def filter_new(items: list[NewsItem], store: dict[str, str]) -> list[NewsItem]:
    """Return only the stories not already recorded in ``store``.

    Also de-duplicates *within* the batch, so two copies of the same story
    arriving in one run cannot both be posted.
    """
    fresh: list[NewsItem] = []
    batch_keys: set[str] = set()

    for item in items:
        keys = _keys(item)
        if any(k in store for k in keys) or any(k in batch_keys for k in keys):
            continue
        batch_keys.update(keys)
        fresh.append(item)

    log.info("New since last run: %d of %d stories", len(fresh), len(items))
    return fresh


def mark(items: list[NewsItem], store: dict[str, str]) -> dict[str, str]:
    """Record stories as posted. Call this only after delivery succeeds."""
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        for key in _keys(item):
            store[key] = now
    return store


def save(store: dict[str, str]) -> None:
    config.ensure_directories()
    config.SEEN_FILE.write_text(
        json.dumps(store, indent=0, sort_keys=True), encoding="utf-8"
    )
    log.info("Seen-store saved: %d entries", len(store))

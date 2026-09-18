"""Free "what's trending on YouTube" signal for Top Picks ranking.

Uses the YouTube Data API v3's public ``videos.list?chart=mostPopular``
endpoint - one quota unit per call against a 10,000-unit/day free allowance,
needing only a Google Cloud API key (no OAuth, no paid tier). Entirely
optional: without ``YOUTUBE_API_KEY``, this module returns no signal and
Top Picks ranking falls back to Instagram history + cross-outlet
corroboration alone, exactly as before this feature existed.

X/Twitter has no equivalent here. Its free API tier is write-only - reading
trends or running a search requires a paid plan - so there is no free way to
build the same signal for Twitter, and this module does not attempt to fake
one.
"""

from __future__ import annotations

import logging
import re
import time

import requests

import config

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/youtube/v3/videos"

#: Noise words specific to video titles, on top of the generic stopwords
#: already used elsewhere - these would otherwise pollute every trending
#: topic's token set and make matching meaningless.
_TITLE_NOISE = frozenset(
    """video full official trailer song movie review shorts live show
    episode part season vlog watch subscribe channel new latest today
    2025 2026 hindi tamil telugu english""".split()
)

#: Per-region cache: region -> (fetched_at_monotonic, topics). A trending
#: list barely changes minute to minute, so one fetch comfortably serves an
#: entire scan or pulse run, and repeated runs within the TTL window reuse it
#: without spending additional quota.
_cache: dict[str, tuple[float, tuple[frozenset[str], ...]]] = {}
CACHE_TTL_SECONDS = 1800  # 30 minutes


def _significant_tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if len(w) > 3 and w not in _TITLE_NOISE)


def fetch_trending_topics(
    region: str = "IN", max_results: int = 50
) -> tuple[frozenset[str], ...]:
    """Return one token set per currently-trending video's title.

    Kept as a tuple of per-video sets (not one merged bag of words) so a
    match later means "this story shares real overlap with *one specific*
    trending video's title", not "this story contains a common word that
    happens to appear somewhere across 50 unrelated videos".

    Never raises: any failure (missing key, network error, quota exceeded,
    bad response) logs a warning and returns an empty tuple, so a broken or
    absent YouTube integration only removes a ranking signal - it never
    breaks a scan.
    """
    api_key = config.get("YOUTUBE_API_KEY")
    if not api_key:
        return ()

    cached = _cache.get(region)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        response = requests.get(
            API_URL,
            params={
                "part": "snippet",
                "chart": "mostPopular",
                "regionCode": region,
                "maxResults": max_results,
                "key": api_key,
            },
            timeout=15,
        )
        payload = response.json()
    except requests.RequestException as exc:
        log.warning("YouTube trending fetch failed: %s", exc)
        return ()
    except ValueError as exc:
        log.warning("YouTube trending response unparsable: %s", exc)
        return ()

    if "error" in payload:
        error = payload["error"]
        log.warning(
            "YouTube API error %s: %s",
            error.get("code"), error.get("message", "unknown"),
        )
        return ()

    topics = tuple(
        tokens
        for item in payload.get("items", [])
        if (tokens := _significant_tokens(item.get("snippet", {}).get("title", "")))
    )
    _cache[region] = (time.monotonic(), topics)
    log.info(
        "YouTube trending (%s): %d video titles tokenised for matching",
        region, len(topics),
    )
    return topics

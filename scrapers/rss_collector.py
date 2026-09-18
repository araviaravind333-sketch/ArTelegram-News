"""Free-tier news aggregation layer.

Fetches a registry of 25+ no-cost RSS/Atom feeds (Google News topic and query
feeds, Reuters, PIB India, WHO, UN and more), normalises every entry into a
:class:`NewsItem`, removes duplicates across sources, and filters by a
publication-timestamp window.

No paid news API is used anywhere in this module.
"""

from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse, urlunparse

import feedparser
import requests

import config

log = logging.getLogger(__name__)

GOOGLE_NEWS = "https://news.google.com/rss"
IN_LOCALE = "hl=en-IN&gl=IN&ceid=IN:en"
US_LOCALE = "hl=en-US&gl=US&ceid=US:en"


# ---------------------------------------------------------------------------
# Feed registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeedSource:
    """A single free RSS endpoint."""

    name: str
    url: str
    category_hint: str | None = None
    #: 1.0 = wire service / government primary source, 0.72 = soft aggregator.
    authority: float = 0.8


FEEDS: tuple[FeedSource, ...] = (
    # --- Google News topic feeds ------------------------------------------
    FeedSource(
        "Google News - India Top Stories",
        f"{GOOGLE_NEWS}?{IN_LOCALE}",
        "India News",
        0.80,
    ),
    FeedSource(
        "Google News - India Nation",
        f"{GOOGLE_NEWS}/headlines/section/topic/NATION?{IN_LOCALE}",
        "India News",
        0.80,
    ),
    FeedSource(
        "Google News - World",
        f"{GOOGLE_NEWS}/headlines/section/topic/WORLD?{IN_LOCALE}",
        "World News",
        0.80,
    ),
    FeedSource(
        "Google News - World (US edition)",
        f"{GOOGLE_NEWS}/headlines/section/topic/WORLD?{US_LOCALE}",
        "World News",
        0.78,
    ),
    FeedSource(
        "Google News - Business",
        f"{GOOGLE_NEWS}/headlines/section/topic/BUSINESS?{IN_LOCALE}",
        "Business & Finance News",
        0.80,
    ),
    FeedSource(
        "Google News - Technology",
        f"{GOOGLE_NEWS}/headlines/section/topic/TECHNOLOGY?{IN_LOCALE}",
        "Business & Finance News",
        0.78,
    ),
    FeedSource(
        "Google News - Health",
        f"{GOOGLE_NEWS}/headlines/section/topic/HEALTH?{IN_LOCALE}",
        "Health & Wellness News",
        0.80,
    ),
    FeedSource(
        "Google News - Science",
        f"{GOOGLE_NEWS}/headlines/section/topic/SCIENCE?{IN_LOCALE}",
        "Uncovered & Shocking News",
        0.78,
    ),
    FeedSource(
        "Google News - Sports",
        f"{GOOGLE_NEWS}/headlines/section/topic/SPORTS?{IN_LOCALE}",
        "Sports News",
        0.80,
    ),
    FeedSource(
        "Google News - Entertainment",
        f"{GOOGLE_NEWS}/headlines/section/topic/ENTERTAINMENT?{IN_LOCALE}",
        None,
        0.72,
    ),

    # --- Google News query feeds (topical depth, still free) --------------
    FeedSource(
        "Google News - Indian Policy & Parliament",
        f"{GOOGLE_NEWS}/search?q=India+policy+OR+parliament+OR+cabinet+when:2d&{IN_LOCALE}",
        "Current Affairs & Policy",
        0.78,
    ),
    FeedSource(
        "Google News - Supreme Court & Law",
        f"{GOOGLE_NEWS}/search?q=Supreme+Court+India+OR+High+Court+verdict+when:2d&{IN_LOCALE}",
        "Current Affairs & Policy",
        0.78,
    ),
    FeedSource(
        "Google News - Markets & Rupee",
        f"{GOOGLE_NEWS}/search?q=Sensex+OR+Nifty+OR+rupee+OR+RBI+when:2d&{IN_LOCALE}",
        "Business & Finance News",
        0.78,
    ),
    FeedSource(
        "Google News - Startups & Funding",
        f"{GOOGLE_NEWS}/search?q=India+startup+funding+OR+IPO+OR+acquisition+when:2d&{IN_LOCALE}",
        "Business & Finance News",
        0.75,
    ),
    FeedSource(
        "Google News - Cricket",
        f"{GOOGLE_NEWS}/search?q=cricket+India+when:2d&{IN_LOCALE}",
        "Sports News",
        0.78,
    ),
    FeedSource(
        "Google News - Global Sport",
        f"{GOOGLE_NEWS}/search?q=olympics+OR+football+OR+tennis+OR+athletics+when:2d&{IN_LOCALE}",
        "Sports News",
        0.75,
    ),
    FeedSource(
        "Google News - Wellness & Nutrition",
        f"{GOOGLE_NEWS}/search?q=nutrition+OR+mental+health+OR+fitness+study+when:2d&{IN_LOCALE}",
        "Health & Wellness News",
        0.75,
    ),
    FeedSource(
        "Google News - Rare & Unusual",
        f"{GOOGLE_NEWS}/search?q=rare+OR+unprecedented+OR+mystery+OR+discovery+when:2d&{IN_LOCALE}",
        "Uncovered & Shocking News",
        0.72,
    ),
    FeedSource(
        "Google News - Investigations & Exposes",
        f"{GOOGLE_NEWS}/search?q=investigation+OR+expose+OR+whistleblower+OR+leaked+when:2d&{IN_LOCALE}",
        "Uncovered & Shocking News",
        0.75,
    ),
    FeedSource(
        "Google News - Climate & Disasters",
        f"{GOOGLE_NEWS}/search?q=climate+OR+heatwave+OR+flood+OR+cyclone+when:2d&{IN_LOCALE}",
        "World News",
        0.75,
    ),
    FeedSource(
        "Google News - Space & Astronomy",
        f"{GOOGLE_NEWS}/search?q=ISRO+OR+NASA+OR+asteroid+OR+space+mission+when:2d&{IN_LOCALE}",
        "Uncovered & Shocking News",
        0.75,
    ),

    # --- Wire services and primary sources --------------------------------
    # Reuters retired its public RSS endpoints (reutersagency.com and
    # reuters.com/rssfeed both return 404/401), so Reuters copy is pulled
    # through the free Google News site: operator, which still indexes it.
    FeedSource(
        "Reuters via Google News",
        f"{GOOGLE_NEWS}/search?q=site:reuters.com+when:2d&{IN_LOCALE}",
        None,
        0.95,
    ),
    FeedSource(
        "Associated Press via Google News",
        f"{GOOGLE_NEWS}/search?q=site:apnews.com+when:2d&{IN_LOCALE}",
        None,
        0.95,
    ),
    FeedSource(
        "PIB India - Press Releases",
        "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        "Current Affairs & Policy",
        1.00,
    ),
    FeedSource(
        "WHO - News",
        "https://www.who.int/rss-feeds/news-English.xml",
        "Health & Wellness News",
        1.00,
    ),
    FeedSource(
        "BBC News - World",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "World News",
        0.95,
    ),
    FeedSource(
        "Al Jazeera - All News",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "World News",
        0.92,
    ),
    FeedSource(
        "NPR - World",
        "https://feeds.npr.org/1004/rss.xml",
        "World News",
        0.92,
    ),
    FeedSource(
        "The Hindu - National",
        "https://www.thehindu.com/news/national/feeder/default.rss",
        "India News",
        0.92,
    ),
    FeedSource(
        "Times of India - Top Stories",
        "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "India News",
        0.88,
    ),
    FeedSource(
        "NDTV - India News",
        "https://feeds.feedburner.com/ndtvnews-india-news",
        "India News",
        0.88,
    ),
    FeedSource(
        "ESPNcricinfo - Cricket",
        "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
        "Sports News",
        0.92,
    ),
    FeedSource(
        "ScienceDaily - Top Science",
        "https://www.sciencedaily.com/rss/top/science.xml",
        "Uncovered & Shocking News",
        0.92,
    ),
    FeedSource(
        "NASA - Breaking News",
        "https://www.nasa.gov/news-release/feed/",
        "Uncovered & Shocking News",
        0.95,
    ),
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    """A normalised, deduplicated article."""

    title: str
    link: str
    summary: str
    published: datetime                 # timezone-aware, UTC
    source_feed: str
    publisher: str
    authority: float
    category_hint: str | None = None
    #: Number of distinct feeds that carried this same story. Higher means more
    #: newsrooms are chasing it, which is a strong virality signal.
    corroboration: int = 1
    #: Names of every feed the story appeared in.
    seen_in: list[str] = field(default_factory=list)

    @property
    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.published).total_seconds() / 60.0

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "link": self.link,
            "summary": self.summary,
            "published": self.published.isoformat(),
            "publisher": self.publisher,
            "corroboration": self.corroboration,
        }


# ---------------------------------------------------------------------------
# Text / URL normalisation helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")

_STOPWORDS = frozenset(
    """a an and are as at be by for from has have he his in is it its of on
    or that the their they this to was were will with after over into out
    says said new amid ahead""".split()
)

_TRACKING_PARAMS = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
     "fbclid", "gclid", "igshid", "ref", "cmpid", "smid", "s_cid"}
)


def clean_text(raw: str | None) -> str:
    """Strip HTML, unescape entities, collapse whitespace."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


_LINK_RE = re.compile(r"<a\s+href", re.I)


def _is_link_list(raw_summary: str | None) -> bool:
    """True when a summary is a list of links rather than prose.

    Google News ships every ``<description>`` as ``<a href=...>headline</a>``
    plus a grey ``<font>`` publisher name. Stripping the tags yields a
    headline/publisher soup that reads like fact but contains none, so it must
    never reach the briefing. Deduplication then keeps the longest *real*
    summary from a direct publisher feed instead.
    """
    if not raw_summary:
        return False
    links = len(_LINK_RE.findall(raw_summary))
    if links == 0:
        return False
    # Text living outside the anchors is genuine summary prose.
    outside = _TAG_RE.sub(" ", re.sub(r"<a\s+href.*?</a>", " ", raw_summary,
                                      flags=re.I | re.S))
    outside = _WS_RE.sub(" ", html.unescape(outside)).strip()
    return links >= 2 or len(outside) < 60


def split_publisher(title: str) -> tuple[str, str]:
    """Google News appends the publisher after a dash; separate the two."""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and 2 <= len(tail) <= 45:
            return head.strip(), tail.strip()
    return title.strip(), ""


def canonical_url(url: str) -> str:
    """Drop tracking params and normalise, so the same story matches itself."""
    if not url:
        return ""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    # Google News sometimes wraps the publisher URL in a ?url= parameter.
    query = parse_qs(parts.query)
    if "url" in query and query["url"]:
        return canonical_url(query["url"][0])
    kept = {k: v for k, v in query.items() if k.lower() not in _TRACKING_PARAMS}
    query_string = "&".join(f"{k}={v[0]}" for k, v in sorted(kept.items()) if v)
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/")
    return urlunparse((parts.scheme or "https", netloc, path, "", query_string, ""))


def title_fingerprint(title: str) -> str:
    """A whitespace/punctuation-insensitive key for exact duplicate matching."""
    core, _ = split_publisher(title)
    core = _NON_WORD_RE.sub(" ", core.lower())
    return _WS_RE.sub(" ", core).strip()


def title_tokens(title: str) -> frozenset[str]:
    """Significant tokens used for near-duplicate (Jaccard) matching."""
    return frozenset(
        tok for tok in title_fingerprint(title).split()
        if len(tok) > 2 and tok not in _STOPWORDS
    )


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def to_utc(struct_time: time.struct_time | None) -> datetime | None:
    """feedparser hands back a UTC struct_time; make it an aware datetime."""
    if not struct_time:
        return None
    try:
        return datetime(*struct_time[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
    )
    # The default pool holds 10 connections; we fan out to FEED_MAX_WORKERS
    # threads, most of them against news.google.com, so size the pool to match
    # or urllib3 discards and re-opens sockets on every request.
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=config.FEED_MAX_WORKERS,
        pool_maxsize=config.FEED_MAX_WORKERS * 2,
        max_retries=1,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_feed(source: FeedSource, session: requests.Session) -> list[NewsItem]:
    """Download and parse one feed. Never raises; returns ``[]`` on failure."""
    try:
        response = session.get(source.url, timeout=config.FEED_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Feed unreachable (%s): %s", source.name, exc)
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        log.warning("Feed unparseable (%s): %s", source.name, parsed.bozo_exception)
        return []

    items: list[NewsItem] = []
    for entry in parsed.entries[: config.FEED_ENTRIES_PER_SOURCE]:
        item = _entry_to_item(entry, source)
        if item:
            items.append(item)
    log.info("Fetched %-42s -> %3d entries", source.name, len(items))
    return items


def _entry_to_item(entry, source: FeedSource) -> NewsItem | None:
    raw_title = clean_text(getattr(entry, "title", ""))
    link = getattr(entry, "link", "") or ""
    if not raw_title or not link:
        return None

    published = (
        to_utc(getattr(entry, "published_parsed", None))
        or to_utc(getattr(entry, "updated_parsed", None))
    )
    if published is None:
        # Undated entries cannot be honestly time-window filtered, so drop them.
        return None

    title, publisher_from_title = split_publisher(raw_title)
    publisher = publisher_from_title
    if not publisher:
        feed_source = getattr(entry, "source", None)
        publisher = clean_text(getattr(feed_source, "title", "")) if feed_source else ""
    if not publisher:
        netloc = urlparse(link).netloc
        publisher = netloc[4:] if netloc.startswith("www.") else netloc

    raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
    summary = "" if _is_link_list(raw_summary) else clean_text(raw_summary)
    # Some feeds repeat the headline as the summary; strip that duplication.
    if summary and summary.lower().startswith(title.lower()[:40]):
        summary = summary[len(title):].strip(" -–—|")
    # A long run with no sentence punctuation is a headline list, not prose.
    if summary and "." not in summary and len(summary.split()) > 12:
        summary = ""

    return NewsItem(
        title=title,
        link=link,
        summary=summary,
        published=published,
        source_feed=source.name,
        publisher=publisher or "Unknown",
        authority=source.authority,
        category_hint=source.category_hint,
        seen_in=[source.name],
    )


def fetch_all(feeds: tuple[FeedSource, ...] = FEEDS) -> list[NewsItem]:
    """Fetch every feed concurrently; a single bad feed never kills the run."""
    session = build_session()
    collected: list[NewsItem] = []
    with ThreadPoolExecutor(max_workers=config.FEED_MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_feed, src, session): src for src in feeds}
        for future in as_completed(futures):
            source = futures[future]
            try:
                collected.extend(future.result())
            except Exception as exc:  # defensive
                log.warning("Feed worker crashed (%s): %s", source.name, exc)
    log.info("Raw entries collected: %d from %d feeds", len(collected), len(feeds))
    return collected


# ---------------------------------------------------------------------------
# Deduplication and filtering
# ---------------------------------------------------------------------------

def deduplicate(items: list[NewsItem], similarity: float = 0.62) -> list[NewsItem]:
    """Collapse the same story appearing across feeds.

    Three passes, cheapest first:

    1. canonical URL match
    2. exact normalised-title match
    3. near-duplicate match via Jaccard overlap of significant title tokens

    The surviving copy is the one with the highest source authority; every
    merge increments ``corroboration``, which the virality engine reads as a
    corroboration signal.
    """
    # Strongest source first, so the survivor of each merge is the best copy.
    ordered = sorted(items, key=lambda i: (-i.authority, i.published))

    by_url: dict[str, NewsItem] = {}
    by_title: dict[str, NewsItem] = {}
    kept: list[tuple[frozenset[str], NewsItem]] = []

    def merge(into: NewsItem, other: NewsItem) -> None:
        into.corroboration += 1
        if other.source_feed not in into.seen_in:
            into.seen_in.append(other.source_feed)
        if len(other.summary) > len(into.summary):
            into.summary = other.summary
        if other.published < into.published:
            into.published = other.published
        if into.category_hint is None:
            into.category_hint = other.category_hint

    for item in ordered:
        url_key = canonical_url(item.link)
        if url_key and url_key in by_url:
            merge(by_url[url_key], item)
            continue

        title_key = title_fingerprint(item.title)
        if title_key and title_key in by_title:
            merge(by_title[title_key], item)
            continue

        tokens = title_tokens(item.title)
        match = next(
            (existing for existing_tokens, existing in kept
             if jaccard(tokens, existing_tokens) >= similarity),
            None,
        )
        if match is not None:
            merge(match, item)
            continue

        if url_key:
            by_url[url_key] = item
        if title_key:
            by_title[title_key] = item
        kept.append((tokens, item))

    unique = [item for _, item in kept]
    log.info("Deduplicated %d -> %d unique stories", len(items), len(unique))
    return unique


def filter_by_window(
    items: list[NewsItem], start: datetime, end: datetime
) -> list[NewsItem]:
    """Keep items whose publication timestamp falls inside ``[start, end]``."""
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    inside = [i for i in items if start_utc <= i.published <= end_utc]
    log.info(
        "Time window %s -> %s kept %d/%d stories",
        start_utc.isoformat(timespec="minutes"),
        end_utc.isoformat(timespec="minutes"),
        len(inside),
        len(items),
    )
    return inside


def collect(
    start: datetime,
    end: datetime,
    feeds: tuple[FeedSource, ...] = FEEDS,
    widen_if_thin: int = 40,
) -> tuple[list[NewsItem], datetime]:
    """End-to-end collection: fetch -> dedupe -> time filter.

    If the requested window yields fewer than ``widen_if_thin`` stories (common
    for a 25-minute scan at a quiet hour), the lookback is progressively
    widened backwards so the briefing can still be filled. The effective start
    is returned alongside the items so the document states the real window.
    """
    raw = fetch_all(feeds)
    unique = deduplicate(raw)

    effective_start = start
    selected = filter_by_window(unique, effective_start, end)
    for widen_hours in (3, 12, 24, 48):
        if len(selected) >= widen_if_thin:
            break
        candidate_start = end - timedelta(hours=widen_hours)
        if candidate_start >= effective_start:
            continue
        log.info(
            "Only %d stories in window; widening lookback to %dh",
            len(selected), widen_hours,
        )
        effective_start = candidate_start
        selected = filter_by_window(unique, effective_start, end)

    selected.sort(key=lambda i: i.published, reverse=True)
    return selected, effective_start

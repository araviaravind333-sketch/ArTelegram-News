"""Instagram insights audit and the daily plain-language report.

Pulls the account's latest posts of every format (reels, images, carousels)
from the Meta Graph API, reads each post's real Insights numbers, and
writes a report for Telegram that a non-analyst can act on: what got
views, what got no reaction, and three reels to post today.

Every number in the report is a raw Instagram Insights value (views,
accounts reached, likes, comments, shares, saves), so it can be checked
against the Insights screen in the Instagram app. Nothing is invented,
and small samples are called out as small rather than dressed up as
trends.

Requires a Business or Creator account linked to a Facebook Page, and a
token with ``instagram_basic`` + ``instagram_manage_insights``.
"""

from __future__ import annotations

import html
import json
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

import config
from analyzer.virality_engine import CATEGORY_LEXICON, ScoredItem

log = logging.getLogger(__name__)

MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,permalink,timestamp,"
    "like_count,comments_count,thumbnail_url"
)

#: Requested in this order; unsupported names are dropped automatically,
#: because Meta retires insight metrics without warning across API versions.
#: "plays" was Meta's original reels-view metric; it has since been retired
#: in favour of "views" (ReelMetrics.views already falls back to "plays" for
#: any account still on an older Graph API version that only reports that
#: name, so nothing is lost by not requesting it directly).
REEL_METRICS = (
    "views", "reach", "total_interactions", "likes", "comments",
    "shares", "saved", "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
)

#: Images and carousels have no watch-time metrics; asking for them would
#: only trigger a rejected request and a retry.
POST_METRICS = (
    "views", "reach", "total_interactions", "likes", "comments",
    "shares", "saved",
)

HOOK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Question hook", re.compile(r"^[^.!?\n]{0,120}\?", re.S)),
    ("Number/list hook", re.compile(r"^\W*\d+[\s\w]{0,40}(?:things|ways|facts|reasons|signs|rules)", re.I)),
    ("Shock/curiosity hook", re.compile(r"\b(shock|shocking|unbelievable|nobody|no one|secret|hidden|truth|exposed|revealed|you won'?t believe)\b", re.I)),
    ("Breaking-news hook", re.compile(r"\b(breaking|just in|big news|alert|update|confirmed)\b", re.I)),
    ("How-to/utility hook", re.compile(r"\b(how to|step[s]? to|guide|checklist|apply|eligibility|deadline)\b", re.I)),
    ("Stat/number hook", re.compile(r"\d[\d,.]*\s*(?:%|per cent|crore|lakh|billion|million)", re.I)),
    ("Opinion/debate hook", re.compile(r"\b(agree|disagree|opinion|should|right or wrong|controversial)\b", re.I)),
)


class InstagramError(RuntimeError):
    """Raised when the Graph API cannot be queried."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ReelMetrics:
    """One Instagram post (reel, image, carousel...) with its raw insight
    values and derived ratios. The name is historical: the daily reel
    history persisted for the news report still only tracks ``kind ==
    "Reel"`` entries, but the audit itself reads every format."""

    media_id: str
    permalink: str
    caption: str
    posted_at: datetime
    raw: dict[str, float] = field(default_factory=dict)
    kind: str = "Reel"

    # -- raw accessors with sane fallbacks ---------------------------------
    @property
    def reach(self) -> float:
        return self.raw.get("reach", 0.0)

    @property
    def views(self) -> float:
        """Meta renamed plays -> views; accept whichever the account returns."""
        return self.raw.get("views") or self.raw.get("plays") or self.reach

    @property
    def likes(self) -> float:
        return self.raw.get("likes", 0.0)

    @property
    def comments(self) -> float:
        return self.raw.get("comments", 0.0)

    @property
    def shares(self) -> float:
        return self.raw.get("shares", 0.0)

    @property
    def saves(self) -> float:
        return self.raw.get("saved", 0.0)

    @property
    def avg_watch_seconds(self) -> float:
        """``ig_reels_avg_watch_time`` is reported in milliseconds."""
        return self.raw.get("ig_reels_avg_watch_time", 0.0) / 1000.0

    # -- derived ratios (per 1,000 views, the unit editors reason in) ------
    def _per_k(self, value: float) -> float:
        return round((value / self.views) * 1000, 2) if self.views else 0.0

    @property
    def share_rate(self) -> float:
        return self._per_k(self.shares)

    @property
    def save_rate(self) -> float:
        return self._per_k(self.saves)

    @property
    def comment_rate(self) -> float:
        return self._per_k(self.comments)

    @property
    def like_rate(self) -> float:
        return self._per_k(self.likes)

    @property
    def engagement_rate(self) -> float:
        return self._per_k(self.likes + self.comments + self.shares + self.saves)

    @property
    def view_through(self) -> float:
        """Views divided by reach: >1.0 means genuine replays, i.e. a hook that lands."""
        return round(self.views / self.reach, 2) if self.reach else 0.0

    @property
    def first_line(self) -> str:
        return (self.caption or "").strip().splitlines()[0] if self.caption else ""

    @property
    def hook_type(self) -> str:
        text = self.first_line or self.caption or ""
        for label, pattern in HOOK_PATTERNS:
            if pattern.search(text):
                return label
        return "Plain statement hook"

    @property
    def topic(self) -> str:
        text = (self.caption or "").lower()
        best, best_hits = "Uncategorised", 0
        for category, (_, keywords) in CATEGORY_LEXICON.items():
            hits = sum(1 for kw in keywords if kw in text)
            if hits > best_hits:
                best, best_hits = category, hits
        return best

    def as_dict(self) -> dict:
        return {
            "media_id": self.media_id,
            "permalink": self.permalink,
            "posted_at": self.posted_at.isoformat(),
            "hook_type": self.hook_type,
            "topic": self.topic,
            "views": self.views,
            "reach": self.reach,
            "share_rate": self.share_rate,
            "save_rate": self.save_rate,
            "comment_rate": self.comment_rate,
            "engagement_rate": self.engagement_rate,
            "view_through": self.view_through,
            "avg_watch_seconds": round(self.avg_watch_seconds, 2),
        }


@dataclass
class Benchmarks:
    """Rolling medians computed from every reel observed so far."""

    views: float = 0.0
    share_rate: float = 0.0
    save_rate: float = 0.0
    comment_rate: float = 0.0
    engagement_rate: float = 0.0
    view_through: float = 0.0
    sample_size: int = 0

    @classmethod
    def from_reels(cls, reels: Iterable[ReelMetrics]) -> "Benchmarks":
        rows = list(reels)
        if not rows:
            return cls()

        def median(attr: str) -> float:
            values = [getattr(r, attr) for r in rows]
            return round(statistics.median(values), 2) if values else 0.0

        return cls(
            views=median("views"),
            share_rate=median("share_rate"),
            save_rate=median("save_rate"),
            comment_rate=median("comment_rate"),
            engagement_rate=median("engagement_rate"),
            view_through=median("view_through"),
            sample_size=len(rows),
        )

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# ---------------------------------------------------------------------------
# Graph API client
# ---------------------------------------------------------------------------

class InstagramAuditor:
    def __init__(self, access_token: str | None = None, user_id: str | None = None):
        if access_token and user_id:
            self.access_token, self.user_id = access_token, user_id
        else:
            self.access_token, self.user_id = config.instagram_credentials()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{config.GRAPH_API_BASE}/{path.lstrip('/')}"
        query = {**params, "access_token": self.access_token}
        try:
            response = self.session.get(url, params=query, timeout=45)
        except requests.RequestException as exc:
            raise InstagramError(f"Graph API request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise InstagramError(
                f"Graph API returned non-JSON (HTTP {response.status_code})"
            ) from exc

        if "error" in payload:
            error = payload["error"]
            raise InstagramError(
                f"Graph API error {error.get('code')}: "
                f"{error.get('message', 'unknown')} "
                f"(type={error.get('type')})"
            )
        if response.status_code >= 400:
            raise InstagramError(f"Graph API HTTP {response.status_code}")
        return payload

    # -- media -------------------------------------------------------------

    def fetch_profile(self) -> dict[str, Any]:
        """Follower and post counts for the report header. Never raises."""
        try:
            return self._get(
                self.user_id, {"fields": "username,followers_count,media_count"}
            )
        except InstagramError as exc:
            log.warning("Could not fetch profile details: %s", exc)
            return {}

    @staticmethod
    def _kind_of(media: dict[str, Any]) -> str:
        if media.get("media_product_type") == "REELS":
            return "Reel"
        return {
            "IMAGE": "Image", "CAROUSEL_ALBUM": "Carousel", "VIDEO": "Video",
        }.get(media.get("media_type", ""), "Post")

    def fetch_recent_posts(
        self, sample_size: int | None = None
    ) -> tuple[list[ReelMetrics], list[ReelMetrics]]:
        """Return ``(sample, reels)``, both newest first.

        ``sample`` is the latest ``sample_size`` posts of *every* format, which
        is what the report describes. ``reels`` is every reel found in the page
        of media fetched - it can reach further back than the sample, so the
        reel history kept for the news report keeps accumulating even though
        reels are rare on this account.
        """
        want = sample_size or config.AUDIT_POST_COUNT
        payload = self._get(
            f"{self.user_id}/media",
            {"fields": MEDIA_FIELDS, "limit": min(100, max(50, want))},
        )

        sample: list[ReelMetrics] = []
        reels: list[ReelMetrics] = []
        for index, media in enumerate(payload.get("data", [])):
            kind = self._kind_of(media)
            in_sample = index < want
            if not in_sample and kind != "Reel":
                continue  # older non-reel posts are not needed at all
            try:
                posted = datetime.fromisoformat(
                    media["timestamp"].replace("+0000", "+00:00")
                )
            except (KeyError, ValueError):
                posted = datetime.now(timezone.utc)

            post = ReelMetrics(
                media_id=media["id"],
                permalink=media.get("permalink", ""),
                caption=media.get("caption", "") or "",
                posted_at=posted.astimezone(timezone.utc),
                raw={
                    "likes": float(media.get("like_count") or 0),
                    "comments": float(media.get("comments_count") or 0),
                },
                kind=kind,
            )
            post.raw.update(self.fetch_insights(post.media_id, kind))
            if in_sample:
                sample.append(post)
            if kind == "Reel":
                reels.append(post)

        if not sample:
            raise InstagramError(
                "No posts found on this account. Confirm INSTA_USER_ID points at "
                "an Instagram Business/Creator account that has published posts."
            )
        log.info(
            "Fetched %d recent posts (%d reels seen) for audit", len(sample), len(reels)
        )
        return sample, reels

    def fetch_insights(self, media_id: str, kind: str = "Reel") -> dict[str, float]:
        """Fetch insight metrics, retrying without any metric Meta rejects."""
        metrics = list(REEL_METRICS if kind == "Reel" else POST_METRICS)
        while metrics:
            try:
                payload = self._get(
                    f"{media_id}/insights", {"metric": ",".join(metrics)}
                )
            except InstagramError as exc:
                dropped = self._drop_unsupported_metrics(str(exc), metrics)
                if dropped:
                    log.debug("Dropping unsupported metric(s) %r", dropped)
                    continue
                log.warning("Insights unavailable for %s: %s", media_id, exc)
                return {}

            values: dict[str, float] = {}
            for entry in payload.get("data", []):
                name = entry.get("name")
                series = entry.get("values") or []
                if name and series:
                    try:
                        values[name] = float(series[0].get("value") or 0)
                    except (TypeError, ValueError):
                        values[name] = 0.0
            return values
        return {}

    #: Matches Meta's actual rejection format, e.g.:
    #: "(#100) metric[0] must be one of the following values: reach, likes, ..."
    #: It never names the *invalid* metric - only the allowed set - so the bad
    #: one can only be found by set difference against what was requested.
    _ALLOWED_VALUES_RE = re.compile(
        r"must be one of the following values:\s*([a-z0-9_,\s]+)", re.I
    )

    @classmethod
    def _drop_unsupported_metrics(cls, message: str, metrics: list[str]) -> list[str]:
        """Remove (in place) every requested metric the API just rejected.

        Returns the list of metrics removed, so the caller can tell "nothing
        changed, stop retrying" apart from "trimmed the list, try again".
        """
        match = cls._ALLOWED_VALUES_RE.search(message)
        if match:
            allowed = {m.strip().lower() for m in match.group(1).split(",") if m.strip()}
            invalid = [m for m in metrics if m.lower() not in allowed]
            for m in invalid:
                metrics.remove(m)
            return invalid

        # Fallback for any other Meta error phrasing that does name the metric
        # directly (e.g. "metric plays is not supported for this media type").
        # Word-boundary match, longest name first: metric names can be
        # substrings of each other ("views" inside "total_views"), and a
        # naive substring scan would grab the wrong one.
        lowered = message.lower()
        if "metric" not in lowered and "unsupported" not in lowered:
            return []
        for m in sorted(metrics, key=len, reverse=True):
            if re.search(rf"\b{re.escape(m.lower())}\b", lowered):
                metrics.remove(m)
                return [m]
        return []


# ---------------------------------------------------------------------------
# Benchmark persistence
# ---------------------------------------------------------------------------

def load_history() -> dict[str, Any]:
    path = config.BENCHMARK_FILE
    if not path.is_file():
        return {"reels": {}, "benchmarks": {}, "category_performance": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Benchmark file unreadable (%s); starting fresh: %s", path, exc)
        return {"reels": {}, "benchmarks": {}, "category_performance": {}}


def save_history(reels: list[ReelMetrics], benchmarks: Benchmarks) -> dict[str, Any]:
    """Merge today's reels into the rolling history and persist it.

    ``category_performance`` feeds straight back into the virality engine, so
    topics that over-perform on this account get promoted in tomorrow's
    High-Virality Picks.
    """
    config.ensure_directories()
    history = load_history()
    stored: dict[str, Any] = history.get("reels", {})

    for reel in reels:
        stored[reel.media_id] = reel.as_dict()

    # Keep the newest 200 reels so the file stays small and git-diffable.
    ordered = sorted(
        stored.items(), key=lambda kv: kv[1].get("posted_at", ""), reverse=True
    )[:200]
    stored = dict(ordered)

    performance: dict[str, list[float]] = {}
    for row in stored.values():
        topic = row.get("topic")
        views = row.get("views") or 0
        if topic and topic != "Uncategorised" and views:
            performance.setdefault(topic, []).append(float(views))

    history.update(
        {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reels": stored,
            "benchmarks": benchmarks.as_dict(),
            "category_performance": {
                topic: round(statistics.median(values), 2)
                for topic, values in performance.items()
            },
        }
    )
    config.BENCHMARK_FILE.write_text(
        json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Benchmarks saved (%d reels tracked)", len(stored))
    return history


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

#: Phrases that mean a caption is asking the viewer to do something. Bare
#: words are deliberately not enough: "shares fell", "saved 2,000 acres",
#: "declined to comment" and "countries agree" are ordinary news wording, not
#: requests, so each pattern needs the imperative form.
_CTA_RE = re.compile(
    r"\bfollow\s+(?:us|for|our|@\w+|\S*news\S*)"
    r"|\b(?:drop|leave|share|post|write)\s+your\b"
    r"|\bin the comments\b|\bcomments? below\b|\bcomment\s+(?:below|now|if|your)\b"
    # Keyword requests: comment "ID" / comment 'ZOMATO' / comment ID to learn...
    r"|\bcomment\s+[\u201c\u201d\u2018\u2019'\x22]|\bcomment\s+\w+\s+to\b"
    r"|\bhit\s+follow\b"
    r"|\btag\s+(?:a|your|someone|two|three|friends?)\b"
    r"|\bshare\s+(?:this|it|with)\b|\bsave\s+(?:this|it|for later)\b"
    r"|\b(?:tell us|let us know|what do you think|dm us|subscribe)\b"
    r"|\byour (?:thoughts|opinion|views?)\b",
    re.I,
)

#: Display order and wording for each post format.
_KIND_ORDER = ("Reel", "Image", "Carousel", "Video", "Post")
_KIND_WORD = {
    "Reel": "reel", "Image": "image", "Carousel": "carousel",
    "Video": "video", "Post": "post",
}

#: Below this many followers the report adds a small-numbers reminder.
SMALL_ACCOUNT_FOLLOWERS = 100
#: Below this many posts the report calls itself a first look.
FIRST_LOOK_POSTS = 5


@dataclass
class AuditResult:
    """Everything the report is built from."""

    posts: list[ReelMetrics]      # latest posts of every format, newest first
    reels: list[ReelMetrics]      # every reel seen (can reach further back)
    profile: dict[str, Any] = field(default_factory=dict)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _num(value: float) -> str:
    """Whole numbers with separators; one decimal for small non-integers."""
    if value >= 10 or abs(value - round(value)) < 0.05:
        return f"{value:,.0f}"
    return f"{value:.1f}"


def _amount(value: float, word: str) -> str:
    """A number with its noun, pluralised: ``1 share``, ``5 shares``."""
    return f"{_num(value)} {word}{'' if value == 1 else 's'}"


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _count_phrase(kind: str, count: int) -> str:
    word = _KIND_WORD.get(kind, kind.lower())
    return f"{count} {word}{'' if count == 1 else 's'}"


def format_breakdown(posts: list[ReelMetrics]) -> list[tuple[str, int, float]]:
    """``(format, post count, average views)`` per format, in display order."""
    rows = []
    for kind in _KIND_ORDER:
        group = [p for p in posts if p.kind == kind]
        if group:
            rows.append((kind, len(group), _mean([p.views for p in group])))
    return rows


def topic_breakdown(
    posts: list[ReelMetrics], min_posts: int = 3
) -> tuple[str, list[tuple[str, float, int]]]:
    """Average views by topic, using only the most common format.

    Reels and images get very different views, so mixing them would make a
    topic look strong or weak purely because of the format it happened to be
    posted in. Topics with fewer than ``min_posts`` posts are left out: one
    or two posts is not a pattern.
    """
    if not posts:
        return "", []
    counts: dict[str, int] = {}
    for post in posts:
        counts[post.kind] = counts.get(post.kind, 0) + 1
    main_kind = max(counts, key=lambda k: counts[k])

    grouped: dict[str, list[float]] = {}
    for post in posts:
        if post.kind == main_kind and post.topic != "Uncategorised":
            grouped.setdefault(post.topic, []).append(post.views)
    rows = sorted(
        ((topic, _mean(views), len(views))
         for topic, views in grouped.items() if len(views) >= min_posts),
        key=lambda row: -row[1],
    )
    return main_kind, rows


def format_verdict(breakdown: list[tuple[str, int, float]]) -> str:
    """One plain sentence comparing reels with image posts, or ``""``."""
    by_kind = {kind: (count, avg) for kind, count, avg in breakdown}
    if "Reel" not in by_kind or "Image" not in by_kind:
        return ""
    reel_count, reel_avg = by_kind["Reel"]
    _, image_avg = by_kind["Image"]
    if image_avg <= 0:
        return ""

    ratio = reel_avg / image_avg
    if ratio >= 1.5:
        times = f"{ratio:.1f}" if ratio < 10 else f"{ratio:.0f}"
        text = f"Reels are getting about {times}× the views of image posts"
    elif ratio <= 2 / 3:
        text = "Image posts are getting more views than reels"
    else:
        text = "Reels and image posts are getting similar views"
    if reel_count < 5:
        text += (
            f", but that is only {_count_phrase('Reel', reel_count)} so far - "
            "treat it as a hint, not proof"
        )
    return f"→ {text}."


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _describe_post(post: ReelMetrics) -> str:
    """Two lines: which post it is, then its real Insights numbers."""
    when = post.posted_at.astimezone(config.IST).strftime("%d %b")
    title = _esc(_clip(post.first_line or post.caption or "(no caption)", 60))
    line = (
        f"{post.kind}, {when} — “{title}”\n"
        f"   {_amount(post.views, 'view')} · "
        f"{_amount(post.reach, 'account')} reached · "
        f"{_amount(post.likes, 'like')} · {_amount(post.comments, 'comment')} · "
        f"{_amount(post.shares, 'share')} · {_amount(post.saves, 'save')}"
    )
    if post.permalink:
        line += f' · <a href="{html.escape(post.permalink, quote=True)}">open</a>'
    return line


def _script_facts(story: ScoredItem) -> str:
    """Up to two sentences of facts for a reel script, without filler.

    ``core_facts`` opens with the headline (already shown on the story line)
    and may end with a "Reported by ... carried by N feeds" note used when
    the publisher gave no summary; neither belongs in a spoken script.
    """
    title = story.item.title.strip().rstrip(".").lower()
    sentences = re.split(r"(?<=[.!?])\s+", story.core_facts or "")
    kept = [
        s.strip() for s in sentences
        if s.strip()
        and not s.lower().startswith("reported by")
        and s.strip().rstrip(".").lower() != title
    ]
    return " ".join(kept[:2]) or story.item.title


def build_action_plan(
    audit: AuditResult, news_picks: list[ScoredItem] | None = None
) -> str:
    """Render the Telegram report (HTML parse mode, plain language)."""
    posts = audit.posts
    profile = audit.profile
    now_ist = datetime.now(config.IST)
    followers = profile.get("followers_count")
    ranked = sorted(posts, key=lambda p: (p.views, p.reach), reverse=True)

    # -- header -----------------------------------------------------------
    lines = [
        "\U0001F4CA <b>ARAVIND NEWS 24 — DAILY INSTAGRAM REPORT</b>",
        f"<i>{now_ist.strftime('%A, %d %B %Y · %I:%M %p IST')}</i>",
        "",
        "<b>Your account right now</b>",
    ]
    account = []
    if profile.get("username"):
        account.append(f"@{_esc(profile['username'])}")
    if followers is not None:
        account.append(f"{followers:,} followers")
    if profile.get("media_count") is not None:
        account.append(f"{profile['media_count']:,} posts")
    if account:
        lines.append(" · ".join(account))

    newest = max(p.posted_at for p in posts).astimezone(config.IST)
    oldest = min(p.posted_at for p in posts).astimezone(config.IST)
    breakdown = format_breakdown(posts)
    mix = ", ".join(_count_phrase(kind, count) for kind, count, _ in breakdown)
    lines.append(
        f"I looked at your latest {len(posts)} posts "
        f"({oldest:%d %b} – {newest:%d %b}): {mix}."
    )
    lines.append(
        "These numbers come straight from Instagram Insights, so they should "
        "match the Insights screen of each post in the app."
    )
    if len(posts) < FIRST_LOOK_POSTS:
        lines.append(
            f"<i>Only {len(posts)} posts so far, so treat this as a first look.</i>"
        )

    # -- 1. what worked ---------------------------------------------------
    lines += ["", "<b>1. WHAT WORKED</b>", "Your top posts by views:"]
    for index, post in enumerate(ranked[:3], start=1):
        lines.append(f"{index}) {_describe_post(post)}")

    lines += [
        "",
        "Average views per post, by format: " + " · ".join(
            f"{_KIND_WORD.get(kind, kind.lower()).capitalize()}s {_num(avg)} "
            f"({count} post{'' if count == 1 else 's'})"
            for kind, count, avg in breakdown
        ),
    ]
    verdict = format_verdict(breakdown)
    if verdict:
        lines.append(verdict)

    main_kind, topics = topic_breakdown(posts)
    if topics:
        kind_word = _KIND_WORD.get(main_kind, main_kind.lower())
        lines.append(
            f"Topics your {kind_word} posts got the most views on (average views): "
            + ", ".join(
                f"{_esc(topic)} {_num(avg)} ({count} posts)"
                for topic, avg, count in topics[:3]
            )
        )

    # -- 2. what didn't work ----------------------------------------------
    n = len(posts)
    likes = sum(p.likes for p in posts)
    comments = sum(p.comments for p in posts)
    shares = sum(p.shares for p in posts)
    saves = sum(p.saves for p in posts)
    lines += ["", "<b>2. WHAT DIDN'T WORK</b>"]
    if likes + comments + shares + saves == 0:
        lines.append(
            f"• Nobody liked, commented on, saved or shared any of these {n} posts."
        )
    else:
        lines.append(
            f"• Across these {n} posts: {_amount(likes, 'like')}, "
            f"{_amount(comments, 'comment')}, {_amount(shares, 'share')}, "
            f"{_amount(saves, 'save')}."
        )
    typical_reach = statistics.median([p.reach for p in posts])
    lines.append(
        f"• A typical post reaches about {_amount(typical_reach, 'account')}."
    )
    asked = sum(1 for p in posts if _CTA_RE.search(p.caption or ""))
    request_line = (
        f"• {asked} of {n} captions ask viewers to comment, share, save or follow."
    )
    if asked < n / 2:
        request_line += " Add one clear request to every caption."
    elif likes + comments + shares + saves == 0:
        request_line += (
            " They already ask, so the bigger limit is how few accounts see "
            "the posts, not the wording."
        )
    lines.append(request_line)

    if n >= 6:
        top_ids = {p.media_id for p in ranked[:3]}
        weakest = [p for p in reversed(ranked) if p.media_id not in top_ids][:2]
        if weakest:
            lines += ["", "Lowest views:"]
            lines += [f"• {_describe_post(p)}" for p in weakest]

    # -- 3. today's plan --------------------------------------------------
    best_reel = max(audit.reels, key=lambda p: p.views, default=None)
    if best_reel is not None:
        goal = (
            f"Goal for today: beat your best reel so far ({_num(best_reel.views)} views)."
        )
    else:
        goal = (
            "Goal for today: post your first reel and beat your best post so far "
            f"({_num(ranked[0].views)} views)."
        )
    lines += [
        "",
        "<b>3. TODAY'S PLAN — 3 REELS TO POST</b>",
        f"{goal} One million views is the long-term aim; this is the next step towards it.",
    ]

    picks = (news_picks or [])[:3]
    if not picks:
        lines.append(
            "No news list was available this morning. Pick the biggest story from "
            "today's briefing and use the same layout: hook, facts, request."
        )
    for index, story in enumerate(picks, start=1):
        lines += [
            "",
            f"<b>Reel {index} — {_esc(story.category)}</b>",
            f"Story: {_esc(story.item.title)}",
            f"First 2 seconds, say: “{_esc(story.hook)}”",
            f"Next ~15 seconds, cover: {_esc(_script_facts(story))}",
            f"Last 5 seconds, ask: {_esc(story.cta)}",
            f"Why this story: it scored {story.score}/100 for viral potential "
            "in this morning's news scan.",
            f'<a href="{html.escape(story.link, quote=True)}">Source</a>',
        ]

    if followers is not None and followers < SMALL_ACCOUNT_FOLLOWERS:
        lines += [
            "",
            f"<i>Small account, small numbers: with {followers:,} followers a single "
            "view moves the averages a lot. Judge the trend over a few weeks, "
            "not one day.</i>",
        ]
    return "\n".join(lines)


def run_audit(
    news_picks: list[ScoredItem] | None = None,
    limit: int | None = None,
) -> tuple[str, AuditResult]:
    """Full audit pipeline: fetch -> keep reel history -> build the report."""
    auditor = InstagramAuditor()
    sample, reels = auditor.fetch_recent_posts(limit)
    profile = auditor.fetch_profile()

    # The news briefing's "Fits Your Instagram?" column reads reel history
    # only, so that is all that gets persisted; the report itself covers
    # every format.
    save_history(reels, Benchmarks.from_reels(reels))

    audit = AuditResult(posts=sample, reels=reels, profile=profile)
    return build_action_plan(audit, news_picks), audit

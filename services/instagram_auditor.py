"""Instagram Reels insights audit and the 1M-view action plan.

Pulls the most recent reels from the Meta Graph API, derives the ratios that
actually predict reach (share rate, save rate, comment rate, watch-through),
compares each reel against a rolling on-disk benchmark, and writes an
executive action plan for Telegram.

Requires a Business or Creator account linked to a Facebook Page, and a token
with ``instagram_basic`` + ``instagram_manage_insights``.
"""

from __future__ import annotations

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
REEL_METRICS = (
    "views", "reach", "plays", "total_interactions", "likes", "comments",
    "shares", "saved", "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
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
    """One reel with its raw insight values and the derived ratios."""

    media_id: str
    permalink: str
    caption: str
    posted_at: datetime
    raw: dict[str, float] = field(default_factory=dict)

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

    def fetch_recent_reels(self, limit: int | None = None) -> list[ReelMetrics]:
        """Return the most recent reels, newest first, with insights attached."""
        want = limit or config.REELS_AUDIT_COUNT
        # Over-fetch: the media edge mixes reels with images and carousels.
        payload = self._get(
            f"{self.user_id}/media",
            {"fields": MEDIA_FIELDS, "limit": min(100, max(25, want * 4))},
        )

        reels: list[ReelMetrics] = []
        for media in payload.get("data", []):
            if media.get("media_product_type") != "REELS":
                continue
            try:
                posted = datetime.fromisoformat(
                    media["timestamp"].replace("+0000", "+00:00")
                )
            except (KeyError, ValueError):
                posted = datetime.now(timezone.utc)

            reel = ReelMetrics(
                media_id=media["id"],
                permalink=media.get("permalink", ""),
                caption=media.get("caption", "") or "",
                posted_at=posted.astimezone(timezone.utc),
                raw={
                    "likes": float(media.get("like_count") or 0),
                    "comments": float(media.get("comments_count") or 0),
                },
            )
            reel.raw.update(self.fetch_insights(reel.media_id))
            reels.append(reel)
            if len(reels) >= want:
                break

        if not reels:
            raise InstagramError(
                "No reels found on this account. Confirm INSTA_USER_ID points at "
                "an Instagram Business/Creator account that has published reels."
            )
        log.info("Fetched %d reels for audit", len(reels))
        return reels

    def fetch_insights(self, media_id: str) -> dict[str, float]:
        """Fetch insight metrics, retrying without any metric Meta rejects."""
        metrics = list(REEL_METRICS)
        while metrics:
            try:
                payload = self._get(
                    f"{media_id}/insights", {"metric": ",".join(metrics)}
                )
            except InstagramError as exc:
                unsupported = self._unsupported_metric(str(exc), metrics)
                if unsupported:
                    log.debug("Dropping unsupported metric %r", unsupported)
                    metrics.remove(unsupported)
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

    @staticmethod
    def _unsupported_metric(message: str, metrics: list[str]) -> str | None:
        """Identify which requested metric the error message is complaining about."""
        lowered = message.lower()
        if "metric" not in lowered and "unsupported" not in lowered:
            return None
        for metric in metrics:
            if metric.lower() in lowered:
                return metric
        return None


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


def historical_benchmarks(history: dict[str, Any]) -> Benchmarks:
    """Rebuild medians from the full stored history, not just today's pull."""
    rows = list(history.get("reels", {}).values())
    if not rows:
        return Benchmarks()

    def median(key: str) -> float:
        values = [float(r.get(key) or 0) for r in rows]
        return round(statistics.median(values), 2) if values else 0.0

    return Benchmarks(
        views=median("views"),
        share_rate=median("share_rate"),
        save_rate=median("save_rate"),
        comment_rate=median("comment_rate"),
        engagement_rate=median("engagement_rate"),
        view_through=median("view_through"),
        sample_size=len(rows),
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def classify_performance(reel: ReelMetrics, bench: Benchmarks) -> tuple[str, list[str]]:
    """Return a verdict and the specific reasons behind it."""
    reasons: list[str] = []
    score = 0

    def compare(label: str, value: float, baseline: float, unit: str = "") -> None:
        nonlocal score
        if baseline <= 0:
            return
        delta = (value - baseline) / baseline * 100
        if delta >= 25:
            score += 1
            reasons.append(f"{label} {value}{unit} is {delta:+.0f}% vs median")
        elif delta <= -25:
            score -= 1
            reasons.append(f"{label} {value}{unit} is {delta:+.0f}% vs median")

    compare("views", reel.views, bench.views)
    compare("share rate", reel.share_rate, bench.share_rate, "/1k")
    compare("save rate", reel.save_rate, bench.save_rate, "/1k")
    compare("comment rate", reel.comment_rate, bench.comment_rate, "/1k")
    compare("view-through", reel.view_through, bench.view_through, "x")

    if reel.view_through and reel.view_through < 1.0 and bench.view_through >= 1.0:
        reasons.append("view-through below 1.0x: the first 2 seconds lost people")

    if score >= 2:
        verdict = "WINNER"
    elif score <= -2:
        verdict = "FLOP"
    else:
        verdict = "FLAT"
    return verdict, reasons


@dataclass
class AuditResult:
    reels: list[ReelMetrics]
    benchmarks: Benchmarks
    verdicts: dict[str, tuple[str, list[str]]]
    winners: list[ReelMetrics]
    flops: list[ReelMetrics]

    def by_verdict(self, verdict: str) -> list[ReelMetrics]:
        return [r for r in self.reels if self.verdicts[r.media_id][0] == verdict]


def analyse_reels(reels: list[ReelMetrics], benchmarks: Benchmarks) -> AuditResult:
    verdicts = {r.media_id: classify_performance(r, benchmarks) for r in reels}
    ranked = sorted(reels, key=lambda r: -r.engagement_rate)
    winners = [r for r in ranked if verdicts[r.media_id][0] == "WINNER"] or ranked[:2]
    flops = [r for r in ranked if verdicts[r.media_id][0] == "FLOP"] or ranked[-2:]
    return AuditResult(reels, benchmarks, verdicts, winners, flops)


def winning_hook_types(result: AuditResult) -> list[tuple[str, float]]:
    """Average engagement rate by hook type, best first."""
    grouped: dict[str, list[float]] = {}
    for reel in result.reels:
        grouped.setdefault(reel.hook_type, []).append(reel.engagement_rate)
    return sorted(
        ((hook, round(statistics.mean(values), 2)) for hook, values in grouped.items()),
        key=lambda kv: -kv[1],
    )


# ---------------------------------------------------------------------------
# Executive action plan
# ---------------------------------------------------------------------------

def _fmt_reel(reel: ReelMetrics, verdict: str, reasons: list[str]) -> str:
    when = reel.posted_at.astimezone(config.IST).strftime("%d %b")
    line = (
        f"<b>{when} — {reel.hook_type}</b> ({reel.topic})\n"
        f"   {int(reel.views):,} views • {reel.view_through}x view-through • "
        f"{reel.share_rate}/1k shares • {reel.save_rate}/1k saves • "
        f"{reel.comment_rate}/1k comments"
    )
    if reasons:
        line += "\n   Why: " + "; ".join(reasons[:3])
    if reel.permalink:
        line += f'\n   <a href="{reel.permalink}">open reel</a>'
    return line


#: Hook formats that genuinely fit each engagement driver. A "how-to" beat
#: sheet on a wildlife-discovery story would be a structure the story cannot
#: deliver, so pairing is filtered through this before performance ranking.
_DRIVER_COMPATIBLE_HOOKS: dict[str, tuple[str, ...]] = {
    "Debate/Comment Trigger": (
        "Opinion/debate hook", "Question hook", "Breaking-news hook",
        "Shock/curiosity hook", "Plain statement hook",
    ),
    "High Save": (
        "How-to/utility hook", "Number/list hook", "Stat/number hook",
        "Question hook", "Plain statement hook",
    ),
    "High Share": (
        "Shock/curiosity hook", "Stat/number hook", "Breaking-news hook",
        "Number/list hook", "Plain statement hook",
    ),
}

#: Categories whose stories only ever work as curiosity plays.
_CATEGORY_COMPATIBLE_HOOKS: dict[str, tuple[str, ...]] = {
    "Uncovered & Shocking News": (
        "Shock/curiosity hook", "Question hook", "Stat/number hook",
        "Number/list hook", "Plain statement hook",
    ),
}


def _pick_hook_for(story: ScoredItem | None, ranked: list[str], used: set[str]) -> str:
    """Highest-performing hook format that the story can actually support."""
    if story is None:
        allowed = tuple(ranked)
    else:
        allowed = _CATEGORY_COMPATIBLE_HOOKS.get(story.category)
        if allowed is None:
            driver = story.drivers[0] if story.drivers else "High Share"
            allowed = _DRIVER_COMPATIBLE_HOOKS.get(
                driver, _DRIVER_COMPATIBLE_HOOKS["High Share"]
            )

    # Prefer a compatible hook this account measurably wins with, and avoid
    # repeating a format already used earlier in today's roadmap.
    for hook in ranked:
        if hook in allowed and hook not in used:
            return hook
    for hook in allowed:
        if hook not in used:
            return hook
    return allowed[0] if allowed else "Plain statement hook"


def build_concepts(
    result: AuditResult, news_picks: list[ScoredItem]
) -> list[dict[str, str]]:
    """Three reel concepts: today's best news through the account's best hooks.

    Each concept pairs a proven hook format (ranked by measured engagement,
    then filtered to formats the story can actually deliver) with a
    high-scoring story from this morning's news scan, so the roadmap is
    grounded in both what the audience rewards and what is actually breaking.
    """
    hooks = winning_hook_types(result)
    ranked = [h for h, _ in hooks] or [
        "Question hook", "Stat/number hook", "Shock/curiosity hook"
    ]
    used_hooks: set[str] = set()

    bench = result.benchmarks
    # A roadmap target has to be a stretch the account can actually chase:
    # 1M is the stated goal, and roughly 1.5x the current median above that.
    target_views = max(1_000_000, int(bench.views * 1.5)) if bench.views else 1_000_000

    # Topics to fall back on when no news context is available, best first and
    # de-duplicated so three concepts never chase the same category.
    fallback_topics: list[str] = []
    for reel in result.winners + sorted(result.reels, key=lambda r: -r.views):
        if reel.topic not in fallback_topics and reel.topic != "Uncategorised":
            fallback_topics.append(reel.topic)
    fallback_topics = fallback_topics or ["India News"]

    concepts: list[dict[str, str]] = []
    for index in range(3):
        story = news_picks[index] if index < len(news_picks) else None
        hook_type = _pick_hook_for(story, ranked, used_hooks)
        used_hooks.add(hook_type)

        if story is not None:
            angle = story.hook
            facts = story.core_facts
            cta = story.cta
            topic = story.category
            source = story.link
        else:
            # Audit-only run: build on the topics this account already wins.
            topic = fallback_topics[index % len(fallback_topics)]
            angle = f"Reframe today's biggest {topic} story as a {hook_type.lower()}."
            facts = "Pull the two hardest numbers from the source and lead with them."
            cta = "Comment your take — we read every one."
            source = ""

        concepts.append(
            {
                "hook_type": hook_type,
                "topic": topic,
                "hook_line": angle,
                "facts": facts,
                "cta": cta,
                "source": source,
                "structure": _structure_for(hook_type),
                "target": f"{target_views:,} views",
            }
        )
    return concepts


def _structure_for(hook_type: str) -> str:
    """The 0-3s / 3-12s / 12-25s beat sheet that fits this hook format."""
    sheets = {
        "Question hook":
            "0-2s ask the question over the strongest frame | 3-10s two hard facts "
            "| 11-20s the twist nobody expects | 21-25s CTA on screen + voice",
        "Number/list hook":
            "0-2s state the number | 3-18s one fact per beat, hard cuts every 3s "
            "| 19-25s the last item is the most shareable | end on CTA card",
        "Shock/curiosity hook":
            "0-2s the shocking frame with no context | 3-6s withhold the answer "
            "| 7-20s pay it off with sourced facts | 21-25s CTA",
        "Breaking-news hook":
            "0-2s BREAKING card + location | 3-12s what happened, who confirmed it "
            "| 13-20s what it changes for the viewer | 21-25s CTA",
        "How-to/utility hook":
            "0-3s name the exact problem | 4-18s numbered steps on screen "
            "| 19-25s 'save this' CTA while the steps stay visible",
        "Stat/number hook":
            "0-2s the number full-screen | 3-12s what it means in rupees/lives "
            "| 13-20s the comparison that lands it | 21-25s CTA",
        "Opinion/debate hook":
            "0-3s state the contested claim flatly | 4-14s the strongest case each way "
            "| 15-22s refuse to resolve it | 23-25s comment CTA",
    }
    return sheets.get(
        hook_type,
        "0-2s hook | 3-12s facts | 13-20s payoff | 21-25s CTA",
    )


def build_action_plan(
    result: AuditResult,
    news_picks: list[ScoredItem] | None = None,
    account_handle: str = "Aravind News 24",
) -> str:
    """Render the Telegram-ready executive action plan (HTML parse mode)."""
    now_ist = datetime.now(config.IST)
    bench = result.benchmarks
    hooks = winning_hook_types(result)
    concepts = build_concepts(result, news_picks or [])

    lines: list[str] = [
        f"\U0001F3AC <b>{account_handle} — DAILY REELS AUDIT</b>",
        f"<i>{now_ist.strftime('%A, %d %B %Y — %I:%M %p IST')}</i>",
        "",
        f"Audited <b>{len(result.reels)}</b> recent reels against a "
        f"<b>{bench.sample_size}</b>-reel historical baseline.",
        f"Baseline medians: {int(bench.views):,} views • {bench.share_rate}/1k shares "
        f"• {bench.save_rate}/1k saves • {bench.comment_rate}/1k comments "
        f"• {bench.view_through}x view-through.",
        "",
        "✅ <b>WHAT WORKED &amp; WHY</b>",
    ]

    if result.winners:
        for reel in result.winners[:3]:
            verdict, reasons = result.verdicts[reel.media_id]
            lines.append(_fmt_reel(reel, verdict, reasons))
    else:
        lines.append("No reel cleared the winner threshold — everything landed flat.")

    if hooks:
        best = ", ".join(f"{h} ({rate}/1k)" for h, rate in hooks[:3])
        lines += ["", f"<b>Hook formats ranked by engagement:</b> {best}"]
        topics: dict[str, list[float]] = {}
        for reel in result.reels:
            topics.setdefault(reel.topic, []).append(reel.views)
        ranked_topics = sorted(
            ((t, statistics.mean(v)) for t, v in topics.items()), key=lambda kv: -kv[1]
        )[:3]
        lines.append(
            "<b>Topic categories by average views:</b> "
            + ", ".join(f"{t} ({int(v):,})" for t, v in ranked_topics)
        )

    lines += ["", "❌ <b>WHAT DIDN'T WORK — AVOID TODAY</b>"]
    if result.flops:
        for reel in result.flops[:3]:
            verdict, reasons = result.verdicts[reel.media_id]
            lines.append(_fmt_reel(reel, verdict, reasons))
    else:
        lines.append("Nothing underperformed badly enough to flag.")

    weak = [h for h, rate in hooks[-2:]] if len(hooks) > 2 else []
    if weak:
        lines.append(
            "\n<b>Stop using:</b> " + ", ".join(weak)
            + " — lowest measured engagement on this account."
        )
    low_retention = [r for r in result.reels if 0 < r.view_through < 1.0]
    if low_retention:
        lines.append(
            f"<b>Retention warning:</b> {len(low_retention)} of {len(result.reels)} "
            "reels had view-through under 1.0x — the opening frame is the problem, "
            "not the topic. Cut the first 1.5 seconds and lead with the payoff."
        )

    lines += ["", "\U0001F680 <b>1-MILLION-VIEW ROADMAP FOR TODAY</b>"]
    for index, concept in enumerate(concepts, start=1):
        block = [
            f"\n<b>CONCEPT {index} — {concept['hook_type']} | {concept['topic']}</b>",
            f"   <b>Hook:</b> {concept['hook_line']}",
            f"   <b>Facts:</b> {concept['facts']}",
            f"   <b>Structure:</b> {concept['structure']}",
            f"   <b>CTA:</b> {concept['cta']}",
            f"   <b>Target:</b> {concept['target']}",
        ]
        if concept["source"]:
            block.append(f'   <a href="{concept["source"]}">source</a>')
        lines += block

    lines += [
        "",
        "<i>Publish windows: 08:00-09:30, 13:00-14:00 and 19:30-21:30 IST. "
        "Post the debate concept last — comments compound into the evening.</i>",
    ]
    return "\n".join(lines)


def run_audit(
    news_picks: list[ScoredItem] | None = None,
    limit: int | None = None,
) -> tuple[str, AuditResult]:
    """Full audit pipeline: fetch -> benchmark -> analyse -> action plan."""
    auditor = InstagramAuditor()
    reels = auditor.fetch_recent_reels(limit)

    history = load_history()
    prior = historical_benchmarks(history)
    today = Benchmarks.from_reels(reels)
    # Use history when we have one; otherwise today's pull is the only baseline.
    baseline = prior if prior.sample_size >= len(reels) else today

    result = analyse_reels(reels, baseline)
    save_history(reels, Benchmarks.from_reels(reels))
    plan = build_action_plan(result, news_picks)
    return plan, result

"""Virality scoring, category routing, hook and CTA engineering.

The engine is deterministic by default: every score is reproducible from the
article itself plus the historical Instagram benchmarks on disk. When
``ANTHROPIC_API_KEY`` is present the top picks are additionally passed through
Claude for hook/CTA polish, but the numeric scoring never depends on it, so a
missing key degrades quality, not correctness.

Editorial rule enforced throughout: a hook may *frame* a fact, it may never
invent one. Core facts are derived only from the headline and the publisher's
own summary text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from scrapers.rss_collector import NewsItem

log = logging.getLogger(__name__)

# Drivers, in the vocabulary the brief asks for.
DRIVER_SHARE = "High Share"
DRIVER_SAVE = "High Save"
DRIVER_DEBATE = "Debate/Comment Trigger"


# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

#: category -> (weight, keywords). Weight scales how strongly a hit counts.
CATEGORY_LEXICON: dict[str, tuple[float, tuple[str, ...]]] = {
    "India News": (
        1.0,
        ("india", "indian", "delhi", "mumbai", "bengaluru", "chennai", "kolkata",
         "hyderabad", "pune", "ahmedabad", "kerala", "tamil nadu", "karnataka",
         "maharashtra", "uttar pradesh", "bihar", "gujarat", "punjab", "assam",
         "rajasthan", "odisha", "telangana", "modi", "rahul gandhi", "bjp",
         "congress", "lok sabha", "rajya sabha", "isro", "ipl", "rupee",
         "new delhi", "bharat"),
    ),
    "World News": (
        1.0,
        ("us", "usa", "china", "russia", "ukraine", "israel", "gaza", "europe",
         "uk", "britain", "france", "germany", "japan", "korea", "pakistan",
         "bangladesh", "sri lanka", "nepal", "afghanistan", "iran", "saudi",
         "united nations", "nato", "white house", "kremlin", "brussels",
         "global", "worldwide", "international", "summit", "war", "ceasefire",
         "earthquake", "hurricane", "typhoon"),
    ),
    "Unreported News": (
        1.1,
        ("mystery", "mysterious", "unexplained", "bizarre", "strange", "rare",
         "first time", "unprecedented", "shock", "shocking", "stunned",
         "discovery", "discovered", "hidden", "secret", "leaked", "exposed",
         "expose", "whistleblower", "investigation", "scandal", "cover-up",
         "unbelievable", "asteroid", "alien", "deep sea", "ancient", "fossil",
         "archaeolog", "anomaly", "record-breaking"),
    ),
    "Health News": (
        1.0,
        ("health", "disease", "virus", "outbreak", "vaccine", "cancer",
         "diabetes", "heart", "obesity", "mental health", "depression",
         "anxiety", "sleep", "diet", "nutrition", "fitness", "exercise",
         "who", "hospital", "doctor", "patient", "drug", "medicine", "study",
         "researchers", "clinical", "wellness", "immunity", "gut"),
    ),
    "Current Affairs": (
        1.0,
        ("policy", "bill", "law", "act", "parliament", "cabinet", "ministry",
         "minister", "government", "supreme court", "high court", "verdict",
         "ruling", "election", "poll", "scheme", "subsidy", "budget", "tax",
         "gst", "regulation", "regulator", "reform", "notification",
         "guidelines", "amendment", "ordinance", "pib", "commission"),
    ),
    "Business News": (
        1.0,
        ("market", "sensex", "nifty", "stock", "shares", "ipo", "funding",
         "acquisition", "merger", "revenue", "profit", "loss",
         "earnings", "quarter", "inflation", "gdp", "rbi", "bank", "loan",
         "gold", "oil", "trade", "tariff", "economy",
         "investors", "valuation", "layoff", "hiring", "salary", "billion",
         "crore", "lakh crore"),
    ),
    "Technology News": (
        1.0,
        ("technology", "tech", "ai", "artificial intelligence", "software",
         "app", "smartphone", "iphone", "android", "chip", "semiconductor",
         "startup", "cybersecurity", "data breach", "hacked", "hacking",
         "algorithm", "robot", "robotics", "electric vehicle",
         "gadget", "launch event", "meta platforms", "google", "microsoft", "apple",
         "openai", "chatgpt", "crypto", "bitcoin", "blockchain", "5g",
         "satellite", "drone", "chatbot"),
    ),
    "Sports News": (
        1.0,
        ("cricket", "test match", "odi", "t20", "ipl", "world cup", "football",
         "soccer", "fifa", "premier league", "olympics", "medal", "tennis",
         "grand slam", "wimbledon", "hockey", "badminton", "kabaddi",
         "athletics", "chess", "formula 1", "wrestling", "boxing", "coach",
         "captain", "innings", "goal", "tournament", "final", "semifinal"),
    ),
}

#: Emotional / structural triggers that move an audience to act.
SHARE_TRIGGERS = (
    "wins", "won", "record", "first", "historic", "breakthrough", "rescued",
    "saved", "hero", "proud", "celebrat", "beats", "defeats", "champion",
    "tribute", "milestone", "gold medal", "world's", "india's first",
)
SAVE_TRIGGERS = (
    "how to", "guide", "explained", "rules", "deadline", "last date", "apply",
    "eligibility", "checklist", "steps", "tips", "study", "research", "report",
    "scheme", "benefits", "list of", "what you need", "documents", "process",
    "new rule", "from april", "from january", "effective",
)
DEBATE_TRIGGERS = (
    "row", "controversy", "slams", "criticis", "backlash", "protest", "outrage",
    "banned", "ban", "accused", "alleged", "denies", "dispute", "clash",
    "verdict", "resigns", "sacked", "opposition", "attacks", "hits back",
    "questions", "debate", "boycott", "apolog", "fine", "penalty", "arrest",
)
CURIOSITY_TRIGGERS = (
    "why", "how", "what", "reason", "secret", "hidden", "revealed", "truth",
    "nobody", "no one", "actually", "really", "turns out", "surprising",
)

_NUMBER_RE = re.compile(r"\b\d[\d,.]*\s*(?:%|per cent|percent|crore|lakh|billion|million|bn|mn|kg|km)?\b")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScoredItem:
    """A news item enriched with everything the .docx and Telegram need."""

    item: NewsItem
    category: str
    score: int
    drivers: list[str]
    hook: str
    core_facts: str
    cta: str
    rationale: str = ""
    #: "YES" / "MAYBE" / "NO" - whether this account's own reel history
    #: supports posting this category. See build_instagram_fit().
    fit_verdict: str = "MAYBE"
    #: One-line reason backing fit_verdict, e.g. "India News reels outperform
    #: your account average by 18%". Shown as the 4th report column.
    fit_reason: str = ""
    #: Raw sub-scores, kept for debugging and for the audit trail.
    components: dict[str, float] = field(default_factory=dict)

    # Convenience passthroughs used by the generators.
    @property
    def link(self) -> str:
        return self.item.link

    @property
    def publisher(self) -> str:
        return self.item.publisher

    @property
    def published_ist(self) -> datetime:
        return self.item.published.astimezone(config.IST)

    @property
    def drivers_text(self) -> str:
        return " | ".join(self.drivers)

    @property
    def instagram_fit(self) -> str:
        """The full "verdict — reason" text shown in the fit column."""
        return f"{self.fit_verdict} — {self.fit_reason}" if self.fit_reason else self.fit_verdict

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "score": self.score,
            "drivers": self.drivers,
            "hook": self.hook,
            "core_facts": self.core_facts,
            "cta": self.cta,
            "publisher": self.publisher,
            "link": self.link,
            "published_ist": self.published_ist.isoformat(),
        }


# ---------------------------------------------------------------------------
# Historical performance weighting
# ---------------------------------------------------------------------------

#: Below this many total tracked reels, a category "multiplier" is not a
#: signal - it's an artifact. With 1 reel tracked, that reel's category
#: trivially equals the account "average" (a mean of one number always
#: equals itself), which would otherwise print as a misleading "performs
#: close to your account average (+0%)" on the very first audit.
MIN_TRACKED_REELS_FOR_SIGNAL = 5


def load_category_performance() -> dict[str, float]:
    """Read the rolling Instagram benchmark file written by the auditor.

    Returns a multiplier per category centred on 1.0. Categories that have
    historically over-performed on this account get promoted into the
    High-Virality Picks bucket; under-performers get damped. A missing or
    unreadable file, or too few tracked reels to mean anything yet, yields an
    empty dict (all multipliers = 1.0, and the Instagram-fit column falls
    back to judging by virality score alone).
    """
    path = config.BENCHMARK_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read benchmarks (%s): %s", path, exc)
        return {}

    tracked = payload.get("reels", {})
    if not isinstance(tracked, dict) or len(tracked) < MIN_TRACKED_REELS_FOR_SIGNAL:
        log.info(
            "Only %d reel(s) tracked (need %d) - not enough history yet for "
            "category performance signals.",
            len(tracked) if isinstance(tracked, dict) else 0,
            MIN_TRACKED_REELS_FOR_SIGNAL,
        )
        return {}

    by_category = payload.get("category_performance", {})
    if not isinstance(by_category, dict) or not by_category:
        return {}

    values = [v for v in by_category.values() if isinstance(v, (int, float)) and v > 0]
    if not values:
        return {}
    mean = sum(values) / len(values)
    if mean <= 0:
        return {}

    multipliers = {}
    for category, value in by_category.items():
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        # Clamp to +/-20% so one freak reel cannot dominate the editorial mix.
        multipliers[category] = max(0.80, min(1.20, value / mean))
    log.info("Historical category multipliers: %s", multipliers)
    return multipliers


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _haystack(item: NewsItem) -> str:
    return f"{item.title} {item.summary}".lower()


def classify(item: NewsItem) -> str:
    """Route an article to one of the seven assignable categories."""
    text = _haystack(item)
    scores: dict[str, float] = {}
    for category, (weight, keywords) in CATEGORY_LEXICON.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            scores[category] = hits * weight

    # The feed's own topic is a strong prior, but never an override.
    if item.category_hint and item.category_hint in CATEGORY_LEXICON:
        scores[item.category_hint] = scores.get(item.category_hint, 0.0) + 1.6

    if not scores:
        return "World News"

    # India-specific stories outrank the generic World bucket on a tie.
    best = max(scores.items(), key=lambda kv: (kv[1], kv[0] == "India News"))
    return best[0]


def detect_drivers(item: NewsItem) -> list[str]:
    """Map trigger language onto the three engagement drivers."""
    text = _haystack(item)
    counts = {
        DRIVER_SHARE: sum(1 for kw in SHARE_TRIGGERS if kw in text),
        DRIVER_SAVE: sum(1 for kw in SAVE_TRIGGERS if kw in text),
        DRIVER_DEBATE: sum(1 for kw in DEBATE_TRIGGERS if kw in text),
    }
    ranked = [d for d, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n > 0]
    if not ranked:
        # Every story still has a default behaviour: informational news is
        # saved, human-interest news is shared.
        ranked = [DRIVER_SAVE if _NUMBER_RE.search(text) else DRIVER_SHARE]
    return ranked[:2]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_item(
    item: NewsItem,
    category: str,
    drivers: list[str],
    performance: dict[str, float],
) -> tuple[int, dict[str, float], str]:
    """Return a 1-100 virality score, its components, and a short rationale.

    Components (max contribution):

    * recency        30  - decays over 48h; breaking news wins
    * corroboration  18  - how many independent feeds carried it
    * authority      12  - wire service / government primary source
    * trigger load   22  - share + save + debate language density
    * curiosity gap  10  - question and reveal framing
    * specificity     8  - concrete numbers in the headline
    """
    text = _haystack(item)
    parts: dict[str, float] = {}

    age_h = max(0.0, item.age_minutes / 60.0)
    parts["recency"] = round(30.0 * max(0.0, 1.0 - (age_h / 48.0)) ** 1.35, 2)

    parts["corroboration"] = round(min(18.0, 6.0 * (item.corroboration ** 0.7)), 2)
    parts["authority"] = round(12.0 * item.authority, 2)

    trigger_hits = (
        sum(1 for kw in SHARE_TRIGGERS if kw in text)
        + sum(1 for kw in SAVE_TRIGGERS if kw in text)
        + sum(1 for kw in DEBATE_TRIGGERS if kw in text)
    )
    parts["triggers"] = round(min(22.0, 5.5 * (trigger_hits ** 0.75)), 2)

    curiosity_hits = sum(1 for kw in CURIOSITY_TRIGGERS if kw in item.title.lower())
    parts["curiosity"] = round(min(10.0, 4.0 * curiosity_hits), 2)

    numbers = len(_NUMBER_RE.findall(item.title))
    parts["specificity"] = round(min(8.0, 4.0 * numbers), 2)

    base = sum(parts.values())

    multiplier = performance.get(category, 1.0)
    parts["historical_multiplier"] = round(multiplier, 3)

    # A debate driver reliably lifts comment volume; a save driver lifts reach
    # over a longer tail. Both are worth a small structural bonus.
    driver_bonus = 0.0
    if DRIVER_DEBATE in drivers:
        driver_bonus += 4.0
    if DRIVER_SAVE in drivers:
        driver_bonus += 2.5
    if DRIVER_SHARE in drivers:
        driver_bonus += 3.0
    parts["driver_bonus"] = driver_bonus

    total = (base + driver_bonus) * multiplier
    score = int(max(1, min(100, round(total))))

    top = sorted(
        ((k, v) for k, v in parts.items()
         if k not in {"historical_multiplier", "driver_bonus"}),
        key=lambda kv: -kv[1],
    )[:2]
    rationale = ", ".join(f"{k} {v:.0f}" for k, v in top)
    if multiplier != 1.0:
        rationale += f", history x{multiplier:.2f}"

    return score, parts, rationale


# ---------------------------------------------------------------------------
# Hook, facts and CTA engineering
# ---------------------------------------------------------------------------

_HOOK_TEMPLATES: dict[str, tuple[str, ...]] = {
    DRIVER_DEBATE: (
        "{head} — and the internet is split.",
        "Nobody agrees on this: {head}.",
        "{head}. Fair call, or too far?",
    ),
    DRIVER_SAVE: (
        "Save this before you need it: {head}.",
        "{head} — here is what actually changes for you.",
        "Bookmark this: {head}.",
    ),
    DRIVER_SHARE: (
        "This one deserves a share: {head}.",
        "{head}. Yes, this really happened.",
        "Everyone should see this: {head}.",
    ),
}

_CURIOSITY_TEMPLATES: tuple[str, ...] = (
    "Almost no one is talking about this: {head}.",
    "Wait — {head_lower}?",
    "You probably missed this: {head}.",
)

#: Each CTA carries a ``requires`` guard. A template is only eligible when the
#: story actually supports it — promising a deadline on a story with no
#: deadline is the fastest way to lose an audience's trust.
_CTA_LIBRARY: dict[str, tuple[tuple[str, str | None], ...]] = {
    DRIVER_DEBATE: (
        ("Comment your opinion on {topic} — agree or disagree?", None),
        ("Drop a 1 if you back this, 2 if you don't. Comment why.", None),
        ("Tell us in the comments: is {topic} the right call?", "decision"),
    ),
    DRIVER_SAVE: (
        ("Save this for later — you will need these {topic} details.", None),
        ("Save + share with someone this {topic} update affects.", None),
        ("Save this post before the {topic} deadline passes.", "deadline"),
        ("Save this — the {topic} numbers are worth coming back to.", "numbers"),
    ),
    DRIVER_SHARE: (
        ("Share this with the one person who needs to see it.", None),
        ("Send this to your group — they have not heard it yet.", None),
        ("Share if this made you proud. Follow for more on {topic}.", "pride"),
    ),
}

#: Evidence each guarded CTA needs to find in the story before it may be used.
_CTA_EVIDENCE: dict[str, tuple[str, ...]] = {
    # Deliberately narrow: only unambiguous deadline language qualifies.
    # Loose markers such as a bare "before" or "until" match almost any
    # headline and produced CTAs promising deadlines that did not exist.
    "deadline": ("deadline", "last date", "last day", "expires", "expiry",
                 "closes on", "closing date", "cut-off date", "cutoff date",
                 "valid till", "valid until", "apply by", "apply before",
                 "final date", "extended till", "extended to",
                 "comes into effect", "effective from", "with effect from"),
    "decision": ("ruling", "verdict", "decision", "ban", "banned", "approved",
                 "rejected", "order", "policy", "rule", "sacked", "resigns",
                 "hike", "cut", "called off"),
    "numbers": ("%", "per cent", "percent", "crore", "lakh", "billion",
                "million", "rate", "growth", "forecast", "survey", "study"),
    "pride": ("wins", "won", "record", "first", "historic", "gold", "medal",
              "champion", "rescued", "saved", "honoured", "awarded", "proud"),
}


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "..."


#: Words that are capitalised only because they open a headline, plus the
#: interrogatives and auxiliaries that make a CTA read like broken English
#: ("Comment your opinion on Body How Gurgaon").
_PHRASE_STOPWORDS = frozenset(
    """the a an of in on for to and or but after before with as at by from
    why how what when where who which is are was were be been being has have
    had will would can could should may might must do does did says said
    new big top this that these those it its his her their our your my
    body man woman people over under into out up down off
    first last next now today yesterday tomorrow amid ahead against""".split()
)


def _topic_phrase(item: NewsItem, category: str) -> str:
    """A short noun phrase for use inside a CTA sentence.

    Prefers the longest run of consecutive capitalised words (a real proper
    noun such as "Asian Games" or "Supreme Court") over a scatter of
    unrelated capitals, and falls back to the category name rather than
    emitting something ungrammatical.
    """
    words = re.findall(r"[A-Za-z][A-Za-z'&.-]*", item.title)

    best_run: list[str] = []
    current: list[str] = []
    for index, word in enumerate(words):
        significant = word[0].isupper() and word.lower() not in _PHRASE_STOPWORDS
        # A capitalised first word is not evidence of a proper noun.
        if index == 0 and word.lower() in _PHRASE_STOPWORDS:
            significant = False
        if significant:
            current.append(word)
            if len(current) > len(best_run):
                best_run = current.copy()
        else:
            current = []

    phrase = " ".join(best_run[:3]).strip(" .-")
    # A trailing possessive reads as a dangling fragment inside a CTA
    # ("Follow for more on Abhishek Sharma's"), so drop it.
    for suffix in ("'s", "’s", "'", "’"):
        if phrase.endswith(suffix):
            phrase = phrase[: -len(suffix)].rstrip()
            break
    if len(phrase) < 3:
        return category.replace(" News", "").replace(" & ", " and ")
    return phrase


def build_hook(item: NewsItem, drivers: list[str], category: str) -> str:
    """Craft a reel hook that reframes the headline without adding facts."""
    head = _shorten(item.title.rstrip(" .!?"), 110)
    primary = drivers[0] if drivers else DRIVER_SHARE

    if category == "Uncovered & Shocking News":
        templates = _CURIOSITY_TEMPLATES
    else:
        templates = _HOOK_TEMPLATES[primary]

    # Deterministic template pick keyed off the headline, so the same story
    # always produces the same hook across reruns.
    index = sum(ord(c) for c in item.title) % len(templates)
    hook = templates[index].format(head=head, head_lower=head[0].lower() + head[1:])
    return _shorten(hook, 140)


def build_core_facts(item: NewsItem) -> str:
    """Two journalistically accurate sentences, sourced from the feed only."""
    headline = item.title.rstrip(" .")
    first = f"{headline}."

    summary = item.summary.strip()
    second = ""
    if summary:
        sentences = [s.strip() for s in _SENTENCE_RE.split(summary) if s.strip()]
        for sentence in sentences:
            # Skip boilerplate and near-repeats of the headline.
            if len(sentence) < 30:
                continue
            if sentence.lower()[:40] == headline.lower()[:40]:
                continue
            second = sentence if sentence.endswith((".", "!", "?")) else sentence + "."
            break

    if not second:
        when = item.published.astimezone(config.IST).strftime("%d %b %Y, %I:%M %p IST")
        second = f"Reported by {item.publisher} on {when}; carried by {item.corroboration} of our monitored feeds."

    return f"{_shorten(first, 220)} {_shorten(second, 260)}".strip()


def build_cta(item: NewsItem, drivers: list[str], category: str) -> str:
    """Pick a CTA the story can actually pay off, then fill in its topic."""
    primary = drivers[0] if drivers else DRIVER_SHARE
    text = _haystack(item)

    eligible = [
        template for template, requirement in _CTA_LIBRARY[primary]
        if requirement is None
        or any(marker in text for marker in _CTA_EVIDENCE[requirement])
    ]
    if not eligible:  # every guarded option was filtered out
        eligible = [t for t, r in _CTA_LIBRARY[primary] if r is None]

    index = sum(ord(c) for c in item.link) % len(eligible)
    return eligible[index].format(topic=_topic_phrase(item, category))


# ---------------------------------------------------------------------------
# Briefing assembly
# ---------------------------------------------------------------------------

#: Multiplier bands beyond which this account's own history is treated as a
#: real signal rather than noise. Mirrors the +/-20% clamp in
#: load_category_performance(), so "close to average" and "no data" don't
#: get conflated: a multiplier is only ever produced for a category this
#: account has actually posted reels in.
FIT_OUTPERFORM_THRESHOLD = 1.05
FIT_UNDERPERFORM_THRESHOLD = 0.95

#: Below this virality score, a category with no reel history yet is called
#: NO rather than YES/MAYBE - "post it, we have no data" is not useful advice
#: for a weak story.
FIT_NO_HISTORY_WEAK_SCORE = 45
FIT_NO_HISTORY_STRONG_SCORE = 65


def build_instagram_fit(
    category: str, score: int, performance: dict[str, float]
) -> tuple[str, str]:
    """Whether this story's category fits the account, per its reel history.

    ``performance`` is the per-category view multiplier from
    load_category_performance() (see [[services/instagram_auditor.py]]'s
    save_history(), which derives it from every reel this account has ever
    posted). A category only appears in that dict once the account has
    actually posted a reel in it - anything else falls back to the story's
    own virality score, since there is no account-specific signal to use yet.

    Returns ``(verdict, reason)`` where verdict is "YES" / "MAYBE" / "NO".
    """
    multiplier = performance.get(category)

    if multiplier is None:
        if score >= FIT_NO_HISTORY_STRONG_SCORE:
            return "YES", (
                f"no reel history yet in {category}, but its virality score "
                f"({score}) is strong enough to try"
            )
        if score >= FIT_NO_HISTORY_WEAK_SCORE:
            return "MAYBE", (
                f"no reel history yet in {category}; virality score "
                f"({score}) is only moderate"
            )
        return "NO", (
            f"no reel history yet in {category}, and virality score "
            f"({score}) is too weak to risk it"
        )

    pct = round((multiplier - 1) * 100)
    if multiplier >= FIT_OUTPERFORM_THRESHOLD:
        return "YES", f"{category} reels outperform your account average by {pct}%"
    if multiplier <= FIT_UNDERPERFORM_THRESHOLD:
        return "NO", (
            f"{category} content has underperformed on this account by "
            f"{abs(pct)}% so far"
        )
    return "MAYBE", f"{category} performs close to your account average ({pct:+d}%)"


def analyse(item: NewsItem, performance: dict[str, float]) -> ScoredItem:
    category = classify(item)
    drivers = detect_drivers(item)
    score, components, rationale = score_item(item, category, drivers, performance)
    fit_verdict, fit_reason = build_instagram_fit(category, score, performance)
    return ScoredItem(
        item=item,
        category=category,
        score=score,
        drivers=drivers,
        hook=build_hook(item, drivers, category),
        core_facts=build_core_facts(item),
        cta=build_cta(item, drivers, category),
        rationale=rationale,
        fit_verdict=fit_verdict,
        fit_reason=fit_reason,
        components=components,
    )


#: Rank order for the Instagram-fit verdict when choosing Top Picks: a story
#: this account's own reel history backs (or, absent history, a strong
#: virality score) outranks one it doesn't, at equal virality score.
_FIT_RANK = {"YES": 2, "MAYBE": 1, "NO": 0}


def _pick_priority(entry: ScoredItem) -> tuple[int, int, int]:
    """Sort key for Top Picks: account fit first, then score, then reach.

    This is what ties Top Picks to "account insight and past reels" rather
    than raw virality alone: two equally-scored stories are broken by which
    one this account's own history (via fit_verdict) actually supports, then
    by corroboration - how many independent outlets are carrying it, the
    free-tier proxy for cross-platform buzz (see the module docstring note on
    Twitter/YouTube trend data).
    """
    return (_FIT_RANK.get(entry.fit_verdict, 1), entry.score, entry.item.corroboration)


def build_briefing(
    items: list[NewsItem],
    min_per_category: int | None = None,
    performance: dict[str, float] | None = None,
    max_per_category: int | None = None,
    min_picks: int | None = None,
) -> dict[str, list[ScoredItem]]:
    """Score everything and lay it out across the eight categories.

    Every story is placed in exactly one section - never repeated across Top
    Picks and its category table, and never cross-filed into two different
    categories at once. Guarantees ``min_per_category`` entries per category
    and ``min_picks`` entries in Top Picks whenever the raw supply allows it;
    categories that are naturally thin (e.g. Sports on a quiet Tuesday) are
    topped up from the highest-scoring *still-unused* stories, relabelled to
    the category they are filling - the source link and facts stay
    untouched, only the section placement changes, and it happens at most
    once per story.
    """
    minimum = min_per_category or config.MIN_ITEMS_PER_CATEGORY
    maximum = max(minimum, max_per_category or config.MAX_ITEMS_PER_CATEGORY)
    picks_target = max(minimum, min_picks or config.MIN_PICKS)
    perf = load_category_performance() if performance is None else performance

    scored = [analyse(item, perf) for item in items]
    scored.sort(key=lambda s: -s.score)

    # Native buckets, one per assignable category, each already score-sorted
    # because `scored` is. `used_links` is the single source of truth for
    # "has this story been placed anywhere yet" across every phase below.
    native: dict[str, list[ScoredItem]] = {c: [] for c in config.ASSIGNABLE_CATEGORIES}
    for entry in scored:
        native[entry.category].append(entry)
    used_links: set[str] = set()

    # 1. High-Virality Instagram Picks - one story per category first (so the
    #    picks slate isn't ten variations of the same category), ranked by
    #    account fit and corroboration ahead of raw score; topped up from the
    #    overall best remainder if categories alone can't reach the target.
    #    Every story chosen here is removed from further consideration, so it
    #    cannot also appear in its category table.
    picks: list[ScoredItem] = []
    cursor = {c: 0 for c in config.ASSIGNABLE_CATEGORIES}
    progressed = True
    while len(picks) < picks_target and progressed:
        progressed = False
        for category in config.ASSIGNABLE_CATEGORIES:
            bucket = native[category]
            i = cursor[category]
            while i < len(bucket) and bucket[i].link in used_links:
                i += 1
            cursor[category] = i
            if i < len(bucket):
                entry = bucket[i]
                picks.append(entry)
                used_links.add(entry.link)
                cursor[category] = i + 1
                progressed = True
                if len(picks) >= picks_target:
                    break

    if len(picks) < picks_target:
        for entry in sorted(scored, key=_pick_priority, reverse=True):
            if len(picks) >= picks_target:
                break
            if entry.link in used_links:
                continue
            picks.append(entry)
            used_links.add(entry.link)

    picks.sort(key=_pick_priority, reverse=True)
    buckets: dict[str, list[ScoredItem]] = {
        "High-Virality Instagram Picks": picks[: max(picks_target, maximum)]
    }

    # 2. Each category table, drawn only from stories not already in Picks,
    #    trimmed to the ceiling.
    for category in config.ASSIGNABLE_CATEGORIES:
        bucket = [e for e in native[category] if e.link not in used_links][:maximum]
        for entry in bucket:
            used_links.add(entry.link)
        buckets[category] = bucket

    # 3. Top up any category still short of `minimum`, pulling only stories
    #    that have not been placed anywhere yet (picks or another category).
    #    `overflow` is therefore consumed exactly once across every category,
    #    so the same story can never land in two different tables.
    overflow = [e for e in scored if e.link not in used_links]
    overflow_index = 0
    for category in config.ASSIGNABLE_CATEGORIES:
        bucket = buckets[category]
        while len(bucket) < minimum and overflow_index < len(overflow):
            candidate = overflow[overflow_index]
            overflow_index += 1
            if candidate.link in used_links:
                continue
            filler = ScoredItem(
                item=candidate.item,
                category=category,
                score=candidate.score,
                drivers=candidate.drivers,
                hook=candidate.hook,
                core_facts=candidate.core_facts,
                cta=candidate.cta,
                rationale=candidate.rationale + " (cross-filed)",
                fit_verdict=candidate.fit_verdict,
                fit_reason=candidate.fit_reason,
                components=candidate.components,
            )
            bucket.append(filler)
            used_links.add(candidate.link)
        bucket.sort(key=lambda s: -s.score)

    for category, bucket in buckets.items():
        want = picks_target if category == "High-Virality Instagram Picks" else minimum
        if len(bucket) < want:
            log.warning(
                "Category %r has only %d/%d items - the feed window was thin.",
                category, len(bucket), want,
            )
    return buckets


def flatten(briefing: dict[str, list[ScoredItem]]) -> list[ScoredItem]:
    return [entry for bucket in briefing.values() for entry in bucket]


def top_picks(briefing: dict[str, list[ScoredItem]], limit: int = 5) -> list[ScoredItem]:
    picks = briefing.get("High-Virality Instagram Picks", [])
    return picks[:limit]


# ---------------------------------------------------------------------------
# Optional Claude refinement
# ---------------------------------------------------------------------------

_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "hook": {"type": "string"},
                    "cta": {"type": "string"},
                },
                "required": ["index", "hook", "cta"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_REFINE_SYSTEM = (
    "You are an award-winning short-form news editor for an Indian Instagram "
    "news page. You rewrite reel hooks and CTAs for maximum retention.\n"
    "Hard rules:\n"
    "1. Never introduce a fact that is not in the supplied headline or facts.\n"
    "2. Never sensationalise beyond what the source supports; no clickbait "
    "that the story cannot pay off.\n"
    "3. Hook: max 120 characters, spoken-word rhythm, front-load the tension.\n"
    "4. CTA: one sentence, name the specific action (save / share / comment) "
    "and give a concrete reason tied to this story.\n"
    "5. Plain English with Indian-audience familiarity. No hashtags, no emoji."
)


def refine_with_claude(entries: list[ScoredItem], model: str | None = None) -> bool:
    """Polish hooks and CTAs in place. Returns True if refinement happened.

    Entirely optional: with no API key, no ``anthropic`` package, or any API
    error, the deterministic hooks are kept and the run continues.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        log.info("ANTHROPIC_API_KEY not set - using deterministic hooks/CTAs.")
        return False
    if not entries:
        return False

    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed - skipping refinement.")
        return False

    payload = [
        {
            "index": i,
            "category": e.category,
            "headline": e.item.title,
            "facts": e.core_facts,
            "primary_driver": e.drivers[0] if e.drivers else DRIVER_SHARE,
            "current_hook": e.hook,
            "current_cta": e.cta,
        }
        for i, e in enumerate(entries)
    ]

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model or config.CLAUDE_MODEL,
            max_tokens=16000,
            system=_REFINE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Rewrite the hook and CTA for each item below. Return "
                        "one object per item, preserving the index.\n\n"
                        + json.dumps(payload, ensure_ascii=False, indent=1)
                    ),
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": _REFINE_SCHEMA}},
        )
    except Exception as exc:  # network, auth, rate limit - all non-fatal here
        log.warning("Claude refinement unavailable (%s) - keeping engine output.", exc)
        return False

    try:
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
    except (StopIteration, json.JSONDecodeError, AttributeError) as exc:
        log.warning("Could not parse Claude refinement response: %s", exc)
        return False

    applied = 0
    for row in data.get("items", []):
        idx = row.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(entries):
            continue
        hook, cta = row.get("hook"), row.get("cta")
        if isinstance(hook, str) and hook.strip():
            entries[idx].hook = _shorten(hook.strip(), 140)
        if isinstance(cta, str) and cta.strip():
            entries[idx].cta = _shorten(cta.strip(), 160)
        applied += 1

    log.info("Claude refined %d/%d hooks and CTAs.", applied, len(entries))
    return applied > 0


def window_label(start: datetime, end: datetime) -> str:
    """Human-readable IST window label used in documents and messages."""
    start_ist = start.astimezone(config.IST)
    end_ist = end.astimezone(config.IST)
    if start_ist.date() == end_ist.date():
        return (
            f"{start_ist.strftime('%d %b %Y, %I:%M %p')} to "
            f"{end_ist.strftime('%I:%M %p')} IST"
        )
    return (
        f"{start_ist.strftime('%d %b %Y, %I:%M %p')} to "
        f"{end_ist.strftime('%d %b %Y, %I:%M %p')} IST"
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

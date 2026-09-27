"""India-only "will this get shares and comments?" ranking for the Telegram pulse.

The general virality score (virality_engine.score_item) rewards generic
newsiness: recency, how many outlets carried it, authority. That let foreign
diplomacy, stock-market predictions and explainers into the hourly Telegram
feed, which a current-affairs reel creator cannot use.

This module re-ranks for what the creator's own account data shows actually
drives shares and comments on Indian news reels:

* stories that change what ordinary people pay or must do (PF, UPI, GST,
  fuel and LPG prices, fees, fines, new rules, deadlines);
* scams and cyber fraud;
* shocking incidents (crime, accidents, stampedes, collapses, disasters);
* breaking news and alerts;
* genuine public controversy.

It also drops anything without a clear Indian connection. Everything here
is deterministic keyword matching on the headline and the publisher's
summary: it explains *why* a story ranked (the tags), and never invents
facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from analyzer.virality_engine import ScoredItem
from scrapers.rss_collector import jaccard, title_tokens


def _words(*terms: str) -> re.Pattern[str]:
    """Case-insensitive whole-word matcher for a list of terms."""
    escaped = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"(?<![\w])(?:" + "|".join(escaped) + r")(?![\w])", re.I)


# ---------------------------------------------------------------------------
# India relevance
# ---------------------------------------------------------------------------

_INDIA = _words(
    "india", "indian", "indians", "bharat", "desi",
    # states and union territories
    "andhra pradesh", "arunachal", "assam", "bihar", "chhattisgarh", "goa",
    "gujarat", "haryana", "himachal", "jharkhand", "karnataka", "kerala",
    "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
    "nagaland", "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "jammu", "kashmir", "ladakh", "puducherry", "chandigarh", "andaman",
    # big cities
    "delhi", "new delhi", "mumbai", "bengaluru", "bangalore", "chennai",
    "kolkata", "hyderabad", "pune", "ahmedabad", "jaipur", "lucknow",
    "patna", "bhopal", "indore", "surat", "kochi", "thiruvananthapuram",
    "coimbatore", "madurai", "trichy", "tiruchirappalli", "salem", "karur",
    "tiruppur", "vellore", "tirunelveli", "noida", "gurugram", "gurgaon",
    "ghaziabad", "nagpur", "visakhapatnam", "vijayawada", "varanasi",
    "kanpur", "agra", "amritsar", "ludhiana", "guwahati", "ranchi",
    "raipur", "dehradun", "srinagar", "mysuru", "mangaluru", "kolhapur",
    # people, parties, institutions
    "modi", "rahul gandhi", "amit shah", "stalin", "udhayanidhi", "yogi",
    "mamata", "kejriwal", "siddaramaiah", "revanth", "naidu", "bjp",
    "congress", "dmk", "aiadmk", "tvk", "aap", "tmc", "shiv sena", "ncp",
    "lok sabha", "rajya sabha", "rbi", "sebi", "epfo", "upi", "aadhaar",
    "isro", "irctc", "cbi", "nia", "bcci", "ipl", "rupee", "rupees",
    "crore", "lakh", "gst", "cbse", "neet", "jee", "uidai", "fssai",
    "iit", "aiims", "election commission", "indian railways",
    "vande bharat", "team india",
)

#: Strong foreign markers. A story that names these more often than Indian
#: ones, and has only a weak Indian link, is foreign news - dropped.
_FOREIGN = _words(
    "usa", "america", "american", "trump", "biden", "white house",
    "washington", "new york", "california", "texas", "mississippi",
    "uk", "britain", "british", "london", "europe", "european", "germany",
    "german", "france", "french", "spain", "madrid", "italy", "russia",
    "russian", "ukraine", "ukrainian", "kremlin", "israel", "gaza", "iran",
    "china", "chinese", "beijing", "japan", "tokyo", "korea", "bangkok",
    "thailand", "australia", "canada", "brazil", "mexico", "africa",
    "nato", "boeing",
)

#: Case-sensitive: "US" the country, not the pronoun "us"; "SIR" the voter-roll
#: revision, not "Sir" the title.
_US = re.compile(r"(?<![\w.])U\.?S\.?(?![\w])")
_SIR = re.compile(r"(?<![\w])SIR(?![\w])")

#: Leagues and sports that exist only abroad. Any mention rules a story out,
#: because they share abbreviations with Indian institutions (baseball's
#: "RBI" = runs batted in, not the Reserve Bank).
_FOREIGN_ONLY = _words(
    "mlb", "nfl", "nba", "nhl", "baseball", "inning", "innings pitched",
    "homer", "touchdown", "super bowl", "premier league", "la liga",
    "serie a", "bundesliga",
)

#: Indian readers care about these countries mainly when India is named too.
_NEIGHBOURS = _words("pakistan", "bangladesh", "nepal", "sri lanka", "china")


def india_relevance(entry: ScoredItem) -> tuple[bool, int]:
    """Return ``(is_indian, strength)`` for a story.

    The headline counts double: a story whose headline is about India is
    Indian even if the summary mentions another country.
    """
    title = entry.item.title
    summary = entry.item.summary or ""
    def hits(text: str, *patterns: re.Pattern[str]) -> int:
        return sum(len(p.findall(text)) for p in patterns)

    if _FOREIGN_ONLY.search(title) or _FOREIGN_ONLY.search(summary):
        return False, 0
    india = 2 * hits(title, _INDIA, _SIR) + hits(summary, _INDIA, _SIR)
    foreign = 2 * hits(title, _FOREIGN, _US) + hits(summary, _FOREIGN, _US)
    if india == 0:
        return False, 0
    if foreign > india and hits(title, _INDIA, _SIR) == 0:
        return False, india
    return True, india


# ---------------------------------------------------------------------------
# What drives shares and comments
# ---------------------------------------------------------------------------

#: Money and rules that change what ordinary people pay or must do. This is
#: the pattern behind the account's best reel (PF wage limit + UPI charges +
#: a fine: 121,570 views, 1,391 shares).
_MONEY_RULES = _words(
    "pf", "epfo", "provident fund", "pension", "salary", "salaries", "wage",
    "upi", "gst", "income tax", "tax", "itr", "tds", "petrol", "diesel",
    "fuel", "lpg", "cylinder", "gold price", "silver price", "price hike",
    "prices", "hike", "cheaper", "costlier", "fee", "fees", "fine", "fined",
    "penalty", "toll", "fastag", "ration", "aadhaar", "pan card",
    "bank account", "bank accounts", "bank holiday", "minimum balance",
    "atm", "loan", "emi", "fd", "fixed deposit", "interest rate",
    "repo rate", "electricity bill", "power cut", "new rule", "new rules",
    "rule change", "rules change", "from today", "from tomorrow",
    "from october", "from november", "from 1", "deadline", "last date",
    "ban", "banned", "mandatory", "compulsory", "scheme", "subsidy",
    "free", "refund", "ticket", "railway", "train fare", "holiday",
    "holidays", "school", "schools", "exam", "result", "results",
    "driving licence", "challan", "traffic rule", "rto",
)

#: Scams and cyber fraud - the account's most-saved reel was a scam story.
_SCAM = _words(
    "scam", "scams", "scammed", "fraud", "frauds", "fraudster", "cheated",
    "duped", "cyber crime", "cybercrime", "cyber fraud", "digital arrest",
    "phishing", "fake", "otp", "hacked", "hacking", "leak", "leaked",
    "data breach", "ponzi", "fake call", "sextortion",
)

#: Shocking incidents: crime, accidents, disasters, public-safety alerts.
_SHOCK = _words(
    "killed", "dead", "dies", "died", "death", "deaths", "murder",
    "murdered", "rape", "raped", "assault", "assaulted", "attack",
    "attacked", "shot", "stabbed", "lynched", "arrested", "abducted",
    "kidnapped", "missing", "stampede", "collapse", "collapses",
    "collapsed", "explosion", "blast", "fire", "accident", "crash",
    "crashed", "derail", "derailed", "hooch", "poisoning", "adulterated",
    "adulteration", "flood", "floods", "landslide", "cyclone", "earthquake",
    "heavy rain", "red alert", "orange alert", "outbreak", "dengue",
    "shocking", "horror", "brutal", "caught on camera", "viral video",
    "cctv", "pocso", "dowry", "acid attack",
)

_BREAKING = _words(
    "breaking", "just in", "alert", "warning", "live updates", "urgent",
    "big update", "announced", "announces", "approved", "approves",
)

#: Public controversy that people argue about in the comments.
_DEBATE = _words(
    "row", "controversy", "slams", "backlash", "outrage", "protest",
    "protests", "boycott", "accused", "allegation", "alleges", "hits back",
    "demands", "opposition", "remark", "remarks", "vs", "clash",
    "nrc", "caa", "reservation", "quota",
)

#: Stories that rarely work as reels for a general Indian audience.
_LOW_VALUE = re.compile(
    r"stock market prediction|stocks? to buy|share price target|price target"
    r"|sensex,? nifty (?:today|tomorrow|prediction)|trade setup|gift nifty"
    r"|horoscope|astrology|zodiac|lucky (?:number|colour)"
    r"|how to watch|lineups?\b|playing xi\b|predicted xi|fantasy (?:team|tips)"
    r"|q[1-4] results|quarterly results|results preview"
    r"|inaugurat|chairs? (?:a )?meeting|reviews? (?:the )?progress|greetings"
    r"|webinar|\bmou\b|conclave|summit to be held"
    r"|recipe|weekend getaway|best (?:phones|laptops|deals)|deal of the day",
    re.I,
)

#: The creator makes Tamil-language content; Tamil Nadu stories are flagged
#: so they are easy to spot, and get a small nudge.
_TAMIL_NADU = _words(
    "tamil nadu", "chennai", "coimbatore", "madurai", "trichy",
    "tiruchirappalli", "salem", "karur", "tiruppur", "vellore",
    "tirunelveli", "stalin", "udhayanidhi", "dmk", "aiadmk", "tvk",
)

#: Display order, label and weight of each reason tag.
TAGS: tuple[tuple[str, str, re.Pattern[str], float], ...] = (
    ("money", "\U0001F4B0 Money / rules", _MONEY_RULES, 16.0),
    ("scam", "\U0001F6A8 Scam alert", _SCAM, 14.0),
    ("shock", "\U0001F631 Shocking", _SHOCK, 12.0),
    ("breaking", "⚡ Breaking", _BREAKING, 7.0),
    ("debate", "\U0001F5E3 Debate", _DEBATE, 8.0),
)
TAMIL_NADU_LABEL = "\U0001F4CD Tamil Nadu"
TAMIL_NADU_BONUS = 4.0
LOW_VALUE_PENALTY = 25.0

#: One comment prompt per lead tag. The account's two highest-comment reels
#: both asked viewers to comment a keyword ("comment ID": 2,722 comments vs
#: a typical 9), so the prompts reuse that pattern where it fits.
COMMENT_PROMPTS = {
    "money": "Ask viewers to comment “DETAILS” for what changes for them",
    "scam": "Ask viewers to comment “SAFE” for how to protect themselves",
    "shock": "Ask viewers: what should happen next? Comment below",
    "breaking": "Ask viewers to follow for updates on this story",
    "debate": "Ask viewers: agree or disagree? Comment below",
}


PROMPT_PRIORITY = ("scam", "money", "debate", "shock", "breaking")


@dataclass
class CreatorPick:
    entry: ScoredItem
    creator_score: int
    tags: list[str]          # tag keys in display order, e.g. ["money", "breaking"]
    tamil_nadu: bool
    india_strength: int

    @property
    def labels(self) -> list[str]:
        by_key = {key: label for key, label, _, _ in TAGS}
        labels = [by_key[k] for k in self.tags]
        if self.tamil_nadu:
            labels.append(TAMIL_NADU_LABEL)
        return labels

    @property
    def comment_prompt(self) -> str:
        """The prompt for the most comment-worthy reason the story has.

        Priority differs from display order: a story that is both a public
        row and shocking gets "agree or disagree?", which invites more
        replies than "what should happen next?".
        """
        for key in PROMPT_PRIORITY:
            if key in self.tags:
                return COMMENT_PROMPTS[key]
        return ""


def assess(entry: ScoredItem) -> CreatorPick | None:
    """Score one story for the creator feed, or ``None`` if it is not Indian."""
    is_indian, strength = india_relevance(entry)
    if not is_indian:
        return None

    title = entry.item.title
    text = f"{title} {entry.item.summary or ''}"
    points = float(entry.score)
    tags: list[str] = []
    for key, _, pattern, weight in TAGS:
        in_title = len(pattern.findall(title))
        in_text = len(pattern.findall(text))
        if in_text:
            tags.append(key)
            # A reason stated in the headline is what the viewer sees first.
            points += weight * (1.0 if in_title else 0.5)
            if in_title >= 2:
                points += weight * 0.25

    tamil_nadu = bool(_TAMIL_NADU.search(text))
    if tamil_nadu:
        points += TAMIL_NADU_BONUS
    if _LOW_VALUE.search(title):
        points -= LOW_VALUE_PENALTY
    if not tags:
        # Indian, but nothing that reliably drives shares or comments.
        points -= 10.0

    return CreatorPick(
        entry=entry,
        creator_score=int(max(1, min(100, round(points)))),
        tags=tags,
        tamil_nadu=tamil_nadu,
        india_strength=strength,
    )


def rank(entries: list[ScoredItem]) -> tuple[list[CreatorPick], int]:
    """Assess every story; return India-only picks best-first and how many
    foreign or unrelated stories were dropped."""
    picks = [p for p in (assess(e) for e in entries) if p is not None]
    picks.sort(key=lambda p: (-p.creator_score, -p.entry.item.corroboration))
    return picks, len(entries) - len(picks)


#: Two headlines sharing this much of their significant words are the same
#: event reported by different outlets (e.g. two write-ups of one
#: anniversary). Looser than feed deduplication on purpose: the pulse shows
#: each event once.
SAME_TOPIC_THRESHOLD = 0.30


def drop_same_topic(picks: list[CreatorPick]) -> tuple[list[CreatorPick], int]:
    """Keep the strongest pick per event; return the kept picks (order kept)
    and how many same-topic repeats were removed. ``picks`` must already be
    sorted best-first."""
    kept: list[CreatorPick] = []
    kept_tokens: list[frozenset[str]] = []
    for pick in picks:
        tokens = title_tokens(pick.entry.item.title)
        if any(jaccard(tokens, other) >= SAME_TOPIC_THRESHOLD for other in kept_tokens):
            continue
        kept.append(pick)
        kept_tokens.append(tokens)
    return kept, len(picks) - len(kept)

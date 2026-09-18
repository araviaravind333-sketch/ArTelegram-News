"""Compact Telegram digest for the hourly news pulse.

Written for a phone screen: every story fits in a few lines, the hook leads,
and the source is a tappable link rather than a raw URL. Telegram HTML parse
mode is used, so all interpolated text must be escaped.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

import config
from analyzer.virality_engine import (
    DRIVER_DEBATE,
    DRIVER_SAVE,
    DRIVER_SHARE,
    ScoredItem,
)

#: A glance-level cue for the primary driver.
DRIVER_ICONS = {
    DRIVER_SHARE: "\U0001F501",    # repeat
    DRIVER_SAVE: "\U0001F4CC",     # pushpin
    DRIVER_DEBATE: "\U0001F4AC",   # speech balloon
}

#: Score band -> flame count, so the editor can triage without reading numbers.
SCORE_BANDS = ((85, "\U0001F525\U0001F525\U0001F525"),
               (70, "\U0001F525\U0001F525"),
               (55, "\U0001F525"),
               (0, ""))

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WORD = re.compile(r"[a-z0-9]+")


def _escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def _band(score: int) -> str:
    for threshold, flames in SCORE_BANDS:
        if score >= threshold:
            return flames
    return ""


def _short_source(entry: ScoredItem) -> str:
    """Publisher name, falling back to the link's host."""
    publisher = (entry.publisher or "").strip()
    if publisher and publisher.lower() != "unknown":
        return publisher
    host = entry.link.split("//")[-1].split("/")[0]
    return host[4:] if host.startswith("www.") else host


def _context_only(entry: ScoredItem) -> str:
    """The facts minus whatever the hook already said.

    ``core_facts`` opens with the headline, which the hook also carries. In a
    table those sit in separate columns, but in a chat message it reads as the
    same sentence twice, so the echo is trimmed here. If nothing but the
    headline exists, the full facts are kept rather than showing nothing.
    """
    facts = (entry.core_facts or "").strip()
    if not facts:
        return ""

    sentences = _SENTENCE_SPLIT.split(facts)
    if len(sentences) < 2:
        return facts

    head_words = set(_WORD.findall(entry.hook.lower()))
    first_words = set(_WORD.findall(sentences[0].lower()))
    if first_words and len(first_words & head_words) / len(first_words) >= 0.7:
        remainder = " ".join(s.strip() for s in sentences[1:]).strip()
        return remainder or facts
    return facts


def format_story(entry: ScoredItem, index: int) -> str:
    """One numbered story block."""
    icon = DRIVER_ICONS.get(entry.drivers[0] if entry.drivers else "", "")
    flames = _band(entry.score)

    lines = [
        f"<b>{index}. {_escape(entry.hook)}</b>",
        f"   {_escape(_context_only(entry))}",
        f"   {icon} <b>{entry.score}</b>{(' ' + flames) if flames else ''} · "
        f"{_escape(entry.drivers_text)}",
        f"   <i>CTA:</i> {_escape(entry.cta)}",
        f"   <a href=\"{_escape(entry.link)}\">{_escape(_short_source(entry))}</a>"
        f" · {entry.published_ist.strftime('%I:%M %p')}",
    ]
    return "\n".join(lines)


def format_pulse(
    entries: list[ScoredItem],
    generated_at: datetime | None = None,
    account: str = "Aravind News 24",
) -> str:
    """Render the full pulse message.

    Returns an empty string when there is nothing to post, which the caller
    treats as "stay silent" rather than sending a no-news notice.
    """
    if not entries:
        return ""

    now_ist = (generated_at or datetime.now(config.UTC)).astimezone(config.IST)
    count = len(entries)
    noun = "new story" if count == 1 else "new stories"

    header = (
        f"\U0001F4F0 <b>NEWS PULSE</b> · {now_ist.strftime('%I:%M %p IST')}\n"
        f"<i>{count} {noun} since the last check</i>"
    )
    body = "\n\n".join(
        format_story(entry, i) for i, entry in enumerate(entries, start=1)
    )

    top = max(entries, key=lambda e: e.score)
    footer = (
        f"<i>Lead with #{entries.index(top) + 1} — highest virality score "
        f"({top.score}) in this batch. {_escape(account)}.</i>"
    )
    # Blank lines between the three blocks; Telegram renders them as spacing.
    return f"{header}\n\n{body}\n\n{footer}"

"""Compact Telegram digest for the hourly India news pulse.

Written for a current-affairs reel creator reading on a phone: each story is
the real headline, one line of facts, why it is likely to get shares and
comments, a comment prompt to use in the reel, and a tappable source link.
No template hooks ("This one deserves a share: ...") - the headline itself
has to be the reason to make the reel. Telegram HTML parse mode is used, so
all interpolated text is escaped.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

import config
from analyzer.creator_filter import CreatorPick

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'‘“])")
_WORD = re.compile(r"[a-z0-9]+")


def _escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def _short_source(pick: CreatorPick) -> str:
    """Publisher name, falling back to the link's host."""
    publisher = (pick.entry.publisher or "").strip()
    if publisher and publisher.lower() != "unknown":
        return publisher
    host = pick.entry.link.split("//")[-1].split("/")[0]
    return host[4:] if host.startswith("www.") else host


def _fact_line(pick: CreatorPick) -> str:
    """One sentence of facts that the headline does not already say.

    ``core_facts`` starts with the headline and, when the publisher gave no
    summary, ends with a "Reported by ... carried by N feeds" note; neither
    adds anything for the reader, so both are dropped. Returns ``""`` when
    nothing new is left.
    """
    headline_words = set(_WORD.findall(pick.entry.item.title.lower()))
    for sentence in _SENTENCE_SPLIT.split(pick.entry.core_facts or ""):
        sentence = sentence.strip()
        if not sentence or sentence.lower().startswith("reported by"):
            continue
        words = set(_WORD.findall(sentence.lower()))
        if words and len(words & headline_words) / len(words) >= 0.7:
            continue  # the headline again
        return sentence
    return ""


def format_story(pick: CreatorPick, index: int) -> str:
    """One numbered story block."""
    lines = [f"<b>{index}. {_escape(pick.entry.item.title)}</b>"]
    fact = _fact_line(pick)
    if fact:
        lines.append(f"   {_escape(fact)}")
    reasons = " · ".join(pick.labels + [f"<b>{pick.creator_score}</b>/100"])
    lines.append(f"   {reasons}")
    if pick.comment_prompt:
        lines.append(f"   \U0001F4AC {_escape(pick.comment_prompt)}")
    lines.append(
        f'   <a href="{html.escape(pick.entry.link, quote=True)}">'
        f"{_escape(_short_source(pick))}</a>"
        f" · {pick.entry.published_ist.strftime('%I:%M %p')}"
    )
    return "\n".join(lines)


def format_pulse(
    picks: list[CreatorPick],
    generated_at: datetime | None = None,
) -> str:
    """Render the full pulse message.

    Returns an empty string when there is nothing to post, which the caller
    treats as "stay silent" rather than sending a no-news notice.
    """
    if not picks:
        return ""

    now_ist = (generated_at or datetime.now(config.UTC)).astimezone(config.IST)
    count = len(picks)
    noun = "story" if count == 1 else "stories"

    header = (
        f"\U0001F1EE\U0001F1F3 <b>INDIA NEWS PULSE</b> · {now_ist.strftime('%I:%M %p IST')}\n"
        f"<i>{count} new India {noun} most likely to get shares and comments</i>"
    )
    body = "\n\n".join(format_story(pick, i) for i, pick in enumerate(picks, start=1))
    footer = (
        "<i>Ranked by: money and rule changes, scams, shocking incidents, "
        "breaking news and public debate. Foreign news is left out.</i>"
    )
    return f"{header}\n\n{body}\n\n{footer}"

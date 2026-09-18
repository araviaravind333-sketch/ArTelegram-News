"""Entry point for both workflows and for on-demand Telegram commands.

CLI
---
    python main.py scan                       # last 24 hours
    python main.py scan --minutes 25          # rolling 25-minute scanner
    python main.py scan --hours 6
    python main.py scan --window "17-09-26 06:00 to now"
    python main.py audit                      # 10:00 AM IST reels audit
    python main.py listen                     # poll Telegram for /scan, /audit
    python main.py scan --dry-run             # build the .docx, send nothing

Telegram
--------
    /scan                       last 24 hours
    /scan 25m                   last 25 minutes
    /scan last 6 hours
    /scan 17-09-26 06:00 to now
    /scan 17-09-26 06:00 to 18-09-26 09:30
    /audit                      run the reels audit now
    /help, /ping
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from analyzer import virality_engine
from generators import doc_generator, pulse_formatter
from scrapers import rss_collector, seen_store
from services.telegram_notifier import TelegramError, TelegramNotifier

log = logging.getLogger("aravindnews24")


# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------

class WindowParseError(ValueError):
    """Raised when a user-supplied time window cannot be understood."""


_RELATIVE_RE = re.compile(
    r"^(?:last\s+)?(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.I,
)
_DATE_FORMATS = (
    ("%d-%m-%y", "DD-MM-YY"),
    ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%d/%m/%y", "DD/MM/YY"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
)
_TIME_FORMATS = ("%H:%M", "%H.%M", "%I:%M %p", "%I%p", "%H")


def _parse_timestamp(text: str, now_ist: datetime) -> datetime:
    """Parse ``DD-MM-YY HH:MM``, a bare date, a bare time, or ``now``."""
    cleaned = " ".join(text.strip().split())
    lowered = cleaned.lower()

    if lowered in {"now", "today", "current"}:
        return now_ist
    if lowered == "yesterday":
        return (now_ist - timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                     microsecond=0)

    # <date> <time>
    parts = cleaned.split(" ", 1)
    date_text = parts[0]
    time_text = parts[1] if len(parts) > 1 else ""

    parsed_date = None
    for fmt, _ in _DATE_FORMATS:
        try:
            parsed_date = datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue

    if parsed_date is None:
        # Maybe the whole string is just a time, e.g. "06:00".
        for fmt in _TIME_FORMATS:
            try:
                parsed_time = datetime.strptime(cleaned.upper(), fmt).time()
                return config.IST.localize(
                    datetime.combine(now_ist.date(), parsed_time)
                )
            except ValueError:
                continue
        raise WindowParseError(
            f"Could not read {text!r} as a date or time. "
            "Use DD-MM-YY HH:MM (e.g. 17-09-26 06:00) or 'now'."
        )

    if time_text:
        parsed_time = None
        for fmt in _TIME_FORMATS:
            try:
                parsed_time = datetime.strptime(time_text.upper().strip(), fmt).time()
                break
            except ValueError:
                continue
        if parsed_time is None:
            raise WindowParseError(
                f"Could not read {time_text!r} as a time. Use HH:MM, e.g. 06:00."
            )
    else:
        parsed_time = datetime.min.time()

    return config.IST.localize(datetime.combine(parsed_date, parsed_time))


def parse_window(spec: str | None, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Turn a window expression into an aware ``(start, end)`` UTC pair.

    Dates are read Indian-style (day first), so ``17-09-26`` is 17 Sep 2026.
    Everything is interpreted in IST, then converted to UTC for filtering.
    """
    now_utc = now or datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(config.IST)

    if not spec or not spec.strip():
        return now_utc - timedelta(hours=config.DEFAULT_SCAN_HOURS), now_utc

    text = " ".join(spec.strip().split())
    lowered = text.lower()

    if lowered in {"now", "latest", "fast", "quick"}:
        return now_utc - timedelta(minutes=config.FAST_SCAN_MINUTES), now_utc
    if lowered in {"today", "since midnight"}:
        midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(timezone.utc), now_utc
    if lowered == "yesterday":
        start_ist = (now_ist - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_ist = start_ist + timedelta(days=1)
        return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)

    relative = _RELATIVE_RE.match(lowered)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        if unit.startswith("m"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("h"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        if delta <= timedelta(0):
            raise WindowParseError("The window length must be greater than zero.")
        return now_utc - delta, now_utc

    # "<start> to <end>" / "<start> - <end>"
    separator = re.search(r"\s+(?:to|until|till|-{1,2}|–)\s+", text, re.I)
    if separator:
        start_text = text[: separator.start()].strip()
        end_text = text[separator.end():].strip()
        start_ist = _parse_timestamp(start_text, now_ist)
        end_ist = _parse_timestamp(end_text, now_ist)
        if end_ist <= start_ist:
            raise WindowParseError(
                "The end of the window must be after its start "
                f"({start_ist:%d-%m-%y %H:%M} -> {end_ist:%d-%m-%y %H:%M} IST)."
            )
        return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)

    # A single timestamp means "from then until now".
    start_ist = _parse_timestamp(text, now_ist)
    if start_ist >= now_ist:
        raise WindowParseError(
            f"{text!r} resolves to the future ({start_ist:%d-%m-%y %H:%M} IST)."
        )
    return start_ist.astimezone(timezone.utc), now_utc


# ---------------------------------------------------------------------------
# Workflow 1 - news scan
# ---------------------------------------------------------------------------

def run_scan(
    window: str | None = None,
    dry_run: bool = False,
    notifier: TelegramNotifier | None = None,
    chat_id: str | None = None,
    refine: bool = True,
) -> Path:
    """Scrape, score, build the .docx and (unless dry-run) push it to Telegram."""
    start, end = parse_window(window)
    label = virality_engine.window_label(start, end)
    log.info("Scan window: %s", label)

    if notifier and not dry_run:
        try:
            notifier.send_message(
                f"\U0001F50E <b>Scanning news</b>\n{notifier.escape(label)}\n"
                f"<i>Aggregating {len(rss_collector.FEEDS)} free sources…</i>",
                chat_id=chat_id,
            )
        except TelegramError as exc:
            log.warning("Could not send scan acknowledgement: %s", exc)

    items, effective_start = rss_collector.collect(start, end)
    if not items:
        raise RuntimeError(
            "No stories were returned by any feed in this window. "
            "Either every source is unreachable or the window is too narrow."
        )

    briefing = virality_engine.build_briefing(items)

    if refine:
        # Only the picks are sent to Claude: they are what gets produced today,
        # and it keeps the optional API call small and cheap.
        virality_engine.refine_with_claude(
            briefing.get("High-Virality Instagram Picks", [])
        )

    path = doc_generator.generate(
        briefing,
        effective_start,
        end,
        source_count=len(rss_collector.FEEDS),
    )

    total = sum(len(v) for v in briefing.values())
    picks = virality_engine.top_picks(briefing, 3)

    if dry_run:
        log.info("Dry run — document at %s (not sent)", path)
        return path

    notifier = notifier or TelegramNotifier()
    headline_block = "\n".join(
        f"  {i}. <b>{notifier.escape(p.hook)}</b>\n"
        f"     Score {p.score} • {notifier.escape(p.drivers_text)}"
        for i, p in enumerate(picks, start=1)
    )
    caption = (
        f"\U0001F4F0 <b>Virality Briefing</b>\n"
        f"{notifier.escape(virality_engine.window_label(effective_start, end))}\n"
        f"{total} stories • {len(config.CATEGORIES)} categories\n\n"
        f"<b>Top picks</b>\n{headline_block}"
    )
    notifier.send_document(path, caption=caption, chat_id=chat_id)
    return path


# ---------------------------------------------------------------------------
# Workflow 1b - the hourly news pulse
# ---------------------------------------------------------------------------

def run_pulse(
    dry_run: bool = False,
    notifier: TelegramNotifier | None = None,
    chat_id: str | None = None,
    limit: int | None = None,
    min_score: int | None = None,
    refine: bool = True,
) -> int:
    """Post only the stories that are new since the previous run.

    Returns the number of stories posted. Zero means the run stayed silent,
    which is the normal outcome during quiet hours and is not an error.

    The seen-store, not the time window, decides what counts as new: feeds lag
    behind publication, so the scan looks back further than the 1-hour interval and
    lets the ledger filter out anything already sent.
    """
    end = virality_engine.now_utc()
    start = end - timedelta(minutes=config.PULSE_LOOKBACK_MINUTES)
    log.info("Pulse window: %s", virality_engine.window_label(start, end))

    # No auto-widening here: a pulse reports what just happened. If the last
    # 90 minutes were quiet, the correct output is silence.
    raw = rss_collector.fetch_all()
    unique = rss_collector.deduplicate(raw)
    in_window = rss_collector.filter_by_window(unique, start, end)

    store = seen_store.load()
    fresh = seen_store.filter_new(in_window, store)
    if not fresh:
        log.info("Nothing new since the last run - staying silent.")
        return 0

    performance = virality_engine.load_category_performance()
    scored = [virality_engine.analyse(item, performance) for item in fresh]

    threshold = config.PULSE_MIN_SCORE if min_score is None else min_score
    strong = [entry for entry in scored if entry.score >= threshold]
    if not strong:
        log.info(
            "%d new stories but none scored >= %d - holding them back.",
            len(scored), threshold,
        )
        # Still mark them seen: they were judged and rejected, and re-judging
        # the same weak stories every hour wastes the whole cycle.
        if not dry_run:
            seen_store.save(seen_store.mark(fresh, store))
        return 0

    strong.sort(key=lambda e: -e.score)
    cap = limit or config.PULSE_MAX_ITEMS
    selected = strong[:cap]
    overflow = strong[cap:]

    if refine:
        virality_engine.refine_with_claude(selected)

    message = pulse_formatter.format_pulse(selected, generated_at=end)

    if dry_run:
        print(message)
        log.info("Dry run - %d stories not sent, seen-store untouched", len(selected))
        return len(selected)

    notifier = notifier or TelegramNotifier()
    notifier.send_message(message, chat_id=chat_id, disable_preview=True)

    # Record only after delivery succeeds, so a Telegram outage retries these
    # stories next run instead of silently dropping them.
    #
    # What gets marked matters. Posted stories obviously do, and so do the
    # ones that scored below the threshold - re-judging the same weak stories
    # every hour would burn the whole cycle. Strong stories that merely
    # overflowed the per-post cap are deliberately left UNMARKED, so they roll
    # into the next pulse and compete again rather than being suppressed
    # forever. Recency decay lowers their score each cycle, so they either get
    # posted soon or fall below the threshold and retire on their own.
    posted_links = {e.link for e in selected}
    overflow_links = {e.link for e in overflow}
    to_mark = [
        item for item in fresh
        if item.link in posted_links or item.link not in overflow_links
    ]
    seen_store.save(seen_store.mark(to_mark, store))
    log.info(
        "Pulse delivered: %d posted, %d held for the next run, %d judged",
        len(selected), len(overflow), len(fresh),
    )
    return len(selected)


# ---------------------------------------------------------------------------
# Workflow 2 - reels audit
# ---------------------------------------------------------------------------

def run_audit(
    dry_run: bool = False,
    notifier: TelegramNotifier | None = None,
    chat_id: str | None = None,
    with_news: bool = True,
) -> str:
    """Audit recent reels and push the executive action plan to Telegram."""
    # Imported lazily so a news-only run never needs Instagram credentials.
    from services import instagram_auditor

    picks: list = []
    if with_news:
        try:
            start, end = parse_window(f"{config.DEFAULT_SCAN_HOURS}h")
            items, _ = rss_collector.collect(start, end, widen_if_thin=25)
            briefing = virality_engine.build_briefing(items)
            picks = virality_engine.top_picks(briefing, 3)
            virality_engine.refine_with_claude(picks)
        except Exception as exc:
            # The audit is the deliverable; today's news is a bonus input.
            log.warning("News context unavailable for the roadmap: %s", exc)

    plan, result = instagram_auditor.run_audit(news_picks=picks)
    log.info(
        "Audit complete: %d reels, %d winners, %d flops",
        len(result.reels), len(result.winners), len(result.flops),
    )

    if dry_run:
        print(plan)
        return plan

    notifier = notifier or TelegramNotifier()
    notifier.send_message(plan, chat_id=chat_id, disable_preview=True)
    return plan


# ---------------------------------------------------------------------------
# Telegram command handling
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "<b>Aravind News 24 — command reference</b>\n\n"
    "<b>/pulse</b> — post whatever is new since the last check\n"
    "   (this runs automatically every hour)\n\n"
    "<b>/scan</b> — virality briefing for the last 24 hours\n"
    "<b>/scan 25m</b> — the rolling 25-minute scan\n"
    "<b>/scan last 6 hours</b>\n"
    "<b>/scan 17-09-26 06:00 to now</b>\n"
    "<b>/scan 17-09-26 06:00 to 18-09-26 09:30</b>\n"
    "   (dates are day-first: 17-09-26 = 17 Sep 2026, all times IST)\n\n"
    "<b>/audit</b> — run the reels audit and 1M-view roadmap now\n"
    "<b>/ping</b> — check the bot is alive\n"
    "<b>/help</b> — this message"
)


def handle_command(text: str, chat_id: str, notifier: TelegramNotifier) -> None:
    """Dispatch one inbound Telegram message. Never raises."""
    raw = text.strip()
    command, _, argument = raw.partition(" ")
    command = command.split("@", 1)[0].lower()   # strip @BotName in groups
    argument = argument.strip()

    try:
        if command in {"/scan", "/news", "/brief"}:
            run_scan(argument or None, notifier=notifier, chat_id=chat_id)
        elif command in {"/pulse", "/latest"}:
            posted = run_pulse(notifier=notifier, chat_id=chat_id)
            if not posted:
                notifier.send_message(
                    "\U0001F634 No new stories since the last check.",
                    chat_id=chat_id,
                )
        elif command in {"/audit", "/reels", "/insights"}:
            notifier.send_message(
                "\U0001F4CA <b>Running reels audit…</b>", chat_id=chat_id
            )
            run_audit(notifier=notifier, chat_id=chat_id)
        elif command in {"/help", "/start"}:
            notifier.send_message(HELP_TEXT, chat_id=chat_id)
        elif command == "/ping":
            now = datetime.now(config.IST).strftime("%d %b %Y, %I:%M:%S %p IST")
            notifier.send_message(f"✅ Alive — {now}", chat_id=chat_id)
        else:
            return  # ignore ordinary chatter
    except WindowParseError as exc:
        notifier.send_message(
            f"⚠️ <b>I could not read that time window.</b>\n"
            f"{notifier.escape(str(exc))}\n\n{HELP_TEXT}",
            chat_id=chat_id,
        )
    except Exception as exc:
        log.exception("Command %r failed", raw)
        notifier.send_error(f"Command {command}", exc)


def run_listener(runtime_seconds: int | None = None) -> int:
    """Poll Telegram for commands until the time budget expires."""
    notifier = TelegramNotifier()
    offset = notifier.drain()
    handled = 0
    for text, chat_id, _ in notifier.iter_commands(runtime_seconds, offset=offset):
        if text.startswith("/"):
            handle_command(text, chat_id, notifier)
            handled += 1
    log.info("Listener finished — %d command(s) handled", handled)
    return handled


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Aravind News 24 — news virality briefing and reels audit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug-level logging")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="build and deliver the virality briefing")
    group = scan.add_mutually_exclusive_group()
    group.add_argument("--window", help='e.g. "17-09-26 06:00 to now"')
    group.add_argument("--hours", type=float, help="look back this many hours")
    group.add_argument("--minutes", type=float, help="look back this many minutes")
    scan.add_argument("--dry-run", action="store_true",
                      help="write the .docx but send nothing")
    scan.add_argument("--no-refine", action="store_true",
                      help="skip the optional Claude hook/CTA pass")

    pulse = sub.add_parser(
        "pulse", help="post only stories new since the last run (20-min cadence)"
    )
    pulse.add_argument("--dry-run", action="store_true",
                       help="print the message and leave the seen-store untouched")
    pulse.add_argument("--limit", type=int, default=None,
                       help=f"max stories per post (default {config.PULSE_MAX_ITEMS})")
    pulse.add_argument("--min-score", type=int, default=None,
                       help=f"hold back below this score (default {config.PULSE_MIN_SCORE})")
    pulse.add_argument("--no-refine", action="store_true",
                       help="skip the optional Claude hook/CTA pass")
    pulse.add_argument("--reset-seen", action="store_true",
                       help="clear the seen-store first (re-posts recent stories)")

    audit = sub.add_parser("audit", help="run the daily reels audit")
    audit.add_argument("--dry-run", action="store_true",
                       help="print the action plan instead of sending it")
    audit.add_argument("--no-news", action="store_true",
                       help="skip the news scan that feeds the roadmap")

    listen = sub.add_parser("listen", help="poll Telegram for on-demand commands")
    listen.add_argument("--seconds", type=int, default=None,
                        help="how long to poll (default: LISTENER_RUNTIME_SECONDS)")

    sub.add_parser("check", help="validate configuration and exit")
    return parser


def run_check() -> int:
    """Report which credentials are present without printing any of them."""
    print("Configuration check")
    print("-" * 52)
    ok = True
    groups = {
        "Telegram (required for delivery)": config.TELEGRAM_SECRETS,
        "Instagram (required for the audit)": config.INSTAGRAM_SECRETS,
        "Claude (optional refinement)": ("ANTHROPIC_API_KEY",),
    }
    for label, names in groups.items():
        print(f"\n{label}")
        for name in names:
            value = config.get(name)
            status = "set" if value else "MISSING"
            print(f"  {name:<22} {status:<8} {config.masked(value)}")
            if not value and name != "ANTHROPIC_API_KEY":
                ok = False
    print(f"\nFeeds registered: {len(rss_collector.FEEDS)}")
    print(f"Categories:       {len(config.CATEGORIES)}")
    print(f"Min per category: {config.MIN_ITEMS_PER_CATEGORY}")
    print(f"Output directory: {config.OUTPUT_DIR}")

    print("\nNews pulse")
    print(f"  cadence:        every {config.PULSE_MINUTES} min")
    print(f"  lookback:       {config.PULSE_LOOKBACK_MINUTES} min "
          f"(absorbs feed and scheduler lag)")
    print(f"  max per post:   {config.PULSE_MAX_ITEMS}")
    print(f"  min score:      {config.PULSE_MIN_SCORE}")
    print(f"  seen-store TTL: {config.SEEN_TTL_HOURS}h")
    if config.SEEN_FILE.is_file():
        try:
            entries = len(json.loads(config.SEEN_FILE.read_text(encoding="utf-8")))
            print(f"  seen-store:     {entries} entries at {config.SEEN_FILE.name}")
        except (OSError, ValueError):
            print(f"  seen-store:     present but unreadable")
    else:
        print("  seen-store:     empty (first run will post the current window)")
    print("\nResult:", "READY" if ok else "INCOMPLETE — see MISSING above")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows consoles default to a legacy code page; force UTF-8 so log lines
    # and the action plan render correctly both locally and in CI.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    config.ensure_directories()

    try:
        if args.command == "check":
            return run_check()

        if args.command == "scan":
            window = args.window
            if args.hours:
                window = f"{args.hours:g}h"
            elif args.minutes:
                window = f"{args.minutes:g}m"
            path = run_scan(
                window, dry_run=args.dry_run, refine=not args.no_refine
            )
            print(f"Briefing: {path}")
            return 0

        if args.command == "pulse":
            if args.reset_seen:
                config.SEEN_FILE.unlink(missing_ok=True)
                log.info("Seen-store cleared.")
            posted = run_pulse(
                dry_run=args.dry_run,
                limit=args.limit,
                min_score=args.min_score,
                refine=not args.no_refine,
            )
            print(f"Posted {posted} story(ies)." if posted
                  else "No new stories - stayed silent.")
            return 0

        if args.command == "audit":
            run_audit(dry_run=args.dry_run, with_news=not args.no_news)
            return 0

        if args.command == "listen":
            run_listener(args.seconds)
            return 0

    except config.ConfigError as exc:
        log.error("%s", exc)
        return 2
    except WindowParseError as exc:
        log.error("%s", exc)
        return 2
    except Exception as exc:
        log.error("Run failed: %s", exc)
        log.debug("%s", traceback.format_exc())
        # Best-effort alert, only if Telegram itself is configured.
        try:
            TelegramNotifier().send_error(f"Scheduled {args.command}", exc)
        except Exception:
            pass
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

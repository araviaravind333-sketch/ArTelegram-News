# Aravind News 24 — Automation

Daily news scraping, virality analysis and Instagram insights reporting. Runs
entirely on free tiers: GitHub Actions for compute, free RSS for news, the
Telegram Bot API for delivery, and the Instagram Graph API for insights. No
paid news API is used anywhere.

---

## What it does

**Workflow 1 — News pulse (every hour)** — the main feed
Aggregates 34 free RSS feeds, deduplicates across sources, scores each story
1-100 for virality, and posts **only what is new since the last run** as a
compact Telegram message: hook, core facts, score, drivers, CTA and a tappable
source link. Nothing is ever posted twice. When a window is quiet the bot
stays silent rather than sending a "no news" notice.

**Workflow 1b — Full briefing (`.docx`, daily at 07:00 IST or on demand)**
The long-form counterpart: a styled Word document covering 8 categories
(India, World, Business, Sports, Technology, Health, Unreported, Current
Affairs) plus a top "High-Virality Instagram Picks" summary — 9 tables in
total, **at least 7 stories per section**. Every table has exactly 4 columns:

| # | News Headline | News Link | Score | Fits Your Instagram? |
|---|---|---|---|---|
| 1 | (plain original headline) | publisher + clickable link | 1-100 | ✅/➡/❌ + a one-line reason |

The 4th column is the one that reads *this* account's history: it pulls the
per-category performance multiplier from `data/benchmarks.json` (written by
the daily Instagram report) and says outright whether that category has helped or
hurt you before, or — with fewer than 5 reels tracked so far — falls back
honestly to "no reel history yet" plus the story's own virality score, rather
than pretending to know something it doesn't. Supports the same custom time
windows as `/scan` (last 24h, a specific range, etc.).

**Workflow 2 — Daily Instagram report (10:00 AM IST)**
Reads the account's latest 50 posts of **every format** (reels, images,
carousels) from the Instagram Graph API and sends a plain-language report to
Telegram: your account size, the top posts by views, average views per format,
what got no reaction, and three reels to post today built from the morning's
top stories. Every figure is a raw Instagram Insights value (views, accounts
reached, likes, comments, shares, saves), so it can be checked against the
Insights screen in the app. Small samples are labelled as small: with a
handful of reels the report says "a hint, not proof" rather than declaring
winners and flops, and it never prints a rate (like "per 1,000 views") when
the counts are too small to mean anything.

The two workflows feed each other: the audit writes `data/benchmarks.json`,
and the virality engine reads it back as a per-category multiplier, so topics
that over-perform on this account get promoted in tomorrow's picks.

---

## Setup

### 1. Secrets

Add these under **Settings → Secrets and variables → Actions**:

| Secret | Required for | How to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | everything | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | everything | message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `INSTA_ACCESS_TOKEN` | the audit | Meta Graph API Explorer, scopes `instagram_basic` + `instagram_manage_insights` |
| `INSTA_USER_ID` | the audit | `GET /me/accounts` → `instagram_business_account.id` |
| `ANTHROPIC_API_KEY` | optional | [console.anthropic.com](https://console.anthropic.com) — polishes hooks and CTAs; everything works without it |
| `YOUTUBE_API_KEY` | optional | [console.cloud.google.com](https://console.cloud.google.com/apis/credentials) → enable "YouTube Data API v3" → create an API key; adds a "trending on YouTube right now" signal to Top Picks ranking. Free, no OAuth. |

Nothing is ever hardcoded. Every value is read through `os.getenv()` in
`config.py`, and `.env` is git-ignored.

> **Instagram tokens expire.** A long-lived user token lasts 60 days. Refresh
> it before expiry or the daily audit will start failing with a Graph API
> error, which the bot will report to Telegram.

### 2. Local run

```bash
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill it in
python main.py check          # verifies configuration, prints no secrets
python main.py pulse --dry-run          # see what the 20-min post looks like
```

---

## Usage

### CLI

```bash
python main.py pulse                            # post only what is new
python main.py pulse --dry-run                  # print it, touch nothing
python main.py pulse --limit 3                  # fewer stories in the post
python main.py pulse --reset-seen               # clear memory, re-post recent
python main.py scan                             # full .docx, last 24 hours
python main.py scan --minutes 25                # .docx for a custom window
python main.py scan --hours 6
python main.py scan --window "17-09-26 06:00 to now"
python main.py scan --dry-run                   # build the .docx, send nothing
python main.py audit                            # daily Instagram report
python main.py audit --dry-run                  # print the plan instead
python main.py listen                           # poll Telegram for commands
python main.py check                            # validate configuration
```

### Telegram commands

| Command | Effect |
|---|---|
| `/pulse` | post whatever is new since the last check (runs automatically every hour) |
| `/scan` | full `.docx` briefing for the last 24 hours |
| `/scan 25m` | `.docx` for a short custom window |
| `/scan last 6 hours` | relative window |
| `/scan 17-09-26 06:00 to now` | explicit window |
| `/scan 17-09-26 06:00 to 18-09-26 09:30` | explicit range |
| `/audit` | run the daily Instagram report now |
| `/ping`, `/help` | liveness and command reference |

Dates are **day-first** (`17-09-26` = 17 September 2026) and all times are
IST. `DD/MM/YY`, `YYYY-MM-DD`, `HH:MM` and `6:00 AM` are also accepted.

---

## Workflows

| File | Schedule (UTC cron) | Purpose |
|---|---|---|
| `.github/workflows/news_pulse.yml` | `5 * * * *` | **the hourly news feed** |
| `.github/workflows/daily_audit.yml` | `30 4 * * *` → 10:00 AM IST | daily Instagram report, delivered well before 10:30 |
| `.github/workflows/news_scan.yml` | `30 1 * * *` → 07:00 AM IST | full `.docx` briefing |
| `.github/workflows/telegram_listener.yml` | `*/5 * * * *` | on-demand `/pulse`, `/scan`, `/audit` |

GitHub's scheduler has 5-minute granularity and is best-effort under load,
which is why the audit fires at 10:00 rather than 10:25 — it needs slack to
still land inside the 10:30 window.

**Scheduler lag does not cause gaps or repeats in the pulse.** If a run fires
late, or is skipped entirely, the next one still catches everything: the
seen-store decides what is new, and the 90-minute lookback covers the gap.

**Free-tier note:** Actions minutes are unlimited on public repositories. On a
private repo the free allowance is 2,000 minutes/month; at an hourly cadence
(24 runs a day, ~1 minute each, ~720 minutes/month) the pulse comfortably fits
alongside the daily audit and briefing, but the 5-minute listener on top of it
would still exceed the allowance. **Make the repo public**, or widen the
listener cron / drop it and drive `/scan` and `/audit` manually via
`workflow_dispatch`.

---

## How the pulse never repeats a story

Each run is a fresh process, so "new since last time" needs memory. That is
`data/seen.json`, a ledger keyed two ways per story — canonical URL *and*
normalised title fingerprint — so a story resurfacing under a syndicated copy,
an updated permalink or a Google News redirect is still recognised.

The flow each run:

1. Scan the last 90 minutes (longer than the 1-hour interval, to absorb
   both RSS publication lag and scheduler lag).
2. Drop anything already in the ledger.
3. Score what remains; hold back anything under `PULSE_MIN_SCORE`.
4. Post the top `PULSE_MAX_ITEMS` (default 10).
5. **Write the ledger only after Telegram confirms delivery**, so a Telegram
   outage retries those stories next run instead of losing them.

Two deliberate details:

- **Strong stories that overflow the per-post cap are not marked as seen.**
  They roll into the next pulse and compete again, rather than being
  suppressed forever. Recency decay lowers their score each cycle, so they
  either get posted soon or fall out of the 90-minute window on their own.
- **Entries expire after `SEEN_TTL_HOURS` (48h)**, so a genuinely developing
  story can resurface with a fresh angle instead of being blocked permanently.

Verified behaviour: five consecutive runs posted 30 stories with zero repeats;
clearing the ledger re-posts the current window.

---

## Architecture

```
config.py                      env loading, validation, secret masking, tunables
scrapers/rss_collector.py      34 feeds -> fetch -> dedupe -> time-window filter
scrapers/seen_store.py         cross-run memory so the pulse never repeats a story
analyzer/virality_engine.py    classification, 1-100 scoring, hooks, CTAs, Instagram-fit
generators/doc_generator.py    styled landscape .docx with per-category tables
generators/pulse_formatter.py  compact Telegram digest for the hourly pulse
services/instagram_auditor.py  Graph API insights, plain-language report, reel plan
services/telegram_notifier.py  sendMessage / sendDocument / getUpdates
main.py                        window parsing, command dispatch, CLI
data/benchmarks.json           rolling reel history (committed by CI each day)
data/seen.json                 posted-story ledger (git-ignored; cached in CI)
```

### How the virality score is built

| Component | Max | Signal |
|---|---|---|
| Recency | 30 | decays over 48h |
| Corroboration | 18 | how many independent feeds carried the story |
| Authority | 12 | wire service / government primary source |
| Trigger load | 22 | share + save + debate language density |
| Curiosity gap | 10 | question and reveal framing in the headline |
| Specificity | 8 | concrete numbers in the headline |
| YouTube trend | 8 | headline overlaps a currently-trending YouTube video title (needs `YOUTUBE_API_KEY`; 0 otherwise) |

Then `× historical multiplier` (0.80-1.20, from this account's own reel
performance per category) plus a small driver bonus, clamped to 1-100.

### How Top Picks are chosen

Beyond the score above, the "High-Virality Instagram Picks" section (**always
at least 10 stories**) ranks candidates by, in order: (1) this account's own
Instagram-fit verdict — a story its reel history backs beats an equally-scored
one it doesn't, (2) whether it's independently trending on YouTube right now,
(3) the virality score itself, (4) corroboration (how many independent
outlets are covering it — the free-tier proxy used here in place of Twitter/X,
whose free API tier cannot read trends or run a search at all, so there is no
free way to build an equivalent signal for it).

Every story is placed in **exactly one section** — Top Picks and the category
tables never repeat the same story, and a story cross-filed into a thin
category is removed from wherever else it might have been considered.

### The Instagram-fit column

Every row's 4th column answers one question: *should this account post this
category today?*

- With **5+ reels tracked** in `data/benchmarks.json`, the verdict comes from
  that category's real performance multiplier (±5% is "close to average" /
  MAYBE; beyond that it's a clear YES or NO), e.g. *"India News reels
  outperform your account average by 18%."*
- With **fewer than 5 reels tracked**, there's no reliable per-category
  signal yet, so the column says so plainly and falls back to the story's own
  virality score instead of guessing.

### Editorial guarantees

- **Hooks reframe, they never invent.** Core facts are derived only from the
  headline and the publisher's own summary text. (The 4-column table shows
  the plain original headline, not the reel hook — the hook, CTA and driver
  tags are still computed internally and used by the hourly Telegram
  pulse.)
- **CTAs must be payable.** A "save before the deadline" CTA is only used when
  the story actually contains deadline language; the guard list is deliberately
  narrow.
- **Google News link-soup is discarded.** Google News ships its `<description>`
  as a list of anchors with no added prose. It is dropped rather than passed
  off as reporting, and deduplication keeps the real summary from a direct
  publisher feed instead.
- **Undated entries are dropped**, because they cannot be honestly filtered
  into a time window.
- Every row carries its source link. **Verify before publishing.**

---

## Known constraints

- **Reuters retired its public RSS.** `reutersagency.com/feed` returns 404 and
  `reuters.com/rssfeed` returns 401. Reuters copy is therefore pulled through
  the free Google News `site:reuters.com` operator, which still indexes it.
  Associated Press is included the same way.
- **A browser User-Agent is required, not cosmetic.** PIB India returns 403 to
  an obvious bot string while serving the same public RSS to a browser UA.
- **`python-telegram-bot` is deliberately not used.** v20+ is async-only and
  pulls an event loop into every CI step, while this project needs exactly
  three endpoints. Synchronous `requests` keeps failures explicit and CI logs
  readable.
- **Meta retires insight metrics without notice.** The auditor requests a
  candidate metric list and automatically retries without any metric the API
  rejects, so a Graph API version bump degrades the audit rather than breaking
  it.
- **Thin windows widen automatically.** A 25-minute scan at a quiet hour
  yields very few stories, so the lookback widens to 3h → 12h → 24h → 48h
  until there is enough supply. The document always states the window that was
  actually used.
- **Category top-up cross-files stories.** If a category is naturally thin, it
  is filled from the highest-scoring unused stories, marked `(cross-filed)` in
  the scoring footnote. Facts and links are never altered — only the section.

# NDT Tracker — system memory

Why the code is shaped the way it is. **Read the ⚠️ blocks before changing the code
they sit beside** — each marks a bug that shipped and looked fine from outside.

Ops: `DEPLOYMENT.md`. Releases: `CHANGELOG.md`. Overview: `PROJECT_CONTEXT.md`.

## Architecture

- **Dual client** — `telethon` (user API) silently monitors channels;
  `python-telegram-bot` (bot API) handles commands and alerts.
- **Storage** — `database.py` (API) over `storage.py` (SQLite or Postgres backend).
- **Headless** — `SESSION_STRING` lets Telethon log in with no terminal.

Pipeline: message → match keywords → resolve product name → extract price →
deduplicate → alert.

## Commands

`/watch` `/unwatch` `/updatesynonyms` `/watchlist` — keywords.
`/channels` `/searchchannel` `/addchannel` `/untrackchannel` — sources.
`/deals` `/stats` `/pause` `/resume` — status.
`/testmatch <text>` `/synonyms <keyword>` — **debug matching**; reach for these
first, since a wrong match is indistinguishable from a right one from outside.

## 1. Keyword matching (`keyword_matcher.py`)

Word-boundary regex, not substring (`pan` ≠ `pant`). URLs are stripped before
matching, so `tv` can't match inside `amzn.to/u5tv2`. Tiers: `exact` > `synonym` >
`plural` > `fuzzy`; longest term wins within a tier.

> ⚠️ **Fuzzy matching is OFF by default. Don't turn it on casually.**
> `difflib.SequenceMatcher` scores two 5-letter words sharing 4 characters at
> exactly **0.800**. The threshold was 0.75, so a watchlist of `shoes` matched
> `homes`, `hoses` and `shows` — home products fired shoes alerts. No threshold
> fixes this: loose enough for a real typo on a short word is loose enough for
> every unrelated word one edit away.
> Its only genuine use was plural tolerance, now done exactly by `singularise()` —
> safe because normalisation maps each word to one form, keeping
> shoes/homes/hoses/shows distinct. `FUZZY_MATCH_ENABLED=true` re-enables it behind
> gates (min length 7, threshold 0.88, equal word count).

> ⚠️ **Synonym expansion is not transitive.**
> `air cooler` is listed under both `ac` and `cooler`. The reverse lookup used to
> pull in every owning category, so `/watch "air cooler"` expanded to 11 terms
> including `split ac` — watching a cooler alerted on air conditioners. A term
> claimed by 2+ categories now matches only itself.

`get_synonyms()` returns a **sorted** list. It once returned `list(set(...))`, and
Python randomises string hashing per process, so `matched_term` changed per restart.

## 2. Product name & scraping (`channel_monitor.py`, `link_scraper.py`)

Order matters — Telegram's servers already defeated Amazon/Flipkart bot protection
to build their previews, so we reuse that rather than scraping:

1. **Native preview** on the message (free).
2. **`GetWebPagePreviewRequest`** — ask Telegram to render one.
3. **HTML scraper** — last resort; `ENABLE_HTML_SCRAPER=false` disables it.

A preview title becomes the product name, which also stabilises the dedup hash.
Alerts show which stage won (`PREVIEW_NATIVE` / `PREVIEW_API` / `LINK_SCRAPE` /
`TEXT_PARSE`).

> ⚠️ **The `WebPagePending` trap.**
> Telegram answers `WebPagePending` the first time it sees any URL, then fetches in
> the background — you must sleep and re-ask. This is where the bot was silently
> broken: the retry branch called `asyncio.sleep()` but the module never imported
> `asyncio`, so it raised `NameError`, a bare `except` swallowed it into a
> `logger.debug` invisible at INFO, and **every message fell through to the
> scraper**. Preview extraction looked implemented but never once ran.
> Also note `_unwrap_media`: older Telegram layers return `MessageMedia` directly,
> newer ones wrap it in `messages.WebPagePreview` with `.media`. Both are handled so
> a layer bump can't break previews again.

## 3. Channel discovery (`bot.py`)

`/channels` shows only joined channels whose title or @username contains a
`CHANNEL_NAME_FILTERS` term (`deal`, `sale` — substrings, so `deals`/`sales` are
covered). A real account has joined hundreds of channels and the unfiltered list was
pages of news and memes. `/searchchannel <text>` ignores the filter; `/addchannel`
never consulted it.

> ⚠️ **Tracked channels are listed even when the name does not match.** Dropping that
> exception makes a channel added via `/addchannel` — which usually won't contain
> "deal" — permanently untoggleable, because the filter hides the only button that
> could turn it off. `_visible_channels()` is the one place this rule lives.

The active search query is parked in `context.user_data`, not `callback_data`:
Telegram caps callback payloads at 64 bytes and the query is arbitrary user text.
Toggling and paging re-render whatever view the button was pressed in.

## 4. Price, dedup, alerts

Prices use `[\d,]+` so `₹1,000` isn't truncated, and catch a bare number beside a
link (`2950 : https://...`). Dedup hashes **cleaned product name + price + keyword**,
not raw text, so the same deal reposted with a new affiliate link is dropped.
Alerts are HTML (not MarkdownV2) with `html.escape()`, so odd characters can't
break delivery.

## 5. Persistence

`storage.py` picks a backend from `DATABASE_URL`: Postgres when set, SQLite
otherwise. `database.py`'s API is identical either way. Postgres exists because a
host without a disk wipes the container filesystem on every restart, losing the
watchlist.

Rules when touching SQL — it is written once in `?` style and translated to `$1`:

- Use `ON CONFLICT ... DO NOTHING / DO UPDATE SET x = excluded.x`. Works in both;
  `try/except IntegrityError` needs a different exception class per driver.
- **Alias aggregates** (`COUNT(*) AS cnt`) — the implicit name differs
  (`COUNT(*)` vs `count`) and unaliased code breaks on one engine.
- `channels.channel_id` is **BIGINT** on Postgres, not optional: Telegram IDs
  exceed 32 bits and Postgres `INTEGER` really is 32-bit, unlike SQLite's.
- `PRAGMA` is SQLite-only — it lives behind `backend.maintenance()`.

> ⚠️ **Serverless Postgres (Neon) suspends when idle.** Without accommodating that,
> the bot works and then quietly stops recording deals. `DB_POOL_MIN_SIZE=0` holds
> nothing open (so it can sleep, and doesn't burn free-tier compute), idle
> connections recycle after 60s, and queries retry on connection-level failures —
> a waking database returns `CannotConnectNowError`. Query errors are never retried.

Tools: `check_db.py` (verify a database before deploying), `migrate_to_postgres.py`
(copy an existing SQLite file across; idempotent, deletes nothing).

## 6. Memory management

Long-lived process on a small container — anything per-message compounds.

- **One connection, not one per call.** Every `database.py` method used to open its
  own `aiosqlite.connect()` — each spawns a thread — and `get_all_keywords()` runs
  on *every* message. Now a singleton over one connection / small pool.
- **Watchlist cached** in memory (`WATCHLIST_CACHE_TTL`, 30s); edits call
  `invalidate_watchlist_cache()`.
- **Caches are bounded.** The scrape cache only evicted *expired* entries past its
  cap, so fresh URLs grew it forever. All caches are now `OrderedDict` LRUs.
- **Deals are pruned.** `cleanup_old_deals()` existed from day one but nothing ever
  called it, so `matched_deals` grew for the life of the deployment. Now on a timer
  plus at boot.
- One shared `aiohttp.ClientSession` (was a new TLS context per URL), page reads
  capped via streaming, `soup.decompose()` to break BeautifulSoup's cycles.

## 7. Startup

> ⚠️ **`main()`'s `asyncio.set_event_loop()` block looks like dead code and is not.**
> python-telegram-bot 21.x calls `asyncio.get_event_loop()` inside `run_polling()`.
> Through Python 3.13 that created a loop implicitly; 3.14 raises. The process then
> dies before `post_init`, so the port is never bound and the deploy fails.
> Pinned by `test_pipeline.py::test_main_installs_an_event_loop`.

> ⚠️ **Bind the port before anything slow.** `post_init` starts the web server
> first, *then* logs into Telethon. Reversed, Telethon's login outran Render's port
> scan and the service was reaped. Directory creation is likewise non-fatal — an
> unguarded `os.makedirs` on a stale path crashed the app at import.

## Tests

`python test_pipeline.py` — offline, no credentials or network. 116 assertions
covering the `WebPagePending` retry, both preview API shapes, the lookalike-word
false positives, category leakage, cache bounds, Postgres retry/translation, and
the event-loop shim.

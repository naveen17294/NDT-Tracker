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

`/menu` — the button hub. `/watch` `/unwatch` `/updatesynonyms` `/exclude`
`/watchlist` — keywords. `/channels` `/searchchannel` `/addchannel`
`/untrackchannel` `/channelreport` — sources. `/deals` `/stats` `/pause` `/resume`
`/keyboard` `/cancel` — control.

`/testmatch <text>` `/synonyms <keyword>` — **debug matching**; reach for these
first, since a wrong match is indistinguishable from a right one from outside.

## Interface rules (`bot.py`)

> ⚠️ **A bare command must ASK, never error.** Telegram's command menu sends
> `/watch` with no argument and there is no way to make the client pre-fill it, so
> answering with "❌ specify a keyword" makes the entire menu useless. Every command
> needing input calls `_prompt()`, which sends a `ForceReply` and parks the action in
> `context.user_data`; `text_input_handler` feeds the next plain message to the
> matching `_APPLIERS` entry. Commands still accept inline arguments unchanged.

Where the argument is "one of your existing keywords" (`/unwatch`, `/synonyms`,
`/exclude`), `_pick_keyword()` offers the watchlist as buttons instead of asking.
Anything that won't fit `callback_data`'s 64 bytes falls back to being typed —
`_fits_callback()` guards every generated button.

`text_input_handler` is registered **last** and always replies to something. Silence
after a tap reads as a broken bot.

> ⚠️ **Everything the bot sends is HTML with `html.escape()`. Never Markdown.**
> Keywords (`iphone_15`), channel titles (`LOOT_DEALS_INDIA`) and product names are
> arbitrary text, and under legacy Markdown an odd number of `_` or `*` is a hard 400
> — the screen fails to appear rather than looking wrong. Views return
> `(text, markup)` and go out through `_reply_view` / `_edit_view`, which hardcode
> the parse mode; picking one per call site is exactly how `menu_stats` and
> `menu_help` ended up claiming Markdown for HTML text. Note `<` and `>` need
> escaping even inside `<code>` — literal `<message>` in the help text was read as a
> tag and broke `/help`. `test_static_text_is_valid_telegram_html` pins both.

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

**Negative keywords.** A watchlist row carries `exclusions`; any of those terms in the
message vetoes that keyword before any synonym is tried. Scoped to the one keyword on
purpose — `/watch shoes -kids` must not stop `laptop` matching the same message. They
use the same word-boundary + singularise machinery as a positive match (so `-kids`
blocks "kid", and `-pen` cannot veto "expensive"), and are never synonym-expanded:
naming a term to block means that term, not its category. `/testmatch` prints
`BLOCKED keyword: …` when a veto fired, because a suppressed alert is otherwise
indistinguishable from one that never matched.

`match()` unpacks watchlist entries as `entry[0], entry[1], entry[2] if len > 2` —
2-tuples still work, which is what lets old call sites and tests keep passing.

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

## 5. Feedback & the channel report

> ⚠️ **NDT keeps no history of alerts. Do not add one.** `matched_deals` is a dedup
> ledger pruned after `DEAL_RETENTION_DAYS`, so nothing per-alert survives a week —
> and nothing should. Anything that must outlive that is a **counter**, not a row.

`channel_stats` is one row per channel, forever, no matter how many alerts pass
through: `alerts`, `up`, `down`, plus `*_at_report` snapshots. The 8-hourly report
subtracts the snapshot to get "since last time" and then re-snapshots, which is how
it shows a delta without storing anything per alert.

Every alert carries 👍/👎 with the **channel id in the button's `callback_data`**.
That is the mechanism, not a shortcut: the vote is attributable when pressed, so no
record of the alert has to exist. On a vote the keyboard is replaced with a static
receipt — the message's own markup is the only thing preventing a double vote, again
because there is no row to check against.

`record_alert()` is called only when the notifier reports the alert actually went
out. Counting before that inflates a channel's total with in-window duplicates you
never saw.

Ordering that matters: `_send_channel_report()` snapshots **after** a successful
send, so a failed send leaves the window intact for the next report instead of
silently eating it.

> ⚠️ **The report schedule is measured from `last_report_at` in the database, not
> from process start.** A plain `sleep(8h)` loop is reset by every restart, and this
> runs on a free tier that recycles containers — the timer would never reach 8 hours.
> The same property makes a failed send retry (the timestamp did not move) instead of
> skipping the window.

## 6. Persistence

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
- **A new column on an existing table needs a migration, not just a schema edit.**
  `CREATE TABLE IF NOT EXISTS` does nothing for a table that already exists, so a
  deployed database never gets the column. Add it to `_COLUMN_MIGRATIONS` in
  `database.py`; `backend.add_column_if_missing()` handles the split (Postgres has
  `ADD COLUMN IF NOT EXISTS`, SQLite has to read `PRAGMA table_info` first, because
  re-running a bare `ALTER TABLE` errors and would take the whole boot with it).
  Migrations are logged but never fatal — `get_all_keywords()` falls back to
  selecting without `exclusions` rather than throwing on every incoming message.

> ⚠️ **Serverless Postgres (Neon) suspends when idle.** Without accommodating that,
> the bot works and then quietly stops recording deals. `DB_POOL_MIN_SIZE=0` holds
> nothing open (so it can sleep, and doesn't burn free-tier compute), idle
> connections recycle after 60s, and queries retry on connection-level failures —
> a waking database returns `CannotConnectNowError`. Query errors are never retried.

Tools: `check_db.py` (verify a database before deploying), `migrate_to_postgres.py`
(copy an existing SQLite file across; idempotent, deletes nothing).

## 7. Memory management

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

## 8. Startup

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

`python test_pipeline.py` — offline, no credentials or network. 231 assertions
covering the `WebPagePending` retry, both preview API shapes, the lookalike-word
false positives, category leakage, negative-keyword vetoes, channel counters
surviving a deal prune, cache bounds, Postgres retry/translation, and the
event-loop shim.

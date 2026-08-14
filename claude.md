# NDT Tracker - Memory & Feature Usage

This file serves as the system memory for the **NDT Tracker** project. It outlines the core architecture, advanced features, and edge-case handling mechanisms developed to make the bot robust and reliable.

## 🏗️ Architecture
- **Dual-Client System:** Uses `telethon` (User API) to silently monitor other channels in the background, and `python-telegram-bot` (Bot API) to handle the UI and commands.
- **Database:** `aiosqlite` is used for asynchronous database operations (storing deals, channels, and keywords).
- **Deployment-Ready:** Supports `SESSION_STRING` for Telethon, allowing the bot to be deployed headlessly (e.g., to Railway) without requiring terminal login codes.

## 🚀 Bot Commands
- `/watchlist` — View and manage tracked products.
- `/channels` — View monitored channels and toggle them 🟢/🔴. Includes an "Untrack All" button.
- `/addchannel <link>` — Join and track a new channel.
- `/untrackchannel <name>` — Instantly untrack a specific channel without opening the menu.
- `/deals` — View recent dropped deals from the database.
- `/stats` — View system diagnostics.
- `/pause` / `/resume` — Suspend or resume scanning.
- `/updatesynonyms <keyword> <syn1>,<syn2>` — Add custom synonyms dynamically.

## 🧠 Advanced Logic & Edge Cases

### 1. Keyword Matching (`keyword_matcher.py`)
- **Word Boundaries:** Uses strict regex word boundaries (`\bword\b`) instead of substring matching. This prevents false positives (e.g., `pan` won't match `pant`, `tv` won't match `smartv`).
- **URL Stripping:** Removes URLs from the message text *before* keyword matching. This prevents accidental matches inside URL slugs (e.g., matching `tv` inside `amzn.to/u5tv2`).
- **Synonym Matrix:** Includes a massive built-in dictionary mapping core keywords to industry terms (e.g., `tv` -> `oled`, `qled`, `smart tv`).
- **Match tiers:** `exact` (the watchlist keyword itself) > `synonym` > `plural` > `fuzzy`. Within a tier the longer, more specific term wins, so `refrigerator` is reported over `double door`. An exact hit on the keyword short-circuits the scan.

> **Fuzzy matching is OFF by default. Read this before turning it on.**
> `difflib.SequenceMatcher` scores two 5-letter words sharing 4 characters at exactly
> **0.800**. The threshold was 0.75, so a watchlist of `shoes` matched `homes`,
> `hoses` and `shows` — home-furnishing deals fired shoes alerts. This is not a
> tuning problem: any threshold loose enough to catch a real typo on a short word
> also catches every unrelated word one edit away from it.
>
> The only thing fuzzy matching genuinely bought us was plural tolerance (`shoe` /
> `shoes`), and that is now done exactly by `singularise()`. Normalisation is safe
> where similarity is not, because it maps each word to exactly one form —
> `shoes→shoe`, `homes→home`, `hoses→hose`, `shows→show` all stay distinct, while
> difflib scored every one of those pairs at 0.800.
>
> `FUZZY_MATCH_ENABLED=true` re-enables it behind gates (min length 7, threshold
> 0.88, max length difference 2, n-grams compared only at equal word count). Expect
> noise regardless.

> **Synonym expansion is not transitive — don't make it so.**
> A term can appear in two categories: `air cooler` is listed under both `ac` and
> `cooler`. The reverse lookup used to pull in *every* owning category wholesale, so
> `/watch "air cooler"` silently expanded to 11 terms including `split ac` and
> `window ac` — watching a cooler alerted you about air conditioners. A term claimed
> by more than one category now expands to nothing and matches only itself.

`get_synonyms()` returns a **sorted** list. It previously returned `list(set(...))`,
and Python randomises string hashing per process, so the reported `matched_term`
changed between restarts.

**Debugging matches:** `/testmatch <text>` dry-runs any message against the live
watchlist and reports whether it would alert and why; `/synonyms <keyword>` prints
every term a keyword expands to. Reach for these first — this class of bug stayed
invisible for so long because a wrong match looks exactly like a right one from the
outside.

### 2. Product Name Extraction & Scraping (`channel_monitor.py` & `link_scraper.py`)
- **Primary Method (Native Previews):** The bot intercepts Telegram's native `MessageMediaWebPage` (link previews). Since Telegram's servers already bypassed Amazon's anti-bot protection to generate the preview, the bot instantly extracts the `title` and `description` from the payload without doing any HTTP requests. When a native preview exists, its title becomes the product name (not the first 100 chars of the marketing blast), which also makes the dedup hash far more stable.
- **Fallback 1 (Hidden API):** If the channel admin disabled link previews, the bot sends a hidden `GetWebPagePreviewRequest` to Telegram's servers, forcing Telegram to generate the preview metadata for us on the fly.
- **Fallback 2 (Python Scraper):** If all else fails, it falls back to a BeautifulSoup web scraper (which may occasionally be blocked by Amazon CAPTCHAs, hence being the last resort). Set `ENABLE_HTML_SCRAPER=false` to switch it off entirely and run preview-only.

> **The `WebPagePending` trap — read before touching `_resolve_preview`.**
> The first time Telegram is asked about a URL it has not seen, it replies
> `WebPagePending` and fetches the page in the background. You *must* sleep and ask
> again; a single request answers pending for nearly every fresh affiliate link.
> This is where the bot was silently broken: the retry branch called
> `asyncio.sleep()` but `channel_monitor.py` never imported `asyncio`, so it raised
> `NameError`, a bare `except` swallowed it into a `logger.debug` invisible at INFO
> level, and **every message fell through to the HTML scraper**. Preview extraction
> looked implemented but never once ran. `test_pipeline.py` now pins this behaviour.
>
> Also note `_unwrap_media`: older Telegram layers return a `MessageMedia` directly,
> newer ones wrap it in a `messages.WebPagePreview` carrying `.media`. Both are
> handled so a Telethon/layer bump can't silently break previews again.

Each alert footer shows which stage found the product — `PREVIEW_NATIVE`,
`PREVIEW_API`, `LINK_SCRAPE` or `TEXT_PARSE` — so you can tell at a glance whether
previews are doing the work or the scraper is carrying it.

### 3. Price Extraction (`price_extractor.py`)
- **Comma Agnostic Regex:** The extraction formulas correctly capture numbers with or without commas (`[\d,]+`) ensuring `₹1000` isn't truncated to `100`.
- **Raw Number Fallback:** It detects raw numbers positioned immediately next to a link (e.g., `2950 : https://amzn.to/...`), even if the channel forgot to include a currency symbol like `₹`.

### 4. Deal Deduplication
- **Robust Hashing:** Instead of hashing the raw message text (which changes if the channel adds an emoji or slightly alters the URL), the bot hashes the **Cleaned Product Name + Price + Keyword**.
- If a channel posts the exact same dinner set three times with three different affiliate links, the bot recognizes the product name and price are identical, and silently drops the duplicates.

### 5. Alert Formatting (`notifier.py`)
- **HTML Engine:** Uses `HTML` parsing instead of `MarkdownV2` to format messages. Product names are wrapped in `html.escape()` to guarantee that weird characters (like `[` or `*`) never crash the Telegram delivery API.
- **Original Transmission:** Appends the exact raw message text sent by the channel into a sleek `<blockquote>` at the bottom of the alert, so the user can see exact coupon codes and context.

## 💾 Persistence — where the data actually lives

Storage is behind a two-backend abstraction in `storage.py`, selected at runtime by
whether `DATABASE_URL` is set:

| | Backend | Survives a restart? |
|---|---|---|
| `DATABASE_URL` unset | SQLite file at `DB_PATH` | Only if that path is a mounted disk |
| `DATABASE_URL` set | Postgres via asyncpg | Yes — the data is off the container |

`database.py`'s public API is identical either way, so nothing else in the codebase
knows which is in use.

**Why this exists.** On a host with no mounted disk — Render's free tier, where disks
are a paid feature — the container filesystem is wiped on every restart and redeploy.
A SQLite file there loses the watchlist, tracked channels and dedup history every
time, so the bot comes back knowing nothing. An external database is the only fix
available on that plan.

**Rules for touching the SQL.** It is written once, in SQLite's `?` placeholder style,
and `storage.to_pg_placeholders()` rewrites it to `$1, $2` for Postgres. Keep every
statement to syntax both engines accept:

- `ON CONFLICT (col) DO NOTHING / DO UPDATE SET x = excluded.x` works in both (SQLite
  has supported it since 3.24). Prefer it over `try/except IntegrityError`, which
  needs a different exception class per driver.
- Alias aggregates — `SELECT COUNT(*) AS cnt`. The implicit column name differs
  (`COUNT(*)` in SQLite, `count` in Postgres) and unaliased code breaks on one engine.
- `channels.channel_id` is `BIGINT` on Postgres and this is not optional. Telegram
  channel IDs routinely exceed the 32-bit range, and Postgres `INTEGER` really is
  32-bit — unlike SQLite's, which is 64-bit. Getting it wrong fails only for large
  IDs, which is the worst kind of bug to ship.
- `PRAGMA` is SQLite-only, so it lives behind `backend.maintenance()` (a no-op on
  Postgres, where autovacuum handles reclamation).

**Serverless Postgres needs two accommodations.** Neon and Supabase suspend an idle
database, and without these the bot appears to work and then quietly stops recording
deals after a quiet period:

- The provider **drops connections** when it suspends. Holding one open both fights
  that and burns the free tier's compute budget, so `DB_POOL_MIN_SIZE` is `0` and idle
  connections are recycled after 60s — inside Neon's ~5 minute suspend window.
- A suspended database **refuses connections while it wakes**. Every query retries on
  connection-level failures (`CannotConnectNowError`, `ConnectionDoesNotExistError`,
  `InterfaceError`, socket errors), discarding the pool first so the retry dials
  fresh. Query errors — bad SQL, constraint violations — are never retried.

**Tools.** `python check_db.py` connects, creates the schema, round-trips a probe row
and prints the current contents — run it after setting `DATABASE_URL` and before
deploying. `python migrate_to_postgres.py` copies an existing SQLite file across; it
is idempotent, inserts with `ON CONFLICT DO NOTHING`, and never deletes anything.

## 🧹 Memory Management

This is a long-lived process on a small container, so anything per-message compounds.
Four things were fixed, and the pattern behind them is worth keeping in mind:

1. **One SQLite connection, not one per call.** `database.py` used to open a fresh
   `aiosqlite.connect()` inside *every* method. Each spawns a dedicated OS thread and
   page cache, and `get_all_keywords()` ran on every incoming message. `Database` is
   now a singleton holding one WAL-mode connection behind an `asyncio.Lock`.
2. **The watchlist is cached in memory** (`WATCHLIST_CACHE_TTL`, default 30s) instead
   of being re-read from disk per message. Any `/watch`, `/unwatch` or
   `/updatesynonyms` calls `invalidate_watchlist_cache()`.
3. **Caches are genuinely bounded.** The scraper's cache only evicted *expired*
   entries once it passed 500, so a stream of fresh URLs grew it forever. All caches
   (preview, scrape, notifier dedup) are now `OrderedDict` LRUs with hard caps.
4. **Deals are pruned.** `cleanup_old_deals()` existed since day one but nothing ever
   called it, so `matched_deals` grew for the entire life of the deployment. A
   maintenance loop runs it every `MAINTENANCE_INTERVAL_HOURS` plus once at boot.

Also: one shared `aiohttp.ClientSession` (a new one per URL meant a new TLS context
per URL), page reads capped at `SCRAPE_MAX_BYTES` via streaming, and
`soup.decompose()` to break BeautifulSoup's reference cycles immediately.

## 🚀 Deployment (Render)

**Bind the port before doing anything slow.** `post_init` starts the aiohttp server
*first*, then logs into Telethon. The reverse order is what killed the old
deployment: Telethon login plus a full dialog sync outran Render's port-detection
window, so Render decided the service never bound a port and reaped it. The server
now answers in ~30ms.

- `/health` returns real state — `telethon_connected`, `monitoring`, `channels`,
  `uptime_seconds` — so a green health check means the uplink is actually up. Use it
  as Render's `healthCheckPath`.
- **Free web services are spun down after ~15 min without an inbound request.**
  Binding the port is not enough; traffic must arrive. The bot self-pings
  `RENDER_EXTERNAL_URL/health` every `KEEPALIVE_INTERVAL` seconds.
- **The container filesystem is ephemeral.** Without a mounted disk, `data/ndt.db`
  and the Telethon session are wiped on every deploy and restart — the bot comes back
  with an empty watchlist. Mount a disk and point `DATA_PATH` / `SESSION_PATH` at it
  (see `render.yaml`). Free instances can't have disks, so at minimum set
  `SESSION_STRING` so the Telegram login survives.
- **A watchdog reconnects Telethon** every `WATCHDOG_INTERVAL` seconds. Previously a
  dropped connection left the bot answering commands while monitoring nothing, with
  no indication anything was wrong.
- `post_shutdown` cancels the background tasks and closes the DB, HTTP session and
  web server, so restarts don't leak.
- Dependencies are pinned in `requirements.txt`. `telethon` was unpinned, meaning any
  upstream release could change preview behaviour on the next deploy.

### Python version — pin it, and not via `runtime.txt`

**Render does not read `runtime.txt`.** That is a Heroku convention. Render resolves
the version from the `PYTHON_VERSION` environment variable, falling back to a
`.python-version` file, and otherwise uses its own default — which is currently 3.14.
The repo's `runtime.txt` said `python-3.10.12` for months and was silently ignored.

That matters because **python-telegram-bot 21.x calls `asyncio.get_event_loop()`**
inside `run_polling()`. Through Python 3.13 that implicitly created a loop when none
was set; 3.14 raises instead:

```
RuntimeError: There is no current event loop in thread 'MainThread'.
```

The process then dies in `main()` before `post_init` runs, so the port is never bound
and the deploy fails outright. `main()` installs a loop up front to survive this
regardless of host Python — **that block is load-bearing despite looking like dead
code**, and `test_pipeline.py::test_main_installs_an_event_loop` pins it.

The pin is `.python-version` (3.11.9), which lives in the repo and needs no dashboard
change, plus `PYTHON_VERSION` in `render.yaml` for blueprint deploys. 3.11 also has
prebuilt manylinux wheels for `aiohttp`, so builds stop compiling it from source the
way they had to on 3.14.

## ✅ Tests

`python test_pipeline.py` — offline, no credentials, no network. Covers the
`WebPagePending` retry path, both `GetWebPagePreviewRequest` return shapes, cache
bounds, the DB singleton and the retention sweep.

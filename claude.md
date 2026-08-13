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

## ✅ Tests

`python test_pipeline.py` — offline, no credentials, no network. Covers the
`WebPagePending` retry path, both `GetWebPagePreviewRequest` return shapes, cache
bounds, the DB singleton and the retention sweep.

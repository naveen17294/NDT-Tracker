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
- **Primary Method (Native Previews):** The bot intercepts Telegram's native `MessageMediaWebPage` (link previews). Since Telegram's servers already bypassed Amazon's anti-bot protection to generate the preview, the bot instantly extracts the `title` and `description` from the payload without doing any HTTP requests.
- **Fallback 1 (Hidden API):** If the channel admin disabled link previews, the bot sends a hidden `GetWebPagePreviewRequest` to Telegram's servers, forcing Telegram to generate the preview metadata for us on the fly.
- **Fallback 2 (Python Scraper):** If all else fails, it falls back to a BeautifulSoup web scraper (which may occasionally be blocked by Amazon CAPTCHAs, hence being the last resort).

### 3. Price Extraction (`price_extractor.py`)
- **Comma Agnostic Regex:** The extraction formulas correctly capture numbers with or without commas (`[\d,]+`) ensuring `₹1000` isn't truncated to `100`.
- **Raw Number Fallback:** It detects raw numbers positioned immediately next to a link (e.g., `2950 : https://amzn.to/...`), even if the channel forgot to include a currency symbol like `₹`.

### 4. Deal Deduplication
- **Robust Hashing:** Instead of hashing the raw message text (which changes if the channel adds an emoji or slightly alters the URL), the bot hashes the **Cleaned Product Name + Price + Keyword**.
- If a channel posts the exact same dinner set three times with three different affiliate links, the bot recognizes the product name and price are identical, and silently drops the duplicates.

### 5. Alert Formatting (`notifier.py`)
- **HTML Engine:** Uses `HTML` parsing instead of `MarkdownV2` to format messages. Product names are wrapped in `html.escape()` to guarantee that weird characters (like `[` or `*`) never crash the Telegram delivery API.
- **Original Transmission:** Appends the exact raw message text sent by the channel into a sleek `<blockquote>` at the bottom of the alert, so the user can see exact coupon codes and context.

# 🎯 NDT — Project Context

**Developer:** Naveen  
**Created:** February 2026  
**Tech Stack:** Python, Telethon, python-telegram-bot v21, aiosqlite, aiohttp, BeautifulSoup4  
**Purpose:** Personal e-commerce deal tracker bot that monitors Telegram deal channels, matches product keywords (with smart synonym expansion & link scraping), and alerts instantly.  
**Status:** ✅ Complete & Ready for Credentials  

---

## 📋 Project Overview

NDT is a Telegram bot that passively monitors user-selected Telegram deal channels 24/7. When a channel post contains a deal for a product on your watchlist (or a synonym like "refrigerator" for "fridge"), NDT extracts the deal price and sends an instant alert notification.

### Core Features
- ✅ **Channel Monitoring:** Telethon UserClient listens to all deal channels you have joined.
- ✅ **Channel Selection UI:** `/channels` command lists your joined channels and lets you toggle which ones NDT watches.
- ✅ **Smart Synonym Matching:** Built-in dictionary matching 30+ Indian e-commerce product categories (e.g., "fridge" → "refrigerator", "double door", "mini fridge").
- ✅ **Telegram Preview Extraction (Primary):** If a deal post doesn't mention the product name, NDT reads Telegram's own link preview for the URL — Telegram's servers already got past Amazon's and Flipkart's anti-bot walls to build it, so this works where scraping gets a CAPTCHA.
- ✅ **Link Scraper (Last Resort):** Only when Telegram returns no preview does NDT follow the shortened link (`amzn.to`, `bit.ly`, `fkrt.it`) and scrape the title itself. Set `ENABLE_HTML_SCRAPER=false` to disable outbound scraping entirely.
- ✅ **Price Extraction:** Regex engine parses Indian price formats (`₹1,999`, `Rs 2,999`, `MRP ₹3,999`, `50% off`) directly from message text.
- ✅ **Deduplication:** Prevents repeated alert spam for the same deal post within 24 hours.
- ✅ **Private & Secured:** Owner-only authentication guard — only you can command the bot.

---

## 🏗️ Architecture

```
NDT/
├── bot.py                 # Telegram Bot command interface (/watch, /channels, /deals, etc.)
├── channel_monitor.py     # Telethon UserClient listening to channel messages
├── keyword_matcher.py     # Synonym dictionary & fuzzy matching engine
├── link_scraper.py        # Follows short links & scrapes product titles (Amazon, Flipkart, etc.)
├── price_extractor.py     # Regex parser for deal price, MRP, and discount %
├── database.py            # Async SQLite storage (watchlist, channels, deal hashes)
├── notifier.py            # Telegram alert formatting & sending
├── config.py              # Central settings & env variables
├── utils.py               # Text cleaning, URL extraction & currency helpers
├── test_pipeline.py       # Offline regression tests (no credentials needed)
├── requirements.txt       # Python dependencies (pinned)
├── Dockerfile             # Container setup
├── Procfile               # Process definition for cloud deployment
├── render.yaml            # Render blueprint (disk, health check, env)
├── railway.json           # Railway deployment config
├── .env.example           # Documented environment variables
├── .gitignore             # Shields db & session files
└── data/
    └── ndt.db             # Local SQLite database
```

---

## 🔑 Environment Variables Required

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Bot API token from `@BotFather` |
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `OWNER_ID` | Your Telegram User ID (numeric) |
| `SESSION_STRING` | Telethon session from `generate_session.py`. **Required on any headless host** — there is no terminal to type the login code into. |
| `DATA_PATH` / `SESSION_PATH` | Where the SQLite DB and session live. Point at a mounted disk in production; container filesystems are wiped on every restart. |
| `PORT` | Set by the host. When present the bot serves `/` and `/health` on it. |
| `KEEPALIVE_URL` | Self-ping target so free tiers don't idle the service out. Render provides `RENDER_EXTERNAL_URL` automatically. |
| `ENABLE_HTML_SCRAPER` | `false` runs preview-only, with no outbound HTTP scraping. |

See `.env.example` for the full list including tuning knobs.

---

## 🚀 Setup & Execution

### Local Execution:
```bash
pip install -r requirements.txt
python bot.py
```

### Tests:
```bash
python test_pipeline.py     # offline, no credentials or network required
```

### Deploying to Render:
`render.yaml` is a ready blueprint. Set `BOT_TOKEN`, `API_ID`, `API_HASH`, `OWNER_ID`
and `SESSION_STRING` as secrets in the dashboard, and use `/health` as the health
check path. Two things to get right or the service will not stay up:

1. **Attach a persistent disk** and set `DATA_PATH` / `SESSION_PATH` to it, otherwise
   the watchlist and tracked channels are lost on every deploy.
2. **Free web services sleep after ~15 minutes without inbound traffic.** The built-in
   self-ping handles this when `RENDER_EXTERNAL_URL` is available; otherwise point an
   external uptime pinger at `/health`.

### Deploying to Railway:
Connect repository to Railway. Set `BOT_TOKEN`, `API_ID`, `API_HASH`, `OWNER_ID` and
`SESSION_STRING` in Railway Environment Variables.

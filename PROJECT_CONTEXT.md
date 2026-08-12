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
- ✅ **Link Scraper (Fallback):** If a deal post doesn't mention the product name, NDT follows shortened links (`amzn.to`, `bit.ly`, `fkrt.it`) and scrapes the product title from the destination page.
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
├── requirements.txt       # Python dependencies
├── Dockerfile             # Container setup
├── Procfile               # Worker setup for cloud deployment
├── railway.json           # Railway deployment config
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

---

## 🚀 Setup & Execution

### Local Execution:
```bash
pip install -r requirements.txt
python bot.py
```

### Deploying to Railway:
Connect repository to Railway. Set `BOT_TOKEN`, `API_ID`, `API_HASH`, and `OWNER_ID` in Railway Environment Variables.

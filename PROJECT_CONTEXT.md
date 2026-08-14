# NDT — Project Context

Personal Telegram deal tracker. Monitors deal channels 24/7, matches posts against
a keyword watchlist (with synonyms and plural handling), extracts the price and
alerts instantly.

**Stack:** Python 3.11, Telethon, python-telegram-bot 21, asyncpg / aiosqlite,
aiohttp, BeautifulSoup4.

Why the code looks the way it does: `claude.md`. Deploying: `DEPLOYMENT.md`.
Release history: `CHANGELOG.md`.

## Features

- **Channel monitoring** — a Telethon user client listens to channels you've
  joined; `/channels` toggles which ones are watched. It lists only channels with
  `deal` or `sale` in the name, since a real account has joined hundreds;
  `/searchchannel` reaches the rest and `/addchannel` joins a new one.
- **Synonym matching** — built-in dictionary across ~40 Indian e-commerce
  categories (`fridge` → `refrigerator`, `double door`, …), plus custom synonyms
  and singular/plural handling.
- **Negative keywords** — `/watch shoes -kids -women` never alerts on a message
  containing a blocked term.
- **Rate your alerts** — 👍/👎 on every alert feeds `/channelreport`, a digest every
  8 hours of which channels are worth keeping. Both run on per-channel counters;
  no history of individual alerts is kept.
- **Product names from Telegram previews** — Telegram's servers already got past
  Amazon and Flipkart's bot protection, so previews are used before any scraping.
- **Price extraction** — `₹1,999`, `Rs 2,999`, `MRP ₹3,999`, `50% off`.
- **Deduplication** — on product + price + keyword, so the same deal reposted with
  a new affiliate link is dropped.
- **Owner-only** — only `OWNER_ID` can command the bot.

## Files

```
bot.py                    Commands, web server, background loops
channel_monitor.py        Telethon listener + matching pipeline
keyword_matcher.py        Synonyms, plurals, match tiers
link_scraper.py           HTML scraping (last-resort fallback)
price_extractor.py        Price / MRP / discount regex
database.py               Storage API
storage.py                SQLite + Postgres backends
notifier.py               Alert formatting
config.py                 Settings, all env-driven
utils.py                  Text cleaning, URL extraction
check_db.py               Verify a database connection
migrate_to_postgres.py    Copy SQLite -> Postgres
generate_session.py       Produce SESSION_STRING
test_pipeline.py          Offline tests (no credentials needed)
```

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in BOT_TOKEN, API_ID, API_HASH, OWNER_ID
python bot.py
```

```bash
python test_pipeline.py     # offline, no credentials or network
python check_db.py          # verify the configured database
```

Without `DATABASE_URL` it uses a local SQLite file — fine for development, wiped on
restart on a container. See `DEPLOYMENT.md`.

# NDT — Project Context

Personal Telegram deal tracker. Monitors deal channels 24/7 through **two bots** fed
by one Telethon connection:

- **bot 1** — a filter. Matches posts against a keyword watchlist (synonyms, plurals,
  negative keywords), extracts the price, alerts instantly.
- **bot 2** — a feed. Mirrors everything the same channels post, *except* what you
  muted, with link previews on so products are visible without opening each link.

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
- **Channel mirror (bot 2)** — every post from the tracked channels, previews forced
  on. 👎 hides that product for good and offers to mute a word from its title;
  `/muted` lists everything hidden with an undo.
- **Rate your alerts** — 👍/👎 on mirrored posts feeds `/channelreport`, a digest
  every 8 hours of which channels are worth keeping. Both run on per-channel
  counters; no history of individual alerts is kept.
- **Product names from Telegram previews** — Telegram's servers already got past
  Amazon and Flipkart's bot protection, so previews are used before any scraping.
- **Price extraction** — `₹1,999`, `Rs 2,999`, `MRP ₹3,999`, `50% off`.
- **Deduplication** — on the seller's own product id (Amazon ASIN, Flipkart pid), so
  the same deal is dropped whichever channel posts it and whatever affiliate tag it
  carries. Short links are followed to their destination first.
- **No command syntax to remember** — send any command bare and it asks for what it
  needs; `/menu` and a persistent keyboard reach everything without typing.
- **Owner-only** — only `OWNER_ID` can command the bot.

## Files

```
bot.py                    Commands, both bots' lifecycles, web server, loops
channel_monitor.py        Telethon listener + matching pipeline
mirror.py                 Bot 2 — the channel mirror, mutes, rate limiting
product_key.py            Canonical product identity (ASIN / pid) for dedup
keyword_matcher.py        Synonyms, plurals, match tiers
link_scraper.py           HTML scraping (last-resort) + short-link resolution
price_extractor.py        Price / MRP / discount regex
database.py               Storage API
storage.py                SQLite + Postgres backends
notifier.py               Bot 1 alert formatting
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

# NDT Tracker — system memory

Why the code is shaped the way it is. **Read the ⚠️ blocks before changing the code
they sit beside** — each marks a bug that shipped and looked fine from outside.

Ops: `DEPLOYMENT.md`. Releases: `CHANGELOG.md`. Overview: `PROJECT_CONTEXT.md`.

## Architecture

- **One Telethon client, two bots.** `telethon` (user API) silently monitors
  channels and is the only thing reading them. Both bots are fed from it:
  - **bot 1** (`BOT_TOKEN`) — a **filter**. Sends only what matches your watchlist.
  - **bot 2** (`BOT2_TOKEN`) — a **feed**. Sends everything *except* what you muted.
- **Storage** — `database.py` (API) over `storage.py` (SQLite or Postgres backend).
- **Headless** — `SESSION_STRING` lets Telethon log in with no terminal.

Pipeline: message → canonical product key → **mirror to bot 2** → match keywords →
resolve product name → extract price → deduplicate → alert via bot 1.

> ⚠️ **The mirror runs before the watchlist check, and is never awaited.** Bot 2's
> whole purpose is the posts bot 1 rejects, so gating it on a keyword match — or on
> a non-empty watchlist — silences it entirely. And it throttles itself to stay
> inside Telegram's per-chat rate limit, so awaiting it inline would add up to a
> second to every bot 1 alert. `_spawn_mirror()` detaches it and keeps a reference to
> the task, because a task with no strong reference can be collected mid-flight and
> take its exception with it. Pinned by
> `test_mirror_is_independent_of_the_watchlist` and `test_both_bots_receive_one_message`.

`run_polling()` can only be called once, so bot 1 owns the event loop and bot 2's
`Application` is driven by hand (`initialize` / `start` / `updater.start_polling`)
inside bot 1's `post_init`. Bot 2 must poll even though its commands come later: a
callback query goes to the bot that **sent** the message, so without polling every
👎 would silently do nothing.

## Commands

`/menu` — the button hub. `/watch` `/unwatch` `/updatesynonyms` `/exclude`
`/watchlist` — keywords. `/channels` `/searchchannel` `/addchannel`
`/untrackchannel` `/channelreport` — sources. `/muted` — bot 2's filter.
`/deals` `/stats` `/pause` `/resume` `/keyboard` `/cancel` — control.

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
link (`2950 : https://...`). Alerts are HTML (not MarkdownV2) with `html.escape()`,
so odd characters can't break delivery.

**Dedup is keyed on product identity** (`product_key.py`), which is the fix for the
duplicate you actually notice.

> ⚠️ **Neither the URL nor the message text can identify a deal.** Every channel
> rewrites the same link with its own affiliate tag and wraps it in its own
> marketing blast, so one product posted by two channels gives two different links
> and two different texts. The old hash was built from the cleaned product name, so
> it differed every time and both copies alerted. `canonical_key()` reduces a link
> to the seller's own id instead — `amazon:B0CX23V2ZK` from any of `/dp/`,
> `/gp/product/`, `?asin=`, with tracking stripped; Flipkart's `pid` beats its path
> slug. Price is deliberately **out** of the hash: the two posts often quote it
> differently and it is the same deal at ₹2,899 or ₹2,999.

An ASIN is **exactly ten characters**. Loosening that regex makes unrelated pages
dedup against each other, and the second deal is then silently swallowed —
`test_product_key_survives_affiliate_rewriting` pins both ends of the boundary.

Short links (`amzn.to`, `fkrt.it`) carry no id at all, so
`LinkScraper.resolve_final_url()` follows the redirect first (HEAD, falling back to
GET; cached per short link). Without that step, cross-channel dedup cannot work on
the shortened links deal channels actually post. `RESOLVE_SHORT_LINKS=false` opts
out. Where no key can be derived, the old name+price+keyword hash still applies.

## 5. Bot 2 — the mirror (`mirror.py`)

A Telegram channel is a bad shopping surface: walls of emoji, a bare shortened link,
no preview card, no way to tell a ₹400 case from a ₹40,000 phone without opening
every one. Bot 2 re-sends each post into a private chat **with the link preview
forced on and pinned to the product URL**, so the image, title and price render
inline. That is the entire reason bot 2 exists, which is why the preview is not
configurable.

> ⚠️ **`LinkPreviewOptions(url=...)` is doing real work — don't drop it for plain
> `disable_web_page_preview=False`.** With no explicit url Telegram previews the
> *first* link in the message, which can be the source channel or a coupon page
> rather than the item. The product link also leads the message body so the fallback
> path (an older python-telegram-bot with no `LinkPreviewOptions`) still previews the
> right thing.

Three filters keep the feed from becoming noise:

- **Mutes.** 👎 mutes that product by canonical key — so it stays muted when the
  channel reposts it under a fresh tag — then offers words from its title to mute a
  brand or category in one more tap. Words match on whole words only, so muting
  `saree` cannot silence `sareena`.
- **Cross-channel dedup.** `mirror_seen` is the same shape and lifetime as
  `matched_deals`: a key and a timestamp, pruned on the same timer. It is in the
  database rather than memory so a restart cannot resurrect a duplicate.
- **Rate limiting.** Telegram refuses past roughly one message per second to a
  single chat, and a deal channel bursts well past that. Sends are serialised and
  spaced, and a 429 is obeyed once rather than dropping the post.

Mutes are **decisions, not history** — never pruned on a timer, unlike the dedup
ledger beside them. `/muted` lists them with an undo, because a mute is permanent
and taken on a single tap: without a visible reversible list, a mis-tapped 👎 hides
a product for good with no way to discover it happened.

`callback_data` is capped at 64 bytes. A real product id fits (`amazon:B0CX23V2ZK`
is 18), so those buttons keep working after a restart; only long generic URL keys
need the in-memory token map, and a token lost to a restart degrades to "mute a word
instead" rather than erroring.

> ⚠️ **Never log a bot-token failure verbatim.** python-telegram-bot puts the
> rejected token *inside* its own error message ("The token `123:abc` was
> rejected"), so `logger.exception` on the mirror's startup path writes `BOT2_TOKEN`
> into the host's logs in plain text on every failed boot. `_scrub_token()` filters
> anything on its way to a log.

Bot 2 failing to start can never take bot 1 down: `_start_mirror()` contains every
error, leaves `monitor.mirror` as None, and `/stats` distinguishes "⚪ Off (reason)"
from "🔴 Failed to start". No `BOT2_TOKEN` is the supported single-bot mode.

**You must press Start on bot 2 once.** Telegram forbids a bot messaging a user who
has never opened a chat with it.

## 6. Feedback & the channel report

> ⚠️ **NDT keeps no history of alerts. Do not add one.** `matched_deals` is a dedup
> ledger pruned after `DEAL_RETENTION_DAYS`, so nothing per-alert survives a week —
> and nothing should. Anything that must outlive that is a **counter**, not a row.

`channel_stats` is one row per channel, forever, no matter how many alerts pass
through: `alerts`, `up`, `down`, plus `*_at_report` snapshots. The 8-hourly report
subtracts the snapshot to get "since last time" and then re-snapshots, which is how
it shows a delta without storing anything per alert.

**The 👍/👎 gesture lives on bot 2, not bot 1.** Bot 1 only ever sends what you
already asked for by name, so rating it says little; the mirror shows everything, and
a 👎 there both mutes the product and counts against the channel. Bot 1's alerts now
carry no buttons at all — but `bot.py` still **handles** the old `fb_*` callbacks,
because alerts already sitting in the chat keep their keyboard forever and tapping
one must not error.

Votes carry the **channel id in the button's `callback_data`**. That is the
mechanism, not a shortcut: the vote is attributable when pressed, so no record of the
alert has to exist. On a vote the keyboard is replaced with a static receipt — the
message's own markup is the only thing preventing a double vote, again because there
is no row to check against.

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

## 7. Persistence

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
- A brand-new **table** is fine to add to `schema()` alone — `CREATE TABLE IF NOT
  EXISTS` does create it on a database that already exists. That is how
  `muted_products`, `muted_terms` and `mirror_seen` reach a deployed database with no
  migration step. A new **column** is the case that silently does nothing:
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

## 8. Memory management

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

## 9. Startup

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

`python test_pipeline.py` — offline, no credentials or network. 319 assertions
covering the `WebPagePending` retry, both preview API shapes, the lookalike-word
false positives, category leakage, negative-keyword vetoes, channel counters
surviving a deal prune, cache bounds, Postgres retry/translation, and the
event-loop shim.

For the two bots specifically: affiliate-link collapsing and the ten-character ASIN
boundary, the same deal from two channels alerting once, mute/dedup/prune
interaction, the forced product preview, callback data staying inside 64 bytes with
a long key round-tripping through the token map, bot-token scrubbing, and
`test_both_bots_receive_one_message`, which drives a real message through
`_handle_message` and asserts bot 1 filters while bot 2 mirrors — including that an
**empty watchlist does not silence bot 2**.

import os
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# ═══════════════════════════════════════
#  NDT — Configuration
# ═══════════════════════════════════════

# Telegram Bot Token (from @BotFather)
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

# Telegram API credentials for User Client (Telethon)
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
SESSION_STRING = os.getenv('SESSION_STRING', '')
PHONE_NUMBER = os.getenv('PHONE_NUMBER', '')

# Your Telegram User ID (only you use this bot)
OWNER_ID = int(os.getenv('OWNER_ID', '0'))

# ── Storage ──
# When DATABASE_URL is set, NDT stores everything in that Postgres server instead of
# a local SQLite file, so the watchlist, tracked channels and deal history survive
# restarts and redeploys. This is the fix for hosts with no persistent disk (Render's
# free tier), where the container filesystem is wiped every time the service restarts.
#   postgresql://user:password@host:5432/dbname
# Leave it empty to use SQLite at DB_PATH.
DATABASE_URL = os.getenv('DATABASE_URL', '')

# Postgres pool tuning. The defaults are chosen for serverless providers (Neon,
# Supabase), which suspend an idle database and drop its connections.
#   min-size 0  — hold nothing open, so the database is free to suspend and we do
#                 not burn the free tier's compute hours keeping it awake.
#   max-idle 60 — recycle idle connections well before the provider kills them
#                 (Neon suspends at roughly 5 minutes), so we rarely hand a dead
#                 socket to a query in the first place.
#   retries     — covers the residual race, plus the cold start when a suspended
#                 database is waking up and briefly refuses connections.
DB_POOL_MIN_SIZE = _env_int('DB_POOL_MIN_SIZE', 0)
DB_POOL_MAX_SIZE = _env_int('DB_POOL_MAX_SIZE', 3)
DB_POOL_MAX_IDLE = _env_int('DB_POOL_MAX_IDLE', 60)
DB_COMMAND_TIMEOUT = _env_int('DB_COMMAND_TIMEOUT', 30)
DB_MAX_RETRIES = _env_int('DB_MAX_RETRIES', 3)
DB_RETRY_DELAY = float(os.getenv('DB_RETRY_DELAY', '0.5'))

# Paths — used by the SQLite backend and always by the Telethon session file.
# NOTE (deployment): on Render the container filesystem is EPHEMERAL. If you are on
# SQLite, point DATA_PATH at a mounted persistent disk (e.g. DATA_PATH=/var/data/) or
# set DATABASE_URL instead. SESSION_PATH has the same problem — set SESSION_STRING so
# the Telegram login does not depend on the filesystem at all.
DATA_PATH = os.getenv('DATA_PATH', './data/')
DB_PATH = os.path.join(DATA_PATH, 'ndt.db')
SESSION_PATH = os.getenv('SESSION_PATH', './sessions/')
SESSION_NAME = os.path.join(SESSION_PATH, 'ndt_user')

# ── Price Extraction Patterns ──
# Ordered by specificity (most specific first)
PRICE_PATTERNS = [
    r'₹\s?([\d,]+(?:\.\d{1,2})?)',          # ₹1,999 or ₹ 1,999 or ₹1000
    r'Rs\.?\s?([\d,]+(?:\.\d{1,2})?)',       # Rs.1,999 or Rs 1999
    r'INR\s?([\d,]+(?:\.\d{1,2})?)',         # INR 1999
    r'MRP\s?[₹:]?\s?([\d,]+(?:\.\d{1,2})?)', # MRP ₹1,999 or MRP: 1999
    r'(?:price|cost|at)\s?[₹:]?\s?([\d,]+(?:\.\d{1,2})?)', # price ₹1999
    r'([\d,]+(?:\.\d{1,2})?)\s*(?:[:\-]\s*)?https?://', # 2950 : https://amzn.to/
]

# Pattern for discount percentage
DISCOUNT_PATTERN = r'(\d{1,2})%\s*(?:off|discount|saving)'

# ── Telegram Link Preview (primary product-name source) ──
# Telegram's own servers render the preview, so they have already cleared Amazon's
# and Flipkart's anti-bot walls for us. This path is tried BEFORE any HTTP scraping.
PREVIEW_MAX_URLS = _env_int('PREVIEW_MAX_URLS', 2)      # URLs to ask Telegram about per message
PREVIEW_RETRIES = _env_int('PREVIEW_RETRIES', 4)        # WebPagePending re-polls before giving up
PREVIEW_RETRY_DELAY = float(os.getenv('PREVIEW_RETRY_DELAY', '1.5'))  # seconds between re-polls
PREVIEW_CACHE_SIZE = _env_int('PREVIEW_CACHE_SIZE', 300)
PREVIEW_CACHE_TTL = _env_int('PREVIEW_CACHE_TTL', 900)  # 15 minutes

# ── Link Scraper Settings (LAST-RESORT fallback only) ──
# Set ENABLE_HTML_SCRAPER=false to disable outbound HTTP scraping entirely and rely
# purely on Telegram previews (lowest memory, no CAPTCHA risk).
ENABLE_HTML_SCRAPER = _env_bool('ENABLE_HTML_SCRAPER', True)
SCRAPE_TIMEOUT = _env_int('SCRAPE_TIMEOUT', 5)          # seconds per URL
SCRAPE_CACHE_TTL = _env_int('SCRAPE_CACHE_TTL', 600)    # 10 minutes cache
SCRAPE_CACHE_SIZE = _env_int('SCRAPE_CACHE_SIZE', 200)  # hard entry cap (LRU eviction)
SCRAPE_MAX_URLS = _env_int('SCRAPE_MAX_URLS', 2)
SCRAPE_MAX_BYTES = _env_int('SCRAPE_MAX_BYTES', 400_000)  # stop reading huge pages

# User-Agent for scraping (mimics Chrome)
SCRAPE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

# Known URL shorteners (for redirect following)
URL_SHORTENERS = [
    'amzn.to', 'bit.ly', 'fkrt.it', 'dl.flipkart.com',
    't.co', 'tinyurl.com', 'goo.gl', 'ow.ly', 'is.gd',
    'buff.ly', 'rebrand.ly', 'cutt.ly', 'shorturl.at',
    'ekaro.in', 'linktr.ee',
]

# ── Keyword Matching ──
#
# Fuzzy matching is OFF by default, and that is deliberate.
#
# difflib.SequenceMatcher scores two 5-letter words that share 4 characters at
# exactly 0.800. With the old 0.75 threshold that made 'shoes' match 'homes',
# 'hoses' and 'shows' — so a home-furnishing deal fired a shoes alert. No threshold
# fixes this for short words: anything permissive enough to catch a real typo also
# catches every unrelated word one edit away.
#
# The legitimate use of fuzzy matching was tolerating plurals ('shoe' vs 'shoes'),
# and that is now handled exactly by singular/plural normalisation in
# keyword_matcher.py, which cannot produce this class of false positive.
#
# If you turn fuzzy back on, the gates below keep it to long, similar-length words
# where the ratio actually means something. Expect some noise regardless.
FUZZY_MATCH_ENABLED = _env_bool('FUZZY_MATCH_ENABLED', False)
FUZZY_MATCH_THRESHOLD = float(os.getenv('FUZZY_MATCH_THRESHOLD', '0.88'))
FUZZY_MIN_LENGTH = _env_int('FUZZY_MIN_LENGTH', 7)      # skip short words entirely
FUZZY_MAX_LENGTH_DIFF = _env_int('FUZZY_MAX_LENGTH_DIFF', 2)
# The watchlist changes rarely but was previously re-read from SQLite on EVERY
# incoming channel message. Cache it in memory for this many seconds.
WATCHLIST_CACHE_TTL = _env_int('WATCHLIST_CACHE_TTL', 30)

# ── Channel discovery (/channels) ──
#
# A real Telegram account has joined hundreds of broadcast channels, and /channels
# used to list every single one — pages of news, memes and group announcements to
# scroll past before reaching a deal channel. Only channels whose title or @username
# contains one of these terms are offered.
#
# These are plain substrings, so 'deal' also covers 'deals' and 'sale' covers
# 'sales' / 'wholesale'. Widen the net with the env var, comma-separated:
#   CHANNEL_NAME_FILTERS=deal,sale,offer,loot,discount
#
# Two deliberate escape hatches, because a filter that hides things needs them:
#   * channels you are already tracking are ALWAYS listed, even if the name does not
#     match — otherwise you could end up tracking something you cannot see to untrack;
#   * /searchchannel and /addchannel ignore the filter entirely.
CHANNEL_NAME_FILTERS = [
    term for term in (
        part.strip().lower()
        for part in os.getenv('CHANNEL_NAME_FILTERS', 'deal,sale').split(',')
    ) if term
]

# ── Deduplication & Retention ──
DEDUP_HOURS = _env_int('DEDUP_HOURS', 24)   # Don't re-notify same deal within this window
NOTIFIER_DEDUP_WINDOW = _env_int('NOTIFIER_DEDUP_WINDOW', 300)  # in-memory alert suppression
NOTIFIER_CACHE_SIZE = _env_int('NOTIFIER_CACHE_SIZE', 500)
# matched_deals used to grow forever — cleanup_old_deals() existed but was never called.
DEAL_RETENTION_DAYS = _env_int('DEAL_RETENTION_DAYS', 7)
MAINTENANCE_INTERVAL_HOURS = _env_int('MAINTENANCE_INTERVAL_HOURS', 6)

# ── Deployment / keep-alive ──
# Render free web services are reaped after ~15 minutes with no inbound request.
# RENDER_EXTERNAL_URL is injected by Render automatically; we self-ping it so the
# service stays awake. Set KEEPALIVE_URL manually on other hosts.
PORT = _env_int('PORT', 0)
KEEPALIVE_URL = os.getenv('KEEPALIVE_URL', '') or os.getenv('RENDER_EXTERNAL_URL', '')
KEEPALIVE_INTERVAL = _env_int('KEEPALIVE_INTERVAL', 600)  # 10 minutes
# Telethon silently drops its connection on flaky hosts; the watchdog reconnects it.
WATCHDOG_INTERVAL = _env_int('WATCHDOG_INTERVAL', 120)

def _ensure_dir(path, needed, purpose):
    """
    Create a working directory, without letting it kill the process at import.

    Two failure modes this guards against, both of which crashed the app before
    main() could run — so the port was never bound and the host reported a failed
    deploy with only a traceback to go on:

    * The path is no longer creatable. A leftover DATA_PATH=/var/data/ from a
      previous disk-backed deployment points at nothing once the disk is detached,
      and the process cannot create /var/data itself.
    * The path is not needed at all. With DATABASE_URL set the SQLite file is never
      opened, and with SESSION_STRING set the session file is never written, so
      failing over a directory neither will ever use is pure own-goal.
    """
    if not needed:
        return
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        # Deliberately not fatal. If the directory really is required, the failure
        # resurfaces at first use with context about what was being opened.
        print(f"WARNING: could not create {purpose} directory {path!r}: {e}")


# Only create what this configuration actually uses.
_ensure_dir(DATA_PATH, not DATABASE_URL, 'SQLite data')
_ensure_dir(SESSION_PATH, not SESSION_STRING, 'Telethon session')

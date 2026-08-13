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
FUZZY_MATCH_THRESHOLD = 0.75  # Minimum similarity for fuzzy match
# The watchlist changes rarely but was previously re-read from SQLite on EVERY
# incoming channel message. Cache it in memory for this many seconds.
WATCHLIST_CACHE_TTL = _env_int('WATCHLIST_CACHE_TTL', 30)

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

# Create directories
os.makedirs(DATA_PATH, exist_ok=True)
os.makedirs(SESSION_PATH, exist_ok=True)

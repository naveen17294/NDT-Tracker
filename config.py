import os
from dotenv import load_dotenv

load_dotenv()

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

# Paths
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

# ── Link Scraper Settings ──
SCRAPE_TIMEOUT = 5  # seconds per URL
SCRAPE_CACHE_TTL = 600  # 10 minutes cache

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

# ── Deduplication ──
DEDUP_HOURS = 24  # Don't re-notify same deal within this window

# Create directories
os.makedirs(DATA_PATH, exist_ok=True)
os.makedirs(SESSION_PATH, exist_ok=True)

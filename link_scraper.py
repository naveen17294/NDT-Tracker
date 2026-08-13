import asyncio
import logging
import time
from collections import OrderedDict
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from config import (
    SCRAPE_CACHE_SIZE,
    SCRAPE_CACHE_TTL,
    SCRAPE_HEADERS,
    SCRAPE_MAX_BYTES,
    SCRAPE_MAX_URLS,
    SCRAPE_TIMEOUT,
    URL_SHORTENERS,
)
from utils import extract_urls

logger = logging.getLogger(__name__)


class LinkScraper:
    """
    Follow URLs and scrape product titles from e-commerce pages.

    LAST-RESORT fallback only — ChannelMonitor tries Telegram's own link previews
    first, because Telegram's servers get past the anti-bot walls that serve this
    scraper a CAPTCHA.

    Two memory fixes live here:
    * One shared aiohttp.ClientSession instead of building a new one (and a new TLS
      context, and a new connector pool) for every URL.
    * A hard-capped LRU cache. The old cache only dropped *expired* entries once it
      passed 500, so a steady stream of fresh URLs grew it without limit.
    """

    def __init__(self):
        self._cache = OrderedDict()  # url -> (title, timestamp)
        self._session = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self):
        """Lazily create (and reuse) the shared HTTP session."""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=SCRAPE_TIMEOUT),
                        headers=SCRAPE_HEADERS,
                        # Keep the pool small; this is a fallback path, not a crawler.
                        connector=aiohttp.TCPConnector(
                            limit=8,
                            limit_per_host=2,
                            ttl_dns_cache=300,
                            ssl=False,
                        ),
                    )
        return self._session

    async def close(self):
        """Close the shared session (called on shutdown)."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._cache.clear()

    def _is_cached(self, url):
        """Check if URL result is cached and not expired."""
        entry = self._cache.get(url)
        if not entry:
            return None
        title, ts = entry
        if time.time() - ts >= SCRAPE_CACHE_TTL:
            self._cache.pop(url, None)
            return None
        self._cache.move_to_end(url)  # LRU touch
        return title

    def _cache_result(self, url, title):
        """Cache a scrape result, evicting the least-recently-used entry past the cap."""
        self._cache[url] = (title, time.time())
        self._cache.move_to_end(url)
        while len(self._cache) > SCRAPE_CACHE_SIZE:
            self._cache.popitem(last=False)

    def _is_shortener(self, url):
        """Check if URL is a known shortener."""
        try:
            domain = urlparse(url).netloc.lower()
            return any(short in domain for short in URL_SHORTENERS)
        except Exception:
            return False

    def _get_site_specific_selector(self, url):
        """Get CSS selectors for known e-commerce sites."""
        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            return None

        selectors = {
            'amazon': [
                {'id': 'productTitle'},
                {'id': 'title'},
            ],
            'flipkart': [
                {'class': 'VU-ZEz'},
                {'class': 'B_NuCI'},
                {'tag': 'h1', 'class': 'yhB1nd'},
            ],
            'myntra': [
                {'class': 'pdp-title'},
                {'class': 'pdp-name'},
            ],
            'croma': [
                {'tag': 'h1', 'class': 'pd-title'},
            ],
            'reliancedigital': [
                {'tag': 'h1', 'class': 'pdp__product-title'},
            ],
            'vijaysales': [
                {'tag': 'h1', 'class': 'product-title'},
            ],
            'jiomart': [
                {'tag': 'h1', 'class': 'product-header-name'},
            ],
            'tatacliq': [
                {'tag': 'h1', 'class': 'ProductDetailsMainBlock__title'},
            ],
        }

        for site, site_selectors in selectors.items():
            if site in domain:
                return site_selectors
        return None

    def _extract_title_from_html(self, html, url):
        """Extract product title from HTML using multiple strategies."""
        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            return None

        try:
            # ── Strategy 1: Site-specific selectors ──
            site_selectors = self._get_site_specific_selector(url)
            if site_selectors:
                for sel in site_selectors:
                    element = None
                    if 'id' in sel:
                        element = soup.find(id=sel['id'])
                    elif 'class' in sel and 'tag' in sel:
                        element = soup.find(sel['tag'], class_=sel['class'])
                    elif 'class' in sel:
                        element = soup.find(class_=sel['class'])

                    if element and element.get_text(strip=True):
                        title = element.get_text(strip=True)
                        if len(title) > 5:  # Sanity check
                            return title

            # ── Strategy 2: Open Graph title (og:title) ──
            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content', '').strip():
                title = og_title['content'].strip()
                if len(title) > 5:
                    return title

            # ── Strategy 3: Twitter title ──
            tw_title = soup.find('meta', attrs={'name': 'twitter:title'})
            if tw_title and tw_title.get('content', '').strip():
                title = tw_title['content'].strip()
                if len(title) > 5:
                    return title

            # ── Strategy 4: <title> tag ──
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
                # Clean common suffixes
                for suffix in [' - Amazon.in', '| Flipkart', ' - Myntra', ' | Croma',
                              ' - Buy Online', ' at Best Price', ' Online India',
                              ' | Buy Online', ' - Reliance Digital']:
                    if title.endswith(suffix):
                        title = title[:-len(suffix)].strip()
                if len(title) > 5:
                    return title

            # ── Strategy 5: First <h1> tag ──
            h1 = soup.find('h1')
            if h1 and h1.get_text(strip=True):
                title = h1.get_text(strip=True)
                if len(title) > 5:
                    return title

            return None
        finally:
            # BeautifulSoup trees are heavily self-referential; decomposing breaks the
            # cycles so the memory comes back at once instead of waiting for the GC's
            # generational sweep.
            soup.decompose()

    async def scrape_url(self, url):
        """
        Follow a URL (handling redirects) and extract the product title.

        Returns:
            str or None: Product title if found
        """
        # Check cache first
        cached = self._is_cached(url)
        if cached:
            return cached

        try:
            session = await self._get_session()
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    logger.debug(f"Scrape failed for {url}: HTTP {response.status}")
                    return None

                content_type = response.headers.get('Content-Type', '')
                if 'html' not in content_type.lower():
                    logger.debug(f"Skipping non-HTML response for {url}: {content_type}")
                    return None

                # Get the final URL after redirects
                final_url = str(response.url)

                # Stream and stop early instead of buffering the whole page — some
                # product pages are several MB of inline JSON.
                chunks = []
                total = 0
                async for chunk in response.content.iter_chunked(16384):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= SCRAPE_MAX_BYTES:
                        break
                html = b''.join(chunks).decode('utf-8', errors='ignore')
                del chunks

                title = self._extract_title_from_html(html, final_url)

                if title:
                    self._cache_result(url, title)
                    logger.info(f"Scraped title: '{title}' from {url}")

                return title

        except asyncio.TimeoutError:
            logger.debug(f"Scrape timeout for {url}")
            return None
        except Exception as e:
            logger.debug(f"Scrape error for {url}: {e}")
            return None

    async def scrape_message_urls(self, text):
        """
        Extract all URLs from message text, scrape each, return titles.

        Returns:
            list of dict: [{'url': str, 'title': str}, ...]
        """
        urls = extract_urls(text)
        if not urls:
            return []

        results = []
        for url in urls[:SCRAPE_MAX_URLS]:
            title = await self.scrape_url(url)
            if title:
                results.append({'url': url, 'title': title})

        return results

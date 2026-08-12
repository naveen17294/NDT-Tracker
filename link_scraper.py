import aiohttp
import asyncio
import logging
import time
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from utils import extract_urls
from config import SCRAPE_HEADERS, SCRAPE_TIMEOUT, SCRAPE_CACHE_TTL, URL_SHORTENERS

logger = logging.getLogger(__name__)


class LinkScraper:
    """Follow URLs and scrape product titles from e-commerce pages."""

    def __init__(self):
        self._cache = {}  # {url: (title, timestamp)}

    def _is_cached(self, url):
        """Check if URL result is cached and not expired."""
        if url in self._cache:
            title, ts = self._cache[url]
            if time.time() - ts < SCRAPE_CACHE_TTL:
                return title
        return None

    def _cache_result(self, url, title):
        """Cache a scrape result."""
        self._cache[url] = (title, time.time())
        # Cleanup old entries (keep cache small)
        if len(self._cache) > 500:
            cutoff = time.time() - SCRAPE_CACHE_TTL
            self._cache = {k: v for k, v in self._cache.items() if v[1] > cutoff}

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
            timeout = aiohttp.ClientTimeout(total=SCRAPE_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=SCRAPE_HEADERS
            ) as session:
                async with session.get(url, allow_redirects=True, ssl=False) as response:
                    if response.status != 200:
                        logger.debug(f"Scrape failed for {url}: HTTP {response.status}")
                        return None

                    # Get the final URL after redirects
                    final_url = str(response.url)

                    # Read HTML (limit to 500KB to avoid huge pages)
                    html = await response.text(encoding='utf-8', errors='ignore')
                    if len(html) > 500000:
                        html = html[:500000]

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
        # Scrape up to 3 URLs per message (avoid overloading)
        for url in urls[:3]:
            title = await self.scrape_url(url)
            if title:
                results.append({'url': url, 'title': title})

        return results

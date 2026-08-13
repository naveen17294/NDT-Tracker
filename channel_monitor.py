import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import GetWebPagePreviewRequest
from telethon.tl.types import (
    Channel,
    MessageMediaWebPage,
    WebPage,
    WebPageEmpty,
    WebPagePending,
)

from config import (
    API_HASH,
    API_ID,
    ENABLE_HTML_SCRAPER,
    OWNER_ID,
    PREVIEW_CACHE_SIZE,
    PREVIEW_CACHE_TTL,
    PREVIEW_MAX_URLS,
    PREVIEW_RETRIES,
    PREVIEW_RETRY_DELAY,
    SESSION_NAME,
    SESSION_STRING,
    WATCHLIST_CACHE_TTL,
)
from database import Database
from keyword_matcher import KeywordMatcher
from link_scraper import LinkScraper
from notifier import Notifier
from price_extractor import PriceExtractor
from utils import clean_text, extract_urls, remove_emojis

logger = logging.getLogger(__name__)


class ChannelMonitor:
    """
    Telethon UserClient that monitors deal channels for matching products.

    Pipeline (preview-first — see _resolve_preview):
    1. New message arrives in a monitored channel
    2. Read Telegram's NATIVE link preview off the message, if the channel left it on
    3. Match message text + preview against the watchlist (keyword_matcher)
    4. Still no match — ask Telegram's servers to RENDER a preview for each URL
    5. Only if Telegram gives us nothing — fall back to the HTML scraper (link_scraper)
    6. Extract price, deduplicate, notify
    """

    def __init__(self, bot_instance):
        """
        Args:
            bot_instance: python-telegram-bot Bot for sending notifications
        """
        if SESSION_STRING:
            logger.info("Using StringSession for authentication.")
            session = StringSession(SESSION_STRING)
        else:
            logger.info("Using local SQLite session file.")
            session = SESSION_NAME

        self.client = TelegramClient(session, API_ID, API_HASH)
        self.matcher = KeywordMatcher()
        self.scraper = LinkScraper()
        self.price_extractor = PriceExtractor()
        self.db = Database()
        self.notifier = Notifier(bot_instance)
        self.paused = False
        self._monitored_channel_ids = set()

        # ── Bounded in-memory caches ──
        # Telegram preview lookups are network calls; deal channels repost the same
        # affiliate link constantly, so cache resolved previews. OrderedDict + explicit
        # cap gives us true LRU eviction instead of a dict that only ever grows.
        self._preview_cache = OrderedDict()  # url -> (title, description, timestamp)
        # The watchlist was previously re-read from SQLite on every single message.
        self._watchlist_cache = None
        self._watchlist_cache_at = 0.0

    async def start(self):
        """Start the Telethon client and register event handlers."""
        await self.db.init()
        await self.client.start(phone=lambda: input('Enter phone number: '))
        logger.info("Telethon client started")

        # Load active channels
        await self.refresh_channels()

        # Register new message handler
        @self.client.on(events.NewMessage)
        async def on_new_message(event):
            try:
                await self._handle_message(event)
            except Exception as e:
                # One malformed message must never kill the event handler and take
                # the whole monitor offline.
                logger.exception(f"Error handling message: {e}")

        logger.info(f"Monitoring {len(self._monitored_channel_ids)} channels")

    async def refresh_channels(self):
        """Reload active channels from database."""
        channels = await self.db.get_active_channels()
        self._monitored_channel_ids = {ch['channel_id'] for ch in channels}
        logger.info(f"Refreshed channels: {len(self._monitored_channel_ids)} active")

    def invalidate_watchlist_cache(self):
        """Drop the cached watchlist so the next message re-reads it from SQLite."""
        self._watchlist_cache = None
        self._watchlist_cache_at = 0.0

    async def _get_watchlist(self):
        """Watchlist, cached for WATCHLIST_CACHE_TTL seconds."""
        now = time.time()
        if self._watchlist_cache is not None and (now - self._watchlist_cache_at) < WATCHLIST_CACHE_TTL:
            return self._watchlist_cache
        self._watchlist_cache = await self.db.get_all_keywords()
        self._watchlist_cache_at = now
        return self._watchlist_cache

    async def get_joined_channels(self):
        """Get list of all channels the user has joined (for channel selection UI)."""
        channels = []
        async for dialog in self.client.iter_dialogs():
            if isinstance(dialog.entity, Channel) and dialog.entity.broadcast:
                channels.append({
                    'channel_id': dialog.entity.id,
                    'channel_name': dialog.entity.title,
                    'channel_username': dialog.entity.username or '',
                })
        return channels

    async def resolve_and_join_channel(self, identifier):
        """Join a channel by username/link and return its info."""
        try:
            # Join the channel
            await self.client(JoinChannelRequest(identifier))
            # Get entity info
            entity = await self.client.get_entity(identifier)
            if isinstance(entity, Channel):
                return {
                    'channel_id': entity.id,
                    'channel_name': entity.title,
                    'channel_username': entity.username or '',
                }
            return None
        except Exception as e:
            logger.error(f"Failed to join/resolve channel {identifier}: {e}")
            raise e

    # ══════════════════════════════════════
    #  Telegram link-preview resolution
    # ══════════════════════════════════════

    def _cache_get(self, url):
        entry = self._preview_cache.get(url)
        if not entry:
            return None
        title, desc, ts = entry
        if time.time() - ts > PREVIEW_CACHE_TTL:
            self._preview_cache.pop(url, None)
            return None
        self._preview_cache.move_to_end(url)  # LRU touch
        return title, desc

    def _cache_put(self, url, title, desc):
        self._preview_cache[url] = (title, desc, time.time())
        self._preview_cache.move_to_end(url)
        while len(self._preview_cache) > PREVIEW_CACHE_SIZE:
            self._preview_cache.popitem(last=False)  # evict least-recently-used

    @staticmethod
    def _unwrap_media(result):
        """
        Normalise the GetWebPagePreviewRequest return value.

        Older Telegram layers return a MessageMedia directly; newer ones wrap it in a
        messages.WebPagePreview that carries the media under `.media`. Handle both so
        an SDK/layer bump doesn't silently break preview resolution again.
        """
        return getattr(result, 'media', result)

    async def _resolve_preview(self, url):
        """
        Ask Telegram's servers to render the link preview for `url`.

        Telegram has already defeated Amazon/Flipkart bot protection to build these
        previews, so this is far more reliable than scraping the page ourselves.

        Telegram answers WebPagePending the first time it sees a URL while it fetches
        the page in the background, so we re-poll a few times before giving up. (The
        previous version crashed here with NameError: asyncio was never imported, the
        bare `except` swallowed it into a debug log, and every message fell through to
        the HTML scraper — which is why preview scraping appeared not to work at all.)

        Returns:
            (title, description) or (None, None)
        """
        cached = self._cache_get(url)
        if cached:
            logger.debug(f"Preview cache hit: {url}")
            return cached

        for attempt in range(1, PREVIEW_RETRIES + 1):
            try:
                result = await self.client(GetWebPagePreviewRequest(message=url))
            except Exception as e:
                logger.warning(f"Telegram preview request failed for {url}: {e}")
                return None, None

            media = self._unwrap_media(result)

            if not isinstance(media, MessageMediaWebPage):
                logger.debug(f"No web page media for {url} ({type(media).__name__})")
                return None, None

            webpage = media.webpage

            if isinstance(webpage, WebPage):
                title = (getattr(webpage, 'title', '') or '').strip()
                desc = (getattr(webpage, 'description', '') or '').strip()
                if title or desc:
                    self._cache_put(url, title, desc)
                    logger.info(f"✅ Telegram preview resolved (attempt {attempt}): '{title[:70]}'")
                    return title, desc
                logger.debug(f"Preview for {url} had no title/description")
                return None, None

            if isinstance(webpage, WebPagePending):
                # Telegram is still fetching the page — wait and ask again.
                if attempt < PREVIEW_RETRIES:
                    logger.debug(
                        f"Preview pending for {url}, retrying "
                        f"({attempt}/{PREVIEW_RETRIES})"
                    )
                    await asyncio.sleep(PREVIEW_RETRY_DELAY)
                    continue
                logger.info(f"Preview still pending after {PREVIEW_RETRIES} attempts: {url}")
                return None, None

            if isinstance(webpage, WebPageEmpty):
                logger.debug(f"Telegram returned an empty preview for {url}")
                return None, None

            return None, None

        return None, None

    @staticmethod
    def _read_native_preview(message):
        """Read the preview Telegram already attached to the message, if any."""
        media = getattr(message, 'media', None)
        if not isinstance(media, MessageMediaWebPage):
            return '', ''
        webpage = media.webpage
        if not isinstance(webpage, WebPage):
            return '', ''
        title = (getattr(webpage, 'title', '') or '').strip()
        desc = (getattr(webpage, 'description', '') or '').strip()
        return title, desc

    # ══════════════════════════════════════
    #  Message handling
    # ══════════════════════════════════════

    async def _handle_message(self, event):
        """Process a new message from a channel."""
        # Skip if paused
        if self.paused:
            return

        # Check if message is from a monitored channel
        # Telethon event.chat_id for channels is usually -100xxxxxxxx.
        # But our database (from dialog.entity.id) stores the raw positive ID xxxxxxxx.
        chat_id = event.chat_id

        raw_id = chat_id
        if chat_id < 0:
            str_id = str(abs(chat_id))
            if str_id.startswith("100"):
                try:
                    raw_id = int(str_id[3:])
                except ValueError:
                    pass

        if chat_id not in self._monitored_channel_ids and raw_id not in self._monitored_channel_ids:
            return

        # Get message text (body + caption for media messages)
        text = event.message.message or ''

        # ── Preview source 1: the native preview already on the message ──
        # Free — the channel admin left link previews enabled, so Telegram's servers
        # already resolved the product page and shipped us the metadata.
        preview_title, preview_desc = self._read_native_preview(event.message)

        # Text the matcher searches: message body plus whatever the preview told us.
        search_text = text
        if preview_title:
            search_text += f"\n{preview_title}"
        if preview_desc:
            search_text += f"\n{preview_desc}"

        if not search_text.strip():
            return

        # Get channel info
        try:
            chat = await event.get_chat()
            channel_name = chat.title if hasattr(chat, 'title') else 'Unknown'
            channel_username = f"@{chat.username}" if hasattr(chat, 'username') and chat.username else channel_name
        except Exception:
            channel_name = 'Unknown'
            channel_username = 'Unknown'

        logger.info(f"📨 Scanning new message from tracked channel: {channel_name}")

        watchlist = await self._get_watchlist()
        if not watchlist:
            return

        # ══════════════════════════════════════
        #  MATCHING PIPELINE
        # ══════════════════════════════════════

        match_result = None
        match_source = 'text'
        product_name = ''
        urls = extract_urls(text)

        # ── Step 1: message text (+ native preview) ──
        match_result = self.matcher.match(search_text, watchlist)
        if match_result:
            if preview_title:
                # Prefer the real product title from the preview over the first 100
                # chars of a marketing blast — better alerts AND a far more stable
                # dedup hash.
                match_source = 'native_preview'
                product_name = preview_title[:150]
            else:
                match_source = 'text'
                product_name = clean_text(remove_emojis(text))[:100]

        # ── Step 2: no match — have Telegram render a preview for each URL ──
        if not match_result and urls:
            for url in urls[:PREVIEW_MAX_URLS]:
                title, desc = await self._resolve_preview(url)
                if not title and not desc:
                    continue
                combined = f"{title}\n{desc}".strip()
                match_result = self.matcher.match(combined, watchlist)
                if match_result:
                    match_source = 'telegram_preview_api'
                    product_name = (title or combined)[:150]
                    break

        # ── Step 3: last resort — scrape the page ourselves ──
        # Amazon/Flipkart serve CAPTCHAs to datacenter IPs, so this rarely wins;
        # set ENABLE_HTML_SCRAPER=false to skip it entirely.
        if not match_result and urls and ENABLE_HTML_SCRAPER:
            scraped_results = await self.scraper.scrape_message_urls(text)
            for scraped in scraped_results:
                match_result = self.matcher.match(scraped['title'], watchlist)
                if match_result:
                    match_source = 'link_scrape'
                    product_name = scraped['title'][:150]
                    break

        # No match found — skip
        if not match_result:
            return

        # ── Step 4: Extract price from message text ──
        price_info = self.price_extractor.extract(text)

        # ── Step 5: Deduplication ──
        deal_hash = self._generate_deal_hash(product_name, price_info['price'], match_result['keyword'])
        if await self.db.is_deal_seen(deal_hash):
            logger.debug(f"Duplicate deal skipped: {match_result['keyword']}")
            return

        # ── Step 6: Build message link ──
        message_link = None
        if hasattr(event.message, 'id') and channel_username.startswith('@'):
            message_link = f"https://t.me/{channel_username[1:]}/{event.message.id}"

        deal_url = urls[0] if urls else None

        # ── Step 7: Save and notify ──
        deal_info = {
            'product_name': product_name,
            'keyword': match_result['keyword'],
            'matched_term': match_result['matched_term'],
            'confidence': match_result['confidence'],
            'match_type': match_result['match_type'],
            'match_source': match_source,
            'price': price_info['price'],
            'original_price': price_info['original_price'],
            'discount': price_info['discount'],
            'channel_name': channel_username,
            'message_link': message_link,
            'deal_url': deal_url,
            'timestamp': time.time(),
            'raw_text': text,
        }

        # Save to database
        await self.db.save_deal(
            deal_hash=deal_hash,
            keyword_matched=match_result['keyword'],
            product_name=product_name,
            price=str(price_info['price']) if price_info['price'] else '',
            channel_name=channel_username,
            message_link=message_link or '',
        )

        # Send notification
        await self.notifier.send_deal_alert(OWNER_ID, deal_info)
        logger.info(
            f"Deal matched! Keyword='{match_result['keyword']}' "
            f"Product='{product_name[:50]}' "
            f"Source={match_source} Channel={channel_username}"
        )

    def _generate_deal_hash(self, product_name, price, keyword):
        """Generate a hash for deduplication based on product and price."""
        # Clean product name to remove slight variations
        clean_name = re.sub(r'[^a-zA-Z0-9]', '', product_name.lower())[:50]
        content = f"{clean_name}|{price}|{keyword}"
        return hashlib.md5(content.encode()).hexdigest()

    async def pause(self):
        """Pause monitoring."""
        self.paused = True
        logger.info("Monitoring paused")

    async def resume(self):
        """Resume monitoring."""
        self.paused = False
        logger.info("Monitoring resumed")

    def is_connected(self):
        """True when the Telethon client still holds a live connection."""
        try:
            return bool(self.client.is_connected())
        except Exception:
            return False

    async def ensure_connected(self):
        """
        Reconnect the Telethon client if it has dropped.

        Long-running free-tier containers get their idle sockets cut; without this the
        bot kept answering commands while silently monitoring nothing.
        """
        if self.is_connected():
            return True
        logger.warning("Telethon client disconnected — reconnecting...")
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error("Telethon reconnected but the session is no longer authorized.")
                return False
            await self.refresh_channels()
            logger.info("✅ Telethon client reconnected")
            return True
        except Exception as e:
            logger.error(f"Telethon reconnect failed: {e}")
            return False

    async def run(self):
        """Run the Telethon client (blocking)."""
        await self.client.run_until_disconnected()

    async def stop(self):
        """Stop the Telethon client and release its resources."""
        try:
            await self.client.disconnect()
        except Exception as e:
            logger.debug(f"Error disconnecting Telethon client: {e}")
        await self.scraper.close()
        self._preview_cache.clear()
        logger.info("Telethon client stopped")

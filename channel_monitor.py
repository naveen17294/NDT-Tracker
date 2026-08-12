import logging
import hashlib
import time
import os
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel
from telethon.tl.functions.channels import JoinChannelRequest
from keyword_matcher import KeywordMatcher
from link_scraper import LinkScraper
from price_extractor import PriceExtractor
from database import Database
from notifier import Notifier
from utils import extract_urls, clean_text, remove_emojis
from config import API_ID, API_HASH, SESSION_NAME, OWNER_ID, SESSION_STRING

logger = logging.getLogger(__name__)


class ChannelMonitor:
    """
    Telethon UserClient that monitors deal channels for matching products.

    Pipeline:
    1. New message arrives in a monitored channel
    2. Check message text against watchlist (keyword_matcher)
    3. If no match — extract URLs, scrape product titles (link_scraper)
    4. If match found — extract price from message (price_extractor)
    5. Deduplicate and notify (notifier)
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
            await self._handle_message(event)

        logger.info(f"Monitoring {len(self._monitored_channel_ids)} channels")

    async def refresh_channels(self):
        """Reload active channels from database."""
        channels = await self.db.get_active_channels()
        self._monitored_channel_ids = {ch['channel_id'] for ch in channels}
        logger.info(f"Refreshed channels: {len(self._monitored_channel_ids)} active")

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
            # logger.debug(f"Ignored message from unmonitored channel ID: {chat_id}")
            return

        # Get message text (body + caption for media messages)
        text = ''
        if event.message.message:
            text = event.message.message

        # Leverage Telegram's native link previews! (Solves the Amazon scraping block)
        preview_title = ""
        preview_desc = ""
        if hasattr(event.message, 'media') and event.message.media:
            from telethon.tl.types import MessageMediaWebPage, WebPage
            if isinstance(event.message.media, MessageMediaWebPage):
                webpage = event.message.media.webpage
                if isinstance(webpage, WebPage):
                    preview_title = getattr(webpage, 'title', '') or ''
                    preview_desc = getattr(webpage, 'description', '') or ''
                    
                    # Append preview text to the main text so the KeywordMatcher can find it
                    if preview_title:
                        text += f"\n{preview_title}"
                    if preview_desc:
                        text += f"\n{preview_desc}"

        if not text.strip():
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

        # Get watchlist
        watchlist = await self.db.get_all_keywords()
        if not watchlist:
            return

        # ══════════════════════════════════════
        #  MATCHING PIPELINE
        # ══════════════════════════════════════

        match_result = None
        match_source = 'text'
        product_name = ''

        # ── Step 1: Check message text directly ──
        match_result = self.matcher.match(text, watchlist)
        if match_result:
            match_source = 'text'
            # Use the message text as product description
            cleaned = clean_text(remove_emojis(text))
            product_name = cleaned[:100]  # First 100 chars as product name

        # ── Step 2: If no text match, try link scraping ──
        if not match_result:
            urls = extract_urls(text)
            if urls:
                # First, try asking Telegram's servers to generate a preview for us
                from telethon.tl.functions.messages import GetWebPagePreviewRequest
                from telethon.tl.types import MessageMediaWebPage, WebPage
                try:
                    preview_media = await self.client(GetWebPagePreviewRequest(message=urls[0]))
                    if isinstance(preview_media, MessageMediaWebPage) and isinstance(preview_media.webpage, WebPage):
                        preview_title = getattr(preview_media.webpage, 'title', '') or ''
                        preview_desc = getattr(preview_media.webpage, 'description', '') or ''
                        combined_text = f"{preview_title}\n{preview_desc}"
                        
                        if combined_text.strip():
                            match_result = self.matcher.match(combined_text, watchlist)
                            if match_result:
                                match_source = 'telegram_preview_api'
                                product_name = preview_title[:100] if preview_title else combined_text[:100]
                except Exception as e:
                    logger.debug(f"Manual preview request failed: {e}")

                # If Telegram couldn't generate a preview, fallback to our Python scraper
                if not match_result:
                    scraped_results = await self.scraper.scrape_message_urls(text)
                    for scraped in scraped_results:
                        match_result = self.matcher.match(scraped['title'], watchlist)
                        if match_result:
                            match_source = 'link_scrape'
                            product_name = scraped['title']
                            break

        # No match found — skip
        if not match_result:
            return

        # ── Step 3: Extract price from message text ──
        price_info = self.price_extractor.extract(text)

        # ── Step 4: Deduplication ──
        deal_hash = self._generate_deal_hash(product_name, price_info['price'], match_result['keyword'])
        if await self.db.is_deal_seen(deal_hash):
            logger.debug(f"Duplicate deal skipped: {match_result['keyword']}")
            return

        # ── Step 5: Build message link ──
        message_link = None
        if hasattr(event.message, 'id') and channel_username.startswith('@'):
            message_link = f"https://t.me/{channel_username[1:]}/{event.message.id}"

        # Extract deal URL from message
        deal_url = None
        urls = extract_urls(text)
        if urls:
            deal_url = urls[0]

        # ── Step 6: Save and notify ──
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

    async def run(self):
        """Run the Telethon client (blocking)."""
        await self.client.run_until_disconnected()

    async def stop(self):
        """Stop the Telethon client."""
        await self.client.disconnect()
        logger.info("Telethon client stopped")

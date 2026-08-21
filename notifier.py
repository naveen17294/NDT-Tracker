import asyncio
import html
import logging
import time
from collections import OrderedDict

from config import NOTIFIER_CACHE_SIZE, NOTIFIER_DEDUP_WINDOW
from utils import format_price, format_time_ago, truncate

logger = logging.getLogger(__name__)


#
# Bot 1's alerts carry no buttons.
#
# They used to have a 👍/👎 row for rating the channel an alert came from. That
# gesture now lives on bot 2's mirror feed instead, where there is far more to judge:
# bot 1 only ever sends what you already asked for by name, so rating it says little,
# while the mirror shows everything and a 👎 there both mutes the product and counts
# against the channel. See mirror.py.
#
# bot.py deliberately still handles the old `fb_*` callbacks, because alerts already
# sitting in the chat keep their buttons forever and tapping one should not error.
#


class Notifier:
    """Format and send deal alert notifications."""

    def __init__(self, bot):
        self.bot = bot  # python-telegram-bot Bot instance
        # OrderedDict + hard cap: a burst of unique deals can no longer grow this
        # cache faster than the time-based prune shrinks it.
        self.sent_deals = OrderedDict()  # dedupe_key -> timestamp
        self.dedupe_window = NOTIFIER_DEDUP_WINDOW

    async def send_deal_alert(self, chat_id, deal_info):
        """
        Send a formatted deal alert to the user.

        Args:
            chat_id: Telegram chat ID to send to
            deal_info: dict with keys:
                - product_name: str
                - keyword: str (watchlist keyword that matched)
                - matched_term: str (actual term that matched)
                - confidence: str ('high', 'medium', 'low')
                - match_type: str ('exact', 'synonym', 'fuzzy')
                - match_source: str ('text', 'link_scrape')
                - price: float or None
                - original_price: float or None
                - discount: str or None
                - channel_name: str
                - message_link: str or None
                - deal_url: str or None (product link)
                - timestamp: float
        """
        # --- Deduplication Logic ---
        now = time.time()
        # Drop entries older than the dedupe window, then enforce the hard cap.
        expired = [k for k, v in self.sent_deals.items() if now - v >= self.dedupe_window]
        for k in expired:
            self.sent_deals.pop(k, None)
        while len(self.sent_deals) > NOTIFIER_CACHE_SIZE:
            self.sent_deals.popitem(last=False)

        deal_url = deal_info.get('deal_url')
        keyword = deal_info.get('keyword', '')
        price = deal_info.get('price')
        product_key = deal_info.get('product_key')

        # Create a unique footprint for this deal
        if product_key:
            # The product's own id — identical across channels and across affiliate
            # tags, so this catches the near-simultaneous repost that the raw URL
            # cannot. See product_key.py.
            dedupe_key = f"pk:{product_key}"
        elif deal_url:
            # A direct link is the next best fingerprint, though two channels sharing
            # one product usually rewrite it differently.
            dedupe_key = f"url:{deal_url}"
        elif price:
            # Otherwise, keyword + price is a strong indicator
            dedupe_key = f"kw:{keyword}_price:{price}"
        else:
            # Fallback to keyword + product name snippet
            prod_name = deal_info.get('product_name', '')[:20]
            dedupe_key = f"kw:{keyword}_prod:{prod_name}"

        if dedupe_key in self.sent_deals:
            logger.info(f"🚫 Skipped duplicate deal alert: {dedupe_key}")
            return False

        # Mark as sent
        self.sent_deals[dedupe_key] = now
        # ---------------------------

        try:
            message = self._format_alert(deal_info)
            await self.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=False,
            )
            logger.info(f"Deal alert sent: {deal_info.get('product_name', 'Unknown')}")
            return True
        except Exception as e:
            logger.error(f"Failed to send deal alert: {e}")
            return False

    def _format_alert(self, info):
        """Format deal info into a beautiful Telegram message using HTML to prevent parsing errors."""
        raw_product = truncate(info.get('product_name', 'Unknown Product'), 80)
        product = html.escape(raw_product)
        keyword = html.escape(info.get('keyword', '?'))
        matched_term = html.escape(info.get('matched_term', keyword))
        confidence = info.get('confidence', 'medium')
        match_source = info.get('match_source', 'text')
        channel = html.escape(info.get('channel_name', 'Unknown'))
        deal_url = info.get('deal_url', '')
        message_link = info.get('message_link', '')
        timestamp = info.get('timestamp', time.time())

        # Confidence emoji
        conf_emoji = {'high': '🟢', 'medium': '🟡', 'low': '🟠'}.get(confidence, '⚪')

        # Source indicator — shows which stage of the pipeline identified the product,
        # so it is obvious at a glance whether previews or the HTML scraper did the work.
        source_text = {
            'text': '📝 TEXT_PARSE',
            'native_preview': '🖼️ PREVIEW_NATIVE',
            'telegram_preview_api': '🛰️ PREVIEW_API',
            'link_scrape': '🕸️ LINK_SCRAPE',
        }.get(match_source, match_source.upper())

        # Build message
        lines = ['⚡ <b>N D T   T R A C K E R</b> ⚡\n']
        lines.append(f'💠 <b>{product}</b>')
        
        # Raw Text (Original Transmission) right after product name
        raw_text = info.get('raw_text')
        if raw_text:
            safe_raw = html.escape(raw_text[:800] + ('...' if len(raw_text) > 800 else ''))
            lines.append(f'<blockquote>{safe_raw}</blockquote>\n')
        else:
            lines.append('\n')

        # Price section
        price = info.get('price')
        original_price = info.get('original_price')
        discount = info.get('discount')

        if price:
            price_str = html.escape(format_price(price))
            if original_price and discount:
                orig_str = html.escape(format_price(original_price))
                disc_str = html.escape(discount)
                lines.append(f'💎 <b>{price_str}</b> (MRP {orig_str} — {disc_str})')
            elif original_price:
                orig_str = html.escape(format_price(original_price))
                lines.append(f'💎 <b>{price_str}</b> (MRP {orig_str})')
            else:
                lines.append(f'💎 <b>{price_str}</b>')
        else:
            lines.append('💎 <i>Price encrypted — check link</i>')

        lines.append(f'📡 Node: {channel}\n')

        # Links
        if deal_url:
            lines.append(f'🔗 <a href="{deal_url}">Access Payload</a>')
        if message_link:
            lines.append(f'💬 <a href="{message_link}">View Source</a>')

        # Time
        elapsed = time.time() - timestamp
        lines.append(f'\n⏱️ {format_time_ago(elapsed)}')

        # Match info
        lines.append(f'🎯 Target: "{keyword}" → "{matched_term}" {conf_emoji}')
        lines.append(f'🔍 Source: {source_text}')

        return '\n'.join(lines)

    async def send_batch_alerts(self, chat_id, deals):
        """Send multiple deal alerts (with slight delay to avoid spam)."""
        for deal in deals:
            await self.send_deal_alert(chat_id, deal)
            await asyncio.sleep(0.5)  # Small delay between messages

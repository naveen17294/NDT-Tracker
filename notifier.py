import logging
import time
from utils import format_price, format_time_ago, truncate

logger = logging.getLogger(__name__)


class Notifier:
    """Format and send deal alert notifications."""

    def __init__(self, bot):
        self.bot = bot  # python-telegram-bot Bot instance
        self.sent_deals = {}  # Cache of sent deals
        self.dedupe_window = 300  # 5 minutes in seconds

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
        # Clean up cache (remove items older than 1 hour)
        self.sent_deals = {k: v for k, v in self.sent_deals.items() if now - v < self.dedupe_window}

        deal_url = deal_info.get('deal_url')
        keyword = deal_info.get('keyword', '')
        price = deal_info.get('price')

        # Create a unique footprint for this deal
        if deal_url:
            # If we have a direct link, that's the best fingerprint
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
            return

        # Mark as sent
        self.sent_deals[dedupe_key] = now
        # ---------------------------

        try:
            import html
            message = self._format_alert(deal_info)
            await self.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='HTML',
                disable_web_page_preview=False,
            )
            logger.info(f"Deal alert sent: {deal_info.get('product_name', 'Unknown')}")
        except Exception as e:
            logger.error(f"Failed to send deal alert: {e}")

    def _format_alert(self, info):
        """Format deal info into a beautiful Telegram message using HTML to prevent parsing errors."""
        import html
        
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

        # Source indicator
        source_text = 'TEXT_PARSE' if match_source == 'text' else 'LINK_TRACE'

        # Build message
        lines = ['⚡ <b>N E O N   D R O P</b> ⚡\n']
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

        return '\n'.join(lines)

    async def send_batch_alerts(self, chat_id, deals):
        """Send multiple deal alerts (with slight delay to avoid spam)."""
        import asyncio
        for deal in deals:
            await self.send_deal_alert(chat_id, deal)
            await asyncio.sleep(0.5)  # Small delay between messages

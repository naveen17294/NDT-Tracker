import asyncio
import functools
import html
import logging
import re
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from database import Database
from keyword_matcher import KeywordMatcher, parse_exclusions
from channel_monitor import ChannelMonitor
from config import (
    BOT_TOKEN,
    CHANNEL_NAME_FILTERS,
    CHANNEL_QUIET_DAYS,
    CHANNEL_REPORT_ENABLED,
    CHANNEL_REPORT_HOURS,
    CHANNEL_REPORT_MAX_ROWS,
    DEAL_RETENTION_DAYS,
    KEEPALIVE_INTERVAL,
    KEEPALIVE_URL,
    MAINTENANCE_INTERVAL_HOURS,
    OWNER_ID,
    PORT,
    WATCHDOG_INTERVAL,
)
from utils import channel_display_name, channel_matches, format_time_ago

# Where the active /searchchannel query is parked, so pagination and toggling stay
# inside the search results. It cannot ride in callback_data — Telegram caps that at
# 64 bytes and the query is arbitrary user text.
_SEARCH_KEY = 'channel_search'

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Silence noisy third-party logs to keep the console clean and helpful
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

db = Database()
matcher = KeywordMatcher()
monitor = None  # Global reference to ChannelMonitor

# Background tasks + web server handles, kept so shutdown can cancel/close them
# instead of leaking a task and its captured frames on every restart.
_background_tasks = []
_web_runner = None
_started_at = time.time()
# Set in post_init. The scheduled report has no update/context to reply to, so it
# needs the Application to reach the owner's chat.
_application = None


def owner_only(func):
    """Decorator to restrict commands to bot owner."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        user_id = user.id if user else 0
        if OWNER_ID != 0 and user_id != OWNER_ID:
            # update.message is None for callback queries — the old version raised
            # AttributeError here instead of rejecting the caller.
            if update.callback_query:
                await update.callback_query.answer("❌ Unauthorized access.", show_alert=True)
            elif update.message:
                await update.message.reply_text("❌ Unauthorized access.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def _invalidate_watchlist():
    """The monitor caches the watchlist in memory — drop it after any edit."""
    if monitor:
        monitor.invalidate_watchlist_cache()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - Welcome & Intro."""
    welcome = """
╔════════════════════════════╗
      ⚡ **N D T   N E X U S** ⚡
╚════════════════════════════╝

💠 **SYSTEM ONLINE** 💠
Your Personal Deal Tracker is now active.
Monitoring encrypted channels 24/7 for zero-day drops and flash deals.

**Terminal Commands:**
💠 /watch `<keyword>` — Track a new target
💠 /unwatch `<keyword>` — Drop a target
💠 /updatesynonyms `<keyword> | <synonyms>` — Update synonyms for a target
💠 /exclude `<keyword> | <terms>` — Block terms for a target
💠 /watchlist — View active tracking matrix
💠 /testmatch `<text>` — Dry-run a message through the matcher
💠 /synonyms `<keyword>` — Show what a keyword actually matches
💠 /channels — Uplink to deal channels
💠 /searchchannel `<text>` — Search every joined channel by name
💠 /addchannel `<link>` — Manually uplink to a new channel
💠 /untrackchannel `<name>` — Instantly untrack a specific channel
💠 /channelreport — Which nodes are earning their place
💠 /deals — View recent drops
💠 /pause or /resume — Suspend/Resume scans
💠 /stats — System diagnostics
💠 /help — Access manual

*Awaiting input...* 📟
    """
    await update.message.reply_text(welcome, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command."""
    help_text = """
📖 **N D T   M A N U A L**

**1️⃣ Add Targets:**
/watch `fridge` — Tracks "fridge", "refrigerator", "double door", etc.
/watch `laptop | gaming, macbook` — Adds custom parameters
/watch `shoes -kids -women` — Blocks terms while adding
/updatesynonyms `laptop | gaming, macbook, asus` — Updates parameters for an existing target
/exclude `shoes | kids, women, socks` — Blocks terms on an existing target.
A message containing any blocked term never alerts for that keyword, even if it
matches otherwise. `/exclude shoes |` clears them.

**Rate your alerts:** every alert carries 👍/👎. One tap tells NDT whether that
channel is worth keeping — it feeds /channelreport, which arrives every 8 hours.
Nothing about the alert itself is stored; only per-channel tallies.

**2️⃣ Network Uplinks:**
Use /channels to view available nodes and toggle monitoring. It lists only channels
with `deal` or `sale` in the name, plus everything you are already tracking — a
joined account has hundreds of channels and the rest are noise.
Use /searchchannel `<text>` to look through *all* joined channels by name.
Use /addchannel `<username or link>` to manually join and track a new channel.

**3️⃣ Scan Algorithms:**
💠 **Text Parse:** Deep scans message packets for target matches.
💠 **Link Trace:** Extracts metadata from shortened URLs (`amzn.to`, etc.) when text is masked.
💠 **Price Rip:** Extracts numeric values directly from payload.

**4️⃣ System Control:**
💠 /pause — Suspend all active scans
💠 /resume — Re-engage scanning

*System ready. Input target.* ⚡
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')


# Matches a standalone -term, e.g. the "-kids" in "/watch shoes -kids". The
# leading (^|\s) is what keeps hyphenated words intact: "t-shirt" and "non-stick"
# have no whitespace before the hyphen, so they are never read as exclusions.
_NEGATIVE_TERM = re.compile(r'(?:^|\s)-(\S+)')


def parse_negatives(text):
    """Split '-term' exclusions out of a command argument. Returns (rest, exclusions)."""
    negatives = [m.group(1).strip().lower() for m in _NEGATIVE_TERM.finditer(text)]
    rest = _NEGATIVE_TERM.sub(' ', text).strip()
    return rest, ', '.join(t for t in negatives if t)


@owner_only
async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add keyword to watchlist."""
    if not context.args:
        await update.message.reply_text(
            "❌ Please specify a keyword.\n"
            "Example: `/watch fridge`, `/watch laptop | gaming, macbook`,\n"
            "or `/watch shoes -kids -women` to block terms.",
            parse_mode='Markdown'
        )
        return

    full_arg = ' '.join(context.args)

    # Negative keywords first, so a '-term' can sit anywhere in the argument.
    full_arg, exclusions = parse_negatives(full_arg)

    # Check for custom synonyms (separated by |)
    if '|' in full_arg:
        parts = full_arg.split('|', 1)
        keyword = parts[0].strip()
        custom_synonyms = parts[1].strip()
    else:
        keyword = full_arg.strip()
        custom_synonyms = ''

    if not keyword:
        await update.message.reply_text(
            "❌ That's only exclusions — I need a keyword too.\n"
            "Example: `/watch shoes -kids`",
            parse_mode='Markdown')
        return

    success = await db.add_keyword(keyword, custom_synonyms, exclusions)

    if success:
        _invalidate_watchlist()
        syns = matcher.get_display_synonyms(keyword, custom_synonyms)
        syn_text = f"\n💡 *Synonyms included:* {', '.join(syns)}" if syns else ""
        excl_text = f"\n🚫 *Blocked terms:* {exclusions}" if exclusions else ""
        await update.message.reply_text(
            f"✅ *Added to watchlist:* `{keyword}`{syn_text}{excl_text}\n\n"
            f"NDT will notify you when deals appear in your monitored channels!",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"⚠️ `{keyword}` is already on your watchlist.\n\nUse `/updatesynonyms {keyword} | new, synonyms` to update it.", parse_mode='Markdown')


@owner_only
async def exclude_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the negative keywords for an existing watchlist entry."""
    full_arg = ' '.join(context.args) if context.args else ''

    if '|' not in full_arg:
        await update.message.reply_text(
            "❌ Format: `/exclude <keyword> | <terms>`\n"
            "Example: `/exclude shoes | kids, women, socks`\n"
            "Clear them with `/exclude shoes |`",
            parse_mode='Markdown')
        return

    keyword, exclusions = (part.strip() for part in full_arg.split('|', 1))
    if not keyword:
        await update.message.reply_text("❌ Which keyword?", parse_mode='Markdown')
        return

    if not await db.update_exclusions(keyword, exclusions):
        await update.message.reply_text(
            f"❌ `{keyword}` is not on your watchlist. Add it with `/watch` first.",
            parse_mode='Markdown')
        return

    _invalidate_watchlist()
    if exclusions:
        await update.message.reply_text(
            f"🚫 *Blocked for* `{keyword}`*:* {exclusions}\n\n"
            f"A message containing any of those will no longer alert for this keyword.\n"
            f"Check it with `/testmatch <some message>`.",
            parse_mode='Markdown')
    else:
        await update.message.reply_text(
            f"✅ Cleared all blocked terms for `{keyword}`.", parse_mode='Markdown')

@owner_only
async def update_synonyms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update custom synonyms for an existing keyword."""
    if not context.args:
        await update.message.reply_text(
            "❌ Please specify a keyword and new synonyms.\n"
            "Example: `/updatesynonyms laptop | gaming, macbook, asus`",
            parse_mode='Markdown'
        )
        return

    full_arg = ' '.join(context.args)
    if '|' not in full_arg:
        await update.message.reply_text("❌ Please format as: `/updatesynonyms keyword | synonym1, synonym2`", parse_mode='Markdown')
        return
        
    parts = full_arg.split('|', 1)
    keyword = parts[0].strip()
    custom_synonyms = parts[1].strip()

    success = await db.update_synonyms(keyword, custom_synonyms)

    if success:
        _invalidate_watchlist()
        syns = matcher.get_display_synonyms(keyword, custom_synonyms)
        syn_text = f"\n💡 *New Synonyms:* {', '.join(syns)}" if syns else ""
        await update.message.reply_text(
            f"✅ *Updated synonyms for:* `{keyword}`{syn_text}",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"❌ `{keyword}` was not found in your watchlist. Add it with `/watch` first.", parse_mode='Markdown')

@owner_only
async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove keyword from watchlist."""
    if not context.args:
        await update.message.reply_text("❌ Please specify a keyword to remove. Example: `/unwatch fridge`", parse_mode='Markdown')
        return

    keyword = ' '.join(context.args).strip()
    success = await db.remove_keyword(keyword)

    if success:
        _invalidate_watchlist()
        await update.message.reply_text(f"🗑️ Removed `{keyword}` from watchlist.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ `{keyword}` was not found in your watchlist.", parse_mode='Markdown')


@owner_only
async def testmatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dry-run the matcher against a message, without waiting for a real deal."""
    if not context.args:
        await update.message.reply_text(
            "❌ Paste a message to test.\n"
            "Example: `/testmatch Nike Running Shoes Rs 1999`",
            parse_mode='Markdown'
        )
        return

    text = ' '.join(context.args)
    watchlist = await db.get_all_keywords()
    report, result = matcher.explain(text, watchlist)

    verdict = "✅ WOULD ALERT" if result else "🚫 WOULD NOT ALERT"
    await update.message.reply_text(
        f"<b>{verdict}</b>\n\n<pre>{html.escape(report)}</pre>",
        parse_mode='HTML'
    )


@owner_only
async def synonyms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show exactly what terms a keyword expands to."""
    if not context.args:
        await update.message.reply_text(
            "❌ Specify a keyword.\nExample: `/synonyms shoes`",
            parse_mode='Markdown'
        )
        return

    keyword = ' '.join(context.args).strip()
    rows = await db.get_watchlist()
    custom = ''
    excluded = ''
    for row in rows:
        if row['keyword'] == keyword.lower():
            custom = row.get('custom_synonyms', '') or ''
            excluded = row.get('exclusions', '') or ''
            break

    terms = matcher.get_synonyms(keyword, custom)
    listed = '\n'.join(f"• {t}" for t in terms)
    blocked = ''
    if excluded:
        blocked_list = '\n'.join(f"• {t}" for t in parse_exclusions(excluded))
        blocked = (f"\n\n🚫 Blocked — a message containing any of these will not "
                   f"alert for <b>{html.escape(keyword)}</b>:\n"
                   f"<pre>{html.escape(blocked_list)}</pre>")
    await update.message.reply_text(
        f"<b>{html.escape(keyword)}</b> matches {len(terms)} term(s):\n\n"
        f"<pre>{html.escape(listed)}</pre>{blocked}",
        parse_mode='HTML'
    )


@owner_only
async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display and manage watchlist."""
    items = await db.get_watchlist()

    if not items:
        await update.message.reply_text(
            "📋 *Your Watchlist is empty!*\n\nAdd products using `/watch <keyword>`",
            parse_mode='Markdown'
        )
        return

    message = "📋 *YOUR WATCHLIST*\n\n"
    keyboard = []

    for item in items:
        kw = item['keyword']
        syns = matcher.get_display_synonyms(kw, item.get('custom_synonyms', ''))
        syn_preview = f" ({', '.join(syns[:3])})" if syns else ""
        excluded = item.get('exclusions') or ''
        excl_preview = f"\n   🚫 {excluded}" if excluded else ""
        message += f"• `{kw}`{syn_preview}{excl_preview}\n"

        # Add delete button for each keyword
        keyboard.append([InlineKeyboardButton(f"❌ Delete '{kw}'", callback_data=f"del_kw_{kw}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')


def _visible_channels(channels_list, active_ids, search):
    """
    Narrow the joined-channel list down to what is worth showing.

    Without this, /channels listed every broadcast channel the account has ever
    joined — hundreds of them, deal channels buried among news and memes.

    Default view: names containing a CHANNEL_NAME_FILTERS term ('deal', 'sale'),
    PLUS everything currently tracked regardless of its name. That second half is
    not optional — a channel added by /addchannel usually will not match the filter,
    and if the filter hid it you could never toggle it back off.

    Search view: the query alone. Tracked-but-unmatched channels are deliberately
    left out here; they are always one /channels away.
    """
    if search:
        terms = [search.strip().lower()]
        return [(cid, info) for cid, info in channels_list if channel_matches(info, terms)]
    return [
        (cid, info) for cid, info in channels_list
        if cid in active_ids or channel_matches(info, CHANNEL_NAME_FILTERS)
    ]


async def _get_channels_page(page=0, search=None):
    joined = await monitor.get_joined_channels()
    db_channels = await db.get_all_channels()
    active_ids = {c['channel_id'] for c in db_channels if c['active'] == 1}
    known_ids = {c['channel_id'] for c in db_channels}

    if not joined and not db_channels:
        return "❌ No channels found. Join deal channels on Telegram first!", None

    # Merge joined channels with database channels
    all_channels = {}
    for ch in joined:
        all_channels[ch['channel_id']] = ch
    for ch in db_channels:
        if ch['channel_id'] not in all_channels:
            all_channels[ch['channel_id']] = ch

    # Sort channels alphabetically by name
    channels_list = list(all_channels.items())
    channels_list.sort(key=lambda x: channel_display_name(x[1]).lower())

    visible = _visible_channels(channels_list, active_ids, search)

    if not visible:
        if search:
            return (
                f"🔍 No joined channel matches `{search}`.\n\n"
                "Try a shorter word, or join it first with "
                "/addchannel `<username or link>`.",
                None,
            )
        shown = ', '.join(CHANNEL_NAME_FILTERS)
        return (
            f"📢 *CHANNELS*\n\n"
            f"No joined channel has `{shown}` in its name.\n\n"
            "🔍 /searchchannel `<text>` — look through every joined channel\n"
            "➕ /addchannel `<username or link>` — join and track a new one",
            None,
        )

    ITEMS_PER_PAGE = 30
    total_pages = max(1, (len(visible) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_items = visible[start_idx:end_idx]

    message = f"📢 *MONITORED CHANNELS*\n\n"
    active_names = [
        channel_display_name(ch_info)
        for ch_id, ch_info in channels_list if ch_id in active_ids
    ]

    if active_names:
        message += "🟢 **Currently Tracking:**\n"
        for name in active_names[:40]:  # Limit to 40 in text to avoid message length limits
            message += f"• {name[:50]}\n"
        if len(active_names) > 40:
            message += f"...and {len(active_names) - 40} more\n"
    else:
        message += "🔴 Not tracking any channels yet.\n"

    if search:
        message += f"\n🔍 *Search:* `{search}` — {len(visible)} match(es)\n"
    else:
        message += (
            f"\n🔎 *Showing channels named* `{'`, `'.join(CHANNEL_NAME_FILTERS)}` "
            f"*— {len(visible)} of {len(channels_list)} joined.*\n"
            "Use /searchchannel `<text>` for the rest, or /addchannel `<link>` for a new one.\n"
        )

    message += f"\n*Toggle channels below (Page {page+1}/{total_pages}):*\n"

    keyboard = []

    for ch_id, ch_info in current_items:
        name = channel_display_name(ch_info)
        is_active = ch_id in active_ids
        status_emoji = "🟢" if is_active else "🔴"

        # Button to toggle (limit callback data size and use simple name)
        keyboard.append([InlineKeyboardButton(
            f"{status_emoji} {name[:25]}",
            callback_data=f"toggle_ch_{ch_id}_{page}"
        )])

        # Auto-save channel to DB if new
        if ch_id not in known_ids:
            await db.add_channel(ch_id, name, ch_info.get('channel_username', ''))

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_ch_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_ch_{page+1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    if search:
        keyboard.append([InlineKeyboardButton(
            "❎ Clear search", callback_data="ch_clear_search")])

    # Untrack All button
    if active_ids:
        keyboard.append([InlineKeyboardButton("🛑 Untrack All Channels", callback_data="untrack_all_channels")])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return message, reply_markup

@owner_only
async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show channel selection menu, filtered to deal/sale channels."""
    if not monitor:
        await update.message.reply_text("⏳ Channel monitor loading, please try again in a moment.")
        return

    context.user_data.pop(_SEARCH_KEY, None)  # /channels always leaves search mode

    try:
        message, reply_markup = await _get_channels_page(page=0)
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error listing channels: {e}")
        await update.message.reply_text(f"❌ Error listing channels: {str(e)}")


@owner_only
async def search_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search every joined channel by name, ignoring the deal/sale filter."""
    if not monitor:
        await update.message.reply_text("⏳ Channel monitor loading, please try again in a moment.")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ What should I search for?\nExample: `/searchchannel loot`",
            parse_mode='Markdown')
        return

    query = ' '.join(context.args).strip()
    context.user_data[_SEARCH_KEY] = query

    try:
        message, reply_markup = await _get_channels_page(page=0, search=query)
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error searching channels: {e}")
        await update.message.reply_text(f"❌ Error searching channels: {str(e)}")


@owner_only
async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a channel by username or link."""
    if not monitor:
        await update.message.reply_text("⏳ Channel monitor loading, please try again in a moment.")
        return

    if not context.args:
        await update.message.reply_text("❌ Please specify a channel username or link.\nExample: `/addchannel @username` or `/addchannel https://t.me/username`", parse_mode='Markdown')
        return

    identifier = context.args[0]
    await update.message.reply_text(f"⏳ Attempting to join and resolve `{identifier}`...", parse_mode='Markdown')

    try:
        channel_info = await monitor.resolve_and_join_channel(identifier)
        if channel_info:
            await db.add_channel(
                channel_info['channel_id'],
                channel_info['channel_name'],
                channel_info['channel_username']
            )
            # Make it active by default when manually added
            await db.toggle_channel(channel_info['channel_id'])
            
            await update.message.reply_text(f"✅ Successfully joined and started tracking **{channel_info['channel_name']}**!", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Could not resolve this channel. Make sure it's a public channel or a valid invite link.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to join channel: {str(e)}")


@owner_only
async def untrack_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Untrack a specific channel by name or username."""
    if not context.args:
        await update.message.reply_text("❌ Please specify a channel name or username.\nExample: `/untrackchannel @dealsbox` or `/untrackchannel BIG DIWALI SALE`", parse_mode='Markdown')
        return

    identifier = ' '.join(context.args)
    success = await db.untrack_specific_channel(identifier)
    
    if success:
        if monitor:
            await monitor.refresh_channels()
        await update.message.reply_text(f"🛑 Successfully untracked channel matching: `{identifier}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Could not find an active channel matching `{identifier}` in your list.", parse_mode='Markdown')

@owner_only
async def deals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent matched deals."""
    deals = await db.get_recent_deals(hours=24)

    if not deals:
        await update.message.reply_text("📥 No matched deals in the last 24 hours.", parse_mode='Markdown')
        return

    message = f"🔥 *RECENT DEALS (Last 24h — {len(deals)} found)*\n\n"

    for deal in deals[:10]:  # Show top 10
        product = deal.get('product_name') or deal.get('keyword_matched')
        price = deal.get('price')
        price_str = f" • ₹{price}" if price else ""
        channel = deal.get('channel_name', '')
        # matched_date is a time.time() epoch value. The old code subtracted it from
        # loop.time(), a monotonic clock with an unrelated origin, so this was always
        # a huge negative number.
        time_ago = format_time_ago(max(0, time.time() - deal.get('matched_date', 0)))

        link = deal.get('message_link')
        link_str = f" [Link]({link})" if link else ""

        message += f"• *{product[:40]}*{price_str} ({channel}) — {time_ago}{link_str}\n"

    await update.message.reply_text(message, parse_mode='Markdown', disable_web_page_preview=True)


@owner_only
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause channel monitoring."""
    if monitor:
        await monitor.pause()
        await update.message.reply_text("⏸️ Monitoring **paused**. Use `/resume` to start again.", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Monitor is not running.")


@owner_only
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume channel monitoring."""
    if monitor:
        await monitor.resume()
        await update.message.reply_text("▶️ Monitoring **resumed**!", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Monitor is not running.")


@owner_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status and statistics."""
    stats = await db.get_stats()
    is_paused = monitor.paused if monitor else True

    status_str = "⏸️ Paused" if is_paused else "🟢 Active & Monitoring"
    link_str = "🟢 Connected" if (monitor and monitor.is_connected()) else "🔴 Disconnected"
    uptime = format_time_ago(time.time() - _started_at)

    msg = f"""
📊 **NDT STATUS & STATS**

**Status:** {status_str}
🔌 **Telethon Uplink:** {link_str}
⏱️ **Uptime:** {uptime.replace(' ago', '')}
🎯 **Tracked Keywords:** {stats['watchlist_count']}
📢 **Monitored Channels:** {stats['active_channels']} / {stats['total_channels']}
🔥 **Deals Found (24h):** {stats['deals_24h']}
📦 **Total Deals All-Time:** {stats['total_deals']}
    """
    await update.message.reply_text(msg, parse_mode='Markdown')


@owner_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data

    # Delete keyword callback
    if data.startswith('del_kw_'):
        keyword = data.replace('del_kw_', '')
        success = await db.remove_keyword(keyword)
        if success:
            _invalidate_watchlist()
            await query.edit_message_text(f"🗑️ Removed `{keyword}` from watchlist.", parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ Error removing `{keyword}`.")

    # 👍 / 👎 on a deal alert
    elif data.startswith('fb_u_') or data.startswith('fb_d_'):
        _, verdict, raw_id = data.split('_', 2)
        is_good = verdict == 'u'
        try:
            await db.record_feedback(int(raw_id), is_good)
        except Exception as e:
            logger.error(f"Could not record feedback: {e}")
            await query.answer("❌ Couldn't save that.")
            return

        # Swap the buttons for a static receipt. This is what stops double-voting:
        # the message's own keyboard is the only record that a vote happened, since
        # no per-alert row exists to check against.
        label = '👍 Rated good' if is_good else '👎 Rated junk'
        try:
            await query.edit_message_reply_markup(
                InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data='fb_done')]]))
        except Exception as e:
            # Telegram refuses edits to messages older than 48h. The vote is already
            # counted, so this is cosmetic.
            logger.debug(f"Could not update feedback buttons: {e}")
        await query.answer('Noted — it shapes /channelreport.')

    # The receipt button left behind after a vote.
    elif data == 'fb_done':
        await query.answer('Already rated.')

    # Toggle channel callback
    elif data.startswith('toggle_ch_'):
        parts = data.split('_')
        ch_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        await db.toggle_channel(ch_id)

        if monitor:
            await monitor.refresh_channels()

        try:
            # Re-render the view the button was pressed in, search included.
            msg, markup = await _get_channels_page(page, context.user_data.get(_SEARCH_KEY))
            await query.edit_message_text(msg, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error updating channels message: {e}")
            await query.edit_message_text("❌ Error updating channels.")

    # Untrack all channels callback
    elif data == 'untrack_all_channels':
        await db.untrack_all_channels()
        if monitor:
            await monitor.refresh_channels()

        try:
            msg, markup = await _get_channels_page(0, context.user_data.get(_SEARCH_KEY))
            await query.edit_message_text(msg, reply_markup=markup, parse_mode='Markdown')
            await query.answer("🛑 Untracked all channels!")
        except Exception as e:
            logger.error(f"Error untracking all channels: {e}")
            await query.edit_message_text("❌ Error untracking channels.")

    # Leave search mode, back to the deal/sale list
    elif data == 'ch_clear_search':
        context.user_data.pop(_SEARCH_KEY, None)
        try:
            msg, markup = await _get_channels_page(0)
            await query.edit_message_text(msg, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error clearing channel search: {e}")
            await query.edit_message_text("❌ Error clearing search.")

    # Pagination callback
    elif data.startswith('page_ch_'):
        page = int(data.replace('page_ch_', ''))
        try:
            msg, markup = await _get_channels_page(page, context.user_data.get(_SEARCH_KEY))
            await query.edit_message_text(msg, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error paging channels: {e}")
            await query.edit_message_text("❌ Error changing pages.")


def _channel_verdict(row, now):
    """
    One-word judgement on a channel, from its counters alone.

    Ordered by what you'd act on first: something you actively dislike, then
    something silent, then something good.
    """
    down, up = row['down'], row['up']
    quiet_for = now - row['last_alert_at'] if row['last_alert_at'] else None

    if down >= 3 and down > up * 2:
        return '👎 mostly junk — consider untracking'
    if row['alerts'] == 0:
        return '💤 never alerted'
    if quiet_for is not None and quiet_for > CHANNEL_QUIET_DAYS * 86400:
        return f'💤 quiet {int(quiet_for // 86400)}d'
    if up >= 3 and up > down * 2:
        return '⭐ good'
    return ''


def build_channel_report(rows, now=None):
    """
    Render the channel quality digest.

    Everything here comes from per-channel counters, so the report costs one query
    and does not depend on any alert being kept around. `new_*` are deltas since the
    previous report; the lifetime totals sit beside them because a single 8-hour
    window is too small to judge a channel on.
    """
    now = time.time() if now is None else now

    if not rows:
        return ("📊 <b>CHANNEL REPORT</b>\n\nNo channels are being tracked. "
                "Run /channels to pick some.")

    total_new = sum(r['new_alerts'] for r in rows)
    lines = [
        '📊 <b>CHANNEL REPORT</b>',
        f'{len(rows)} tracked · {total_new} alert(s) since the last report\n',
    ]

    shown = rows[:CHANNEL_REPORT_MAX_ROWS]
    for row in shown:
        name = html.escape(
            (row['channel_name'] or row['channel_username'] or
             f"Channel {row['channel_id']}")[:38])
        delta = row['new_alerts']
        delta_str = f'+{delta}' if delta else '—'
        rating = ''
        if row['up'] or row['down']:
            rating = f" · 👍{row['up']} 👎{row['down']}"
        verdict = _channel_verdict(row, now)
        verdict_str = f'\n   <i>{verdict}</i>' if verdict else ''
        lines.append(
            f"• <b>{name}</b>\n"
            f"   {delta_str} new · {row['alerts']} total{rating}{verdict_str}")

    if len(rows) > len(shown):
        lines.append(f"\n…and {len(rows) - len(shown)} more tracked channel(s).")

    rated = sum(r['up'] + r['down'] for r in rows)
    if rated == 0:
        lines.append(
            "\n💡 Rate alerts with 👍/👎 and this report can tell you which "
            "channels are worth keeping.")

    return '\n'.join(lines)


async def _send_channel_report(reason='scheduled'):
    """Build, send, and then snapshot the counters so the next delta is fresh."""
    rows = await db.get_channel_report()
    message = build_channel_report(rows)
    await _application.bot.send_message(
        chat_id=OWNER_ID, text=message, parse_mode='HTML',
        disable_web_page_preview=True)
    # Snapshot only AFTER a successful send. If the send fails, the next report
    # still covers this window rather than silently swallowing it.
    await db.snapshot_channel_report()
    logger.info(f"📊 Channel report sent ({reason}, {len(rows)} channels)")


@owner_only
async def channel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the channel quality report on demand."""
    rows = await db.get_channel_report()
    await update.message.reply_text(
        build_channel_report(rows), parse_mode='HTML',
        disable_web_page_preview=True)


async def _start_web_server():
    """
    Bind the HTTP port Render expects.

    This MUST happen before anything slow. Previously it ran after
    `await monitor.start()`, which does Telethon login plus a full dialog sync — on a
    cold container that easily outruns Render's port-detection window, so Render
    concluded the service never bound a port and shut it down. That is the "deployed,
    then stopped after some time" symptom.
    """
    global _web_runner

    if not PORT:
        logger.info("No PORT set — skipping web server (worker mode).")
        return

    from aiohttp import web

    async def health(request):
        connected = bool(monitor and monitor.is_connected())
        return web.json_response({
            'status': 'ok',
            'telethon_connected': connected,
            'monitoring': bool(monitor and not monitor.paused),
            'channels': len(monitor._monitored_channel_ids) if monitor else 0,
            'uptime_seconds': int(time.time() - _started_at),
        })

    async def root(request):
        return web.Response(text="NDT Tracker is running 24/7!")

    web_app = web.Application()
    web_app.router.add_get('/', root)
    web_app.router.add_get('/health', health)

    _web_runner = web.AppRunner(web_app)
    await _web_runner.setup()
    site = web.TCPSite(_web_runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Web server listening on port {PORT} (/ and /health)")


async def _keepalive_loop():
    """
    Ping our own public URL so a free-tier host doesn't idle us out.

    Render spins a free web service down after ~15 minutes with no inbound request.
    Binding the port is not enough — traffic has to actually arrive.
    """
    if not KEEPALIVE_URL:
        logger.info("No KEEPALIVE_URL/RENDER_EXTERNAL_URL — self-ping disabled.")
        return

    import aiohttp

    url = KEEPALIVE_URL.rstrip('/') + '/health'
    timeout = aiohttp.ClientTimeout(total=30)
    # One session for the life of the loop rather than one per ping.
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                async with session.get(url) as resp:
                    logger.debug(f"Keep-alive ping {url} -> {resp.status}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Keep-alive ping failed: {e}")


async def _watchdog_loop():
    """Reconnect Telethon if its connection drops."""
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        try:
            if monitor and not monitor.is_connected():
                await monitor.ensure_connected()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Watchdog error: {e}")


async def _maintenance_loop():
    """
    Prune old deals on a timer.

    cleanup_old_deals() has always existed but nothing ever called it, so
    matched_deals grew for the entire life of the deployment.
    """
    interval = MAINTENANCE_INTERVAL_HOURS * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            deleted = await db.cleanup_old_deals(DEAL_RETENTION_DAYS)
            if deleted:
                logger.info(f"🧹 Maintenance: pruned {deleted} deals older than {DEAL_RETENTION_DAYS}d")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Maintenance error: {e}")


async def _seconds_until_next_report(interval):
    """
    How long to wait, measured from the LAST REPORT rather than from process start.

    A plain `sleep(interval)` loop would be reset by every restart, and this runs on a
    free tier that redeploys and recycles containers — an 8-hour timer that restarts
    every few hours never fires. `last_report_at` lives in the database, so the
    schedule survives the process. A failed send leaves it unchanged, which makes the
    next wait short and retries rather than skipping the window.
    """
    try:
        rows = await db.get_channel_report()
    except Exception as e:
        logger.error(f"Could not read report schedule: {e}")
        return interval
    last = max((row['last_report_at'] or 0 for row in rows), default=0)
    if not last:
        return interval  # never reported — give it a full window of data first
    return max(300, interval - (time.time() - last))


async def _channel_report_loop():
    """Send the channel quality digest every CHANNEL_REPORT_HOURS."""
    if not CHANNEL_REPORT_ENABLED:
        logger.info("Channel report disabled (CHANNEL_REPORT_ENABLED=false).")
        return

    interval = max(1, CHANNEL_REPORT_HOURS) * 3600
    while True:
        await asyncio.sleep(await _seconds_until_next_report(interval))
        try:
            await _send_channel_report()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Channel report failed: {e}")


async def post_init(application):
    """Start the Telethon monitor when the bot starts."""
    global monitor, _application

    _application = application

    # ── 1. Bind the port FIRST so the host's health check succeeds immediately ──
    await _start_web_server()

    # ── 2. Then the slow work ──
    # Set bot commands menu
    await application.bot.set_my_commands([
        BotCommand("watch", "Add product to watchlist"),
        BotCommand("unwatch", "Remove product from watchlist"),
        BotCommand("updatesynonyms", "Update custom synonyms for a keyword"),
        BotCommand("exclude", "Block terms for a keyword"),
        BotCommand("watchlist", "View & manage tracked products"),
        BotCommand("testmatch", "Test if a message would trigger an alert"),
        BotCommand("synonyms", "Show what terms a keyword matches"),
        BotCommand("channels", "Select channels to monitor"),
        BotCommand("searchchannel", "Search all joined channels by name"),
        BotCommand("addchannel", "Join & track a new channel"),
        BotCommand("untrackchannel", "Untrack a specific channel"),
        BotCommand("channelreport", "Which channels are worth keeping"),
        BotCommand("deals", "View recent matched deals"),
        BotCommand("pause", "Pause monitoring"),
        BotCommand("resume", "Resume monitoring"),
        BotCommand("stats", "Check bot status"),
        BotCommand("help", "How to use"),
    ])
    
    try:
        monitor = ChannelMonitor(application.bot)
        await monitor.start()
    except Exception as e:
        # Don't leave the process "up but blind" — the watchdog will keep retrying,
        # and /health + /stats now report the uplink as down so it's visible.
        logger.exception(f"Failed to start ChannelMonitor: {e}")

    # ── 3. Background loops ──
    _background_tasks.append(asyncio.create_task(_watchdog_loop(), name='watchdog'))
    _background_tasks.append(asyncio.create_task(_maintenance_loop(), name='maintenance'))
    _background_tasks.append(asyncio.create_task(_keepalive_loop(), name='keepalive'))
    _background_tasks.append(asyncio.create_task(_channel_report_loop(), name='channel-report'))

    # Prune once at boot rather than waiting a full interval on a fresh container.
    try:
        deleted = await db.cleanup_old_deals(DEAL_RETENTION_DAYS)
        if deleted:
            logger.info(f"🧹 Startup cleanup: pruned {deleted} old deals")
    except Exception as e:
        logger.error(f"Startup cleanup failed: {e}")


async def post_shutdown(application):
    """Cancel background tasks and release sockets, threads and the DB handle."""
    for task in _background_tasks:
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()

    if monitor:
        try:
            await monitor.stop()
        except Exception as e:
            logger.debug(f"Error stopping monitor: {e}")

    if _web_runner is not None:
        try:
            await _web_runner.cleanup()
        except Exception as e:
            logger.debug(f"Error stopping web server: {e}")

    try:
        await db.close()
    except Exception as e:
        logger.debug(f"Error closing database: {e}")

    logger.info("👋 NDT shut down cleanly")


def main():
    """Start NDT Bot."""
    # DO NOT REMOVE — this looks like dead code and is not.
    #
    # python-telegram-bot 21.x calls asyncio.get_event_loop() inside run_polling().
    # Up to Python 3.13 that implicitly created a loop when none was set. Python 3.14
    # made it raise instead:
    #     RuntimeError: There is no current event loop in thread 'MainThread'.
    # so the process dies before post_init ever runs, which means the port is never
    # bound and the host reports a failed deploy. Install a loop up front.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is missing!")
        return

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("unwatch", unwatch_command))
    application.add_handler(CommandHandler("updatesynonyms", update_synonyms_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("testmatch", testmatch_command))
    application.add_handler(CommandHandler("synonyms", synonyms_command))
    application.add_handler(CommandHandler("channels", channels_command))
    application.add_handler(CommandHandler("exclude", exclude_command))
    application.add_handler(CommandHandler("channelreport", channel_report_command))
    application.add_handler(CommandHandler("searchchannel", search_channel_command))
    application.add_handler(CommandHandler("addchannel", add_channel_command))
    application.add_handler(CommandHandler("untrackchannel", untrack_channel_command))
    application.add_handler(CommandHandler("deals", deals_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🚀 NDT Telegram Bot initializing...")

    # Start bot polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

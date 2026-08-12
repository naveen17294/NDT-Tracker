import logging
import asyncio
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from database import Database
from keyword_matcher import KeywordMatcher
from channel_monitor import ChannelMonitor
from config import BOT_TOKEN, OWNER_ID
from utils import format_time_ago, format_price

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Silence noisy third-party logs to keep the console clean and helpful
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

db = Database()
matcher = KeywordMatcher()
monitor = None  # Global reference to ChannelMonitor


def owner_only(func):
    """Decorator to restrict commands to bot owner."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if OWNER_ID != 0 and user_id != OWNER_ID:
            await update.message.reply_text("❌ Unauthorized access.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


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
💠 /watchlist — View active tracking matrix
💠 /channels — Uplink to deal channels
💠 /addchannel `<link>` — Manually uplink to a new channel
💠 /untrackchannel `<name>` — Instantly untrack a specific channel
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
/updatesynonyms `laptop | gaming, macbook, asus` — Updates parameters for an existing target

**2️⃣ Network Uplinks:**
Use /channels to view available nodes and toggle monitoring.
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


@owner_only
async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add keyword to watchlist."""
    if not context.args:
        await update.message.reply_text(
            "❌ Please specify a keyword.\n"
            "Example: `/watch fridge` or `/watch laptop | gaming, macbook`",
            parse_mode='Markdown'
        )
        return

    full_arg = ' '.join(context.args)

    # Check for custom synonyms (separated by |)
    if '|' in full_arg:
        parts = full_arg.split('|', 1)
        keyword = parts[0].strip()
        custom_synonyms = parts[1].strip()
    else:
        keyword = full_arg.strip()
        custom_synonyms = ''

    success = await db.add_keyword(keyword, custom_synonyms)

    if success:
        syns = matcher.get_display_synonyms(keyword, custom_synonyms)
        syn_text = f"\n💡 *Synonyms included:* {', '.join(syns)}" if syns else ""
        await update.message.reply_text(
            f"✅ *Added to watchlist:* `{keyword}`{syn_text}\n\n"
            f"NDT will notify you when deals appear in your monitored channels!",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"⚠️ `{keyword}` is already on your watchlist.\n\nUse `/updatesynonyms {keyword} | new, synonyms` to update it.", parse_mode='Markdown')

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
        await update.message.reply_text(f"🗑️ Removed `{keyword}` from watchlist.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ `{keyword}` was not found in your watchlist.", parse_mode='Markdown')


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
        message += f"• `{kw}`{syn_preview}\n"

        # Add delete button for each keyword
        keyboard.append([InlineKeyboardButton(f"❌ Delete '{kw}'", callback_data=f"del_kw_{kw}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')


@owner_only
async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show channel selection menu."""
    if not monitor:
        await update.message.reply_text("⏳ Channel monitor loading, please try again in a moment.")
        return

    await update.message.reply_text("🔍 Fetching your joined channels... ⏳")

async def _get_channels_page(page=0):
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
    channels_list.sort(key=lambda x: (x[1].get('channel_name') or x[1].get('channel_username') or '').lower())
    
    ITEMS_PER_PAGE = 30
    total_pages = max(1, (len(channels_list) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_items = channels_list[start_idx:end_idx]

    message = f"📢 *MONITORED CHANNELS*\n\n"
    active_names = []
    for ch_id, ch_info in channels_list:
        if ch_id in active_ids:
            name = ch_info.get('channel_name') or ch_info.get('channel_username') or f"Channel {ch_id}"
            active_names.append(name)

    if active_names:
        message += "🟢 **Currently Tracking:**\n"
        for name in active_names[:40]:  # Limit to 40 in text to avoid message length limits
            message += f"• {name[:50]}\n"
        if len(active_names) > 40:
            message += f"...and {len(active_names) - 40} more\n"
    else:
        message += "🔴 Not tracking any channels yet.\n"

    message += f"\n*Toggle channels below (Page {page+1}/{total_pages}):*\n"
    
    keyboard = []

    for ch_id, ch_info in current_items:
        name = ch_info.get('channel_name') or ch_info.get('channel_username') or f"Channel {ch_id}"
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

    # Untrack All button
    if active_ids:
        keyboard.append([InlineKeyboardButton("🛑 Untrack All Channels", callback_data="untrack_all_channels")])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return message, reply_markup

@owner_only
async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show channel selection menu."""
    if not monitor:
        await update.message.reply_text("⏳ Channel monitor loading, please try again in a moment.")
        return

    try:
        message, reply_markup = await _get_channels_page(page=0)
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error listing channels: {e}")
        await update.message.reply_text(f"❌ Error listing channels: {str(e)}")


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
        time_ago = format_time_ago(asyncio.get_event_loop().time() - deal.get('matched_date', 0))

        link = deal.get('message_link')
        link_str = f" [Link]({link})" if link else ""

        message += f"• *{product[:40]}*{price_str} ({channel}){link_str}\n"

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

    msg = f"""
📊 **NDT STATUS & STATS**

**Status:** {status_str}
🎯 **Tracked Keywords:** {stats['watchlist_count']}
📢 **Monitored Channels:** {stats['active_channels']} / {stats['total_channels']}
🔥 **Deals Found (24h):** {stats['deals_24h']}
📦 **Total Deals All-Time:** {stats['total_deals']}
    """
    await update.message.reply_text(msg, parse_mode='Markdown')


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
            await query.edit_message_text(f"🗑️ Removed `{keyword}` from watchlist.", parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ Error removing `{keyword}`.")

    # Toggle channel callback
    elif data.startswith('toggle_ch_'):
        parts = data.split('_')
        ch_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        await db.toggle_channel(ch_id)

        if monitor:
            await monitor.refresh_channels()

        try:
            msg, markup = await _get_channels_page(page)
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
            msg, markup = await _get_channels_page(0)
            await query.edit_message_text(msg, reply_markup=markup, parse_mode='Markdown')
            await query.answer("🛑 Untracked all channels!")
        except Exception as e:
            logger.error(f"Error untracking all channels: {e}")
            await query.edit_message_text("❌ Error untracking channels.")
            
    # Pagination callback
    elif data.startswith('page_ch_'):
        page = int(data.replace('page_ch_', ''))
        try:
            msg, markup = await _get_channels_page(page)
            await query.edit_message_text(msg, reply_markup=markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error paging channels: {e}")
            await query.edit_message_text("❌ Error changing pages.")


async def post_init(application):
    """Start the Telethon monitor when the bot starts."""
    global monitor
    
    # Set bot commands menu
    await application.bot.set_my_commands([
        BotCommand("watch", "Add product to watchlist"),
        BotCommand("unwatch", "Remove product from watchlist"),
        BotCommand("updatesynonyms", "Update custom synonyms for a keyword"),
        BotCommand("watchlist", "View & manage tracked products"),
        BotCommand("channels", "Select channels to monitor"),
        BotCommand("addchannel", "Join & track a new channel"),
        BotCommand("untrackchannel", "Untrack a specific channel"),
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
        logger.error(f"Failed to start ChannelMonitor: {e}")

    # ── Render.com Keep-Alive Web Server ──
    # Render requires "Web Services" to bind to a port, or it shuts them down.
    # We run a dummy web server in the background to satisfy Render's health checks.
    port = os.getenv('PORT')
    if port:
        from aiohttp import web
        async def dummy_health_check(request):
            return web.Response(text="Neon Drop Tracker is running 24/7!")
        
        web_app = web.Application()
        web_app.router.add_get('/', dummy_health_check)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', int(port))
        await site.start()
        logger.info(f"🌐 Started dummy web server on port {port} for Render keep-alive.")


def main():
    """Start NDT Bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is missing!")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("unwatch", unwatch_command))
    application.add_handler(CommandHandler("updatesynonyms", update_synonyms_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("channels", channels_command))
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

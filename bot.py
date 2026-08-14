import asyncio
import functools
import html
import logging
import re
import time

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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


# ══════════════════════════════════════════════════════════════════════
#  Ask-for-input flow
#
#  Tapping a command in Telegram's menu sends it BARE — there is no way to
#  make the client pre-fill "/watch " and wait for you to type the rest. So
#  every command that needs an argument used to answer a menu tap with
#  "❌ Please specify a keyword", which is the whole reason the bot felt
#  unusable from the menu.
#
#  Instead, a bare command now ASKS. ForceReply pops the keyboard open with
#  the question quoted and a placeholder in the input box, and the next
#  message the user sends is consumed as that command's argument. Typing
#  `/watch shoes` in one go still works exactly as before.
# ══════════════════════════════════════════════════════════════════════

_PENDING_KEY = 'pending_prompt'

# action -> (question, input-box placeholder, extra help shown under the question)
_PROMPTS = {
    'watch': (
        '➕ What should I watch for?',
        'shoes',
        'Just a keyword — or <code>shoes -kids</code> to block terms, '
        '<code>laptop | gaming, macbook</code> to add synonyms.',
    ),
    'unwatch': ('➖ Which keyword should I drop?', 'shoes', ''),
    'synonyms': ('🔍 Which keyword?', 'shoes', ''),
    'updatesynonyms': (
        '💡 New synonyms',
        'laptop | gaming, macbook',
        'Format: <code>keyword | synonym1, synonym2</code>',
    ),
    'exclude': (
        '🚫 Terms to block',
        'shoes | kids, women',
        'Format: <code>keyword | term1, term2</code>. Send <code>keyword |</code> to clear.',
    ),
    'exclude_terms': (
        '🚫 Terms to block for <code>{keyword}</code>',
        'kids, women, socks',
        'Comma-separated. Send <code>-</code> to clear them.',
    ),
    'testmatch': (
        '🧪 Paste a message to test',
        'Nike Running Shoes Rs 1999',
        "I'll tell you whether it would alert, and why.",
    ),
    'addchannel': (
        '➕ Channel username or link',
        '@dealschannel',
        'Also accepts <code>https://t.me/dealschannel</code>.',
    ),
    'untrackchannel': ('🛑 Which channel should I untrack?', 'dealschannel', ''),
    'searchchannel': (
        '🔍 Search joined channels for',
        'loot',
        'Searches every joined channel, not just deal/sale ones.',
    ),
}


async def _prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, action, **data):
    """Ask for a command's missing argument and remember what it was for."""
    question, placeholder, hint = _PROMPTS[action]
    # Values land inside the question text, and a keyword is arbitrary user input.
    question = question.format(**{k: html.escape(str(v)) for k, v in data.items()})
    context.user_data[_PENDING_KEY] = dict(action=action, **data)

    body = question
    if hint:
        body += f"\n\n{hint}"
    body += "\n\n<i>Send /cancel to stop.</i>"

    await update.effective_message.reply_text(
        body,
        parse_mode='HTML',
        reply_markup=ForceReply(input_field_placeholder=placeholder[:64]),
    )


@owner_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abandon whatever the bot was waiting for."""
    pending = context.user_data.pop(_PENDING_KEY, None)
    if pending:
        await update.message.reply_text(
            f"🚫 Cancelled.", reply_markup=_quick_keyboard())
    else:
        await update.message.reply_text(
            "Nothing to cancel.", reply_markup=_quick_keyboard())


@owner_only
async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle a plain (non-command) message.

    Two jobs: run a quick-keyboard button, or feed the answer to whatever question
    the bot last asked. Anything else gets a nudge rather than silence — an
    unacknowledged message reads as a broken bot.
    """
    text = (update.message.text or '').strip()
    if not text:
        return

    action = _QUICK_ACTIONS.get(text)
    if action:
        # A quick-keyboard tap abandons any half-finished question.
        context.user_data.pop(_PENDING_KEY, None)
        await action(update, context)
        return

    pending = context.user_data.pop(_PENDING_KEY, None)
    if not pending:
        await update.message.reply_text(
            "🤔 I'm not expecting anything right now.\n"
            "Tap ☰ Menu below, or /help for what I can do.",
            reply_markup=_quick_keyboard())
        return

    applier = _APPLIERS[pending['action']]
    await applier(update, context, text, **{k: v for k, v in pending.items()
                                            if k != 'action'})


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - Welcome & Intro."""
    welcome = """╔════════════════════════════╗
      ⚡ <b>N D T   N E X U S</b> ⚡
╚════════════════════════════╝

💠 <b>SYSTEM ONLINE</b> 💠
Your personal deal tracker is watching your channels 24/7.

<b>Three things to get going:</b>
1️⃣ /watch — tell me what to look for
2️⃣ /channels — pick the channels to watch
3️⃣ Wait. Rate alerts with 👍/👎 so I learn which channels are worth it.

Every command works bare — send /watch with nothing and I'll ask.
Buttons below, or ☰ /menu for everything. /help for the manual."""
    await update.message.reply_text(
        welcome, parse_mode='HTML', reply_markup=_quick_keyboard())
    is_paused = monitor.paused if monitor else True
    await update.message.reply_text(
        _menu_text(is_paused), parse_mode='HTML',
        reply_markup=_menu_markup(is_paused))


HELP_TEXT = """📖 <b>N D T   M A N U A L</b>

<i>You never have to remember an argument.</i> Send any command bare — /watch, /exclude,
/testmatch — and NDT asks for what it needs. /cancel backs out. ☰ /menu does the
whole thing with buttons.

<b>1️⃣ Targets</b>
/watch <code>fridge</code> — also matches "refrigerator", "double door", …
/watch <code>laptop | gaming, macbook</code> — adds your own synonyms
/watch <code>shoes -kids -women</code> — blocks terms while adding
/exclude <code>shoes | kids, women</code> — blocks terms on an existing target.
A message containing a blocked term never alerts for that keyword, even if it
matches otherwise. Send <code>shoes |</code> to clear.
/testmatch <code>&lt;message&gt;</code> — dry-run before waiting on a real deal. It says which
keyword matched, or which blocked term vetoed it.

<b>2️⃣ Channels</b>
/channels — toggle monitoring. Lists only channels with <code>deal</code> or <code>sale</code> in the
name, plus everything you already track; a joined account has hundreds and the
rest are noise.
/searchchannel <code>&lt;text&gt;</code> — reaches every joined channel.
/addchannel <code>&lt;@name or link&gt;</code> — join and track a new one.

<b>3️⃣ Rate your alerts</b>
Every alert carries 👍/👎. One tap says whether that channel is worth keeping —
it feeds /channelreport, which also arrives on its own every 8 hours. Nothing
about the alert itself is stored, only per-channel tallies.

<b>4️⃣ Control</b>
/pause · /resume · /stats · /keyboard <code>on|off</code>

<i>System ready.</i> ⚡"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command."""
    await update.effective_message.reply_text(
        HELP_TEXT, parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('☰ Menu', callback_data='menu_home')]]))


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
        await _prompt(update, context, 'watch')
        return
    await _apply_watch(update, context, ' '.join(context.args))


async def _apply_watch(update, context, text):
    # Negative keywords first, so a '-term' can sit anywhere in the argument.
    full_arg, exclusions = parse_negatives(text)

    # Check for custom synonyms (separated by |)
    if '|' in full_arg:
        parts = full_arg.split('|', 1)
        keyword = parts[0].strip()
        custom_synonyms = parts[1].strip()
    else:
        keyword = full_arg.strip()
        custom_synonyms = ''

    if not keyword:
        await update.effective_message.reply_text(
            "❌ That's only exclusions — I need a keyword too.\n"
            "Example: <code>shoes -kids</code>",
            parse_mode='HTML')
        return

    success = await db.add_keyword(keyword, custom_synonyms, exclusions)

    if success:
        _invalidate_watchlist()
        syns = matcher.get_display_synonyms(keyword, custom_synonyms)
        syn_text = (f"\n💡 <b>Synonyms included:</b> {html.escape(', '.join(syns))}"
                    if syns else "")
        excl_text = (f"\n🚫 <b>Blocked terms:</b> {html.escape(exclusions)}"
                     if exclusions else "")
        await update.effective_message.reply_text(
            f"✅ <b>Now watching:</b> <code>{html.escape(keyword)}</code>"
            f"{syn_text}{excl_text}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('🚫 Block terms', callback_data=f'exc_kw_{keyword}'),
                InlineKeyboardButton('📋 Watchlist', callback_data='menu_watchlist'),
            ]]) if _fits_callback(f'exc_kw_{keyword}') else None,
        )
    else:
        await update.effective_message.reply_text(
            f"⚠️ <code>{html.escape(keyword)}</code> is already on your watchlist.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('💡 Synonyms', callback_data=f'syn_kw_{keyword}'),
                InlineKeyboardButton('🚫 Block terms', callback_data=f'exc_kw_{keyword}'),
            ]]) if _fits_callback(f'syn_kw_{keyword}') else None,
        )


@owner_only
async def exclude_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the negative keywords for an existing watchlist entry."""
    full_arg = ' '.join(context.args) if context.args else ''

    if not full_arg:
        # No argument: offer the watchlist as buttons rather than making the user
        # remember and retype a keyword.
        await _pick_keyword(update, context, 'exc_kw_',
                            '🚫 Block terms for which keyword?')
        return

    if '|' not in full_arg:
        await _prompt(update, context, 'exclude')
        return

    await _apply_exclude(update, context, full_arg)


async def _apply_exclude(update, context, text):
    keyword, exclusions = (part.strip() for part in text.split('|', 1))
    if not keyword:
        await update.effective_message.reply_text("❌ Which keyword?")
        return
    await _set_exclusions(update, context, keyword, exclusions)


async def _apply_exclude_terms(update, context, text, keyword=''):
    """Second half of the button flow: the keyword is known, this is the term list."""
    await _set_exclusions(update, context, keyword,
                          '' if text.strip() == '-' else text)


async def _set_exclusions(update, context, keyword, exclusions):
    if not await db.update_exclusions(keyword, exclusions):
        await update.effective_message.reply_text(
            f"❌ <code>{html.escape(keyword)}</code> is not on your watchlist. "
            f"Add it with /watch first.", parse_mode='HTML')
        return

    _invalidate_watchlist()
    if exclusions:
        await update.effective_message.reply_text(
            f"🚫 <b>Blocked for</b> <code>{html.escape(keyword)}</code>: "
            f"{html.escape(exclusions)}\n\n"
            f"A message containing any of those will no longer alert for this keyword.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('🧪 Test a message', callback_data='ask_testmatch'),
                InlineKeyboardButton('📋 Watchlist', callback_data='menu_watchlist'),
            ]]))
    else:
        await update.effective_message.reply_text(
            f"✅ Cleared all blocked terms for <code>{html.escape(keyword)}</code>.",
            parse_mode='HTML')

@owner_only
async def update_synonyms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update custom synonyms for an existing keyword."""
    full_arg = ' '.join(context.args) if context.args else ''
    if '|' not in full_arg:
        await _prompt(update, context, 'updatesynonyms')
        return
    await _apply_update_synonyms(update, context, full_arg)


async def _apply_update_synonyms(update, context, text):
    if '|' not in text:
        await update.effective_message.reply_text(
            "❌ Format: <code>keyword | synonym1, synonym2</code>", parse_mode='HTML')
        return

    parts = text.split('|', 1)
    keyword = parts[0].strip()
    custom_synonyms = parts[1].strip()

    success = await db.update_synonyms(keyword, custom_synonyms)

    if success:
        _invalidate_watchlist()
        syns = matcher.get_display_synonyms(keyword, custom_synonyms)
        syn_text = f"\n💡 *New Synonyms:* {', '.join(syns)}" if syns else ""
        await update.effective_message.reply_text(
            f"✅ <b>Updated synonyms for:</b> <code>{html.escape(keyword)}</code>{syn_text}",
            parse_mode='HTML'
        )
    else:
        await update.effective_message.reply_text(
            f"❌ <code>{html.escape(keyword)}</code> was not found in your watchlist. "
            f"Add it with /watch first.", parse_mode='HTML')


@owner_only
async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove keyword from watchlist."""
    if not context.args:
        await _pick_keyword(update, context, 'del_kw_', '➖ Which keyword should I drop?')
        return
    await _apply_unwatch(update, context, ' '.join(context.args))


async def _apply_unwatch(update, context, text):
    keyword = text.strip()
    success = await db.remove_keyword(keyword)

    if success:
        _invalidate_watchlist()
        await update.effective_message.reply_text(
            f"🗑️ Removed <code>{html.escape(keyword)}</code> from watchlist.",
            parse_mode='HTML')
    else:
        await update.effective_message.reply_text(
            f"❌ <code>{html.escape(keyword)}</code> was not found in your watchlist.",
            parse_mode='HTML')


@owner_only
async def testmatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dry-run the matcher against a message, without waiting for a real deal."""
    if not context.args:
        await _prompt(update, context, 'testmatch')
        return
    await _apply_testmatch(update, context, ' '.join(context.args))


async def _apply_testmatch(update, context, text):
    watchlist = await db.get_all_keywords()
    report, result = matcher.explain(text, watchlist)

    verdict = "✅ WOULD ALERT" if result else "🚫 WOULD NOT ALERT"
    await update.effective_message.reply_text(
        f"<b>{verdict}</b>\n\n<pre>{html.escape(report)}</pre>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('🧪 Test another', callback_data='ask_testmatch'),
            InlineKeyboardButton('📋 Watchlist', callback_data='menu_watchlist'),
        ]]),
    )


@owner_only
async def synonyms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show exactly what terms a keyword expands to."""
    if not context.args:
        await _pick_keyword(update, context, 'syn_kw_', '💡 Show synonyms for which keyword?')
        return
    await _apply_synonyms(update, context, ' '.join(context.args))


async def _apply_synonyms(update, context, text):
    keyword = text.strip()
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
    await update.effective_message.reply_text(
        f"<b>{html.escape(keyword)}</b> matches {len(terms)} term(s):\n\n"
        f"<pre>{html.escape(listed)}</pre>{blocked}",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('🚫 Block terms', callback_data=f'exc_kw_{keyword}'),
            InlineKeyboardButton('📋 Watchlist', callback_data='menu_watchlist'),
        ]]) if _fits_callback(f'exc_kw_{keyword}') else None,
    )


def _fits_callback(data):
    """Telegram rejects callback_data over 64 bytes — a long keyword would 400."""
    return len(data.encode()) <= 64


# Every view renderer returns (text, markup) and every view is HTML. Sending them
# through these two helpers is what keeps that true: the parse mode is not a
# per-call-site decision, so a renderer converted to HTML cannot be left with a
# caller still claiming Markdown — which is a hard 400 from Telegram, i.e. the
# screen simply fails to appear.
async def _reply_view(update, view):
    text, markup = view
    return await update.effective_message.reply_text(
        text, reply_markup=markup, parse_mode='HTML',
        disable_web_page_preview=True)


async def _edit_view(query, view):
    text, markup = view
    return await query.edit_message_text(
        text, reply_markup=markup, parse_mode='HTML',
        disable_web_page_preview=True)


async def _pick_keyword(update, context, prefix, title):
    """
    Offer the watchlist as buttons instead of asking the user to retype a keyword.

    Used wherever a command's argument is "one of your existing keywords" —
    /unwatch, /synonyms, /exclude. Keywords too long to fit in callback_data fall
    back to being typed.
    """
    items = await db.get_watchlist()
    if not items:
        await update.effective_message.reply_text(
            "📋 Your watchlist is empty.\n\nAdd something with /watch first.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('➕ Add a keyword', callback_data='ask_watch')]]))
        return

    keyboard, overflow = [], []
    for item in items:
        kw = item['keyword']
        if _fits_callback(f"{prefix}{kw}"):
            keyboard.append([InlineKeyboardButton(kw, callback_data=f"{prefix}{kw}")])
        else:
            overflow.append(kw)

    note = ''
    if overflow:
        note = ("\n\n<i>Too long to show as buttons: "
                + html.escape(', '.join(overflow)) + "</i>")

    await update.effective_message.reply_text(
        f"{title}{note}", parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)


async def _render_watchlist():
    """
    The watchlist view, shared by the command and the menu button.

    HTML, not Markdown — see the note on _render_deals. A keyword like `iphone_15`
    is enough to make Telegram reject a Markdown message outright.
    """
    items = await db.get_watchlist()

    if not items:
        return ("📋 <b>Your watchlist is empty.</b>\n\nTap below to add your first keyword.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton('➕ Add a keyword', callback_data='ask_watch')]]))

    message = f"📋 <b>YOUR WATCHLIST</b> ({len(items)})\n\n"
    keyboard = []

    for item in items:
        kw = item['keyword']
        syns = matcher.get_display_synonyms(kw, item.get('custom_synonyms', ''))
        syn_preview = f" ({html.escape(', '.join(syns[:3]))})" if syns else ""
        excluded = item.get('exclusions') or ''
        excl_preview = f"\n   🚫 blocked: {html.escape(excluded)}" if excluded else ""
        message += f"• <code>{html.escape(kw)}</code>{syn_preview}{excl_preview}\n"

        # Per-keyword actions. Delete used to be the only one, which meant editing a
        # keyword's synonyms or exclusions required retyping it from memory.
        if _fits_callback(f"exc_kw_{kw}"):
            keyboard.append([
                InlineKeyboardButton(f"🚫 {kw[:14]}", callback_data=f"exc_kw_{kw}"),
                InlineKeyboardButton(f"💡 {kw[:14]}", callback_data=f"syn_kw_{kw}"),
                InlineKeyboardButton("❌", callback_data=f"del_kw_{kw}"),
            ])

    keyboard.append([
        InlineKeyboardButton('➕ Add', callback_data='ask_watch'),
        InlineKeyboardButton('🧪 Test a message', callback_data='ask_testmatch'),
    ])
    keyboard.append([InlineKeyboardButton('« Menu', callback_data='menu_home')])

    return message, InlineKeyboardMarkup(keyboard)


@owner_only
async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display and manage watchlist."""
    await _reply_view(update, await _render_watchlist())


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
        back = InlineKeyboardMarkup([
            [InlineKeyboardButton('🔍 Search all', callback_data='ask_searchchannel'),
             InlineKeyboardButton('➕ Add channel', callback_data='ask_addchannel')],
            [InlineKeyboardButton('« Menu', callback_data='menu_home')],
        ])
        if search:
            return (
                f"🔍 No joined channel matches <code>{html.escape(search)}</code>.\n\n"
                "Try a shorter word, or join it directly.",
                back,
            )
        shown = html.escape(', '.join(CHANNEL_NAME_FILTERS))
        return (
            f"📢 <b>CHANNELS</b>\n\n"
            f"No joined channel has <code>{shown}</code> in its name.",
            back,
        )

    ITEMS_PER_PAGE = 30
    total_pages = max(1, (len(visible) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_items = visible[start_idx:end_idx]

    # HTML throughout: channel titles are arbitrary text and "LOOT_DEALS_INDIA" is a
    # completely ordinary name. In Markdown an odd number of underscores or asterisks
    # makes Telegram reject the message, so /channels would fail to render at all.
    message = "📢 <b>MONITORED CHANNELS</b>\n\n"
    active_names = [
        channel_display_name(ch_info)
        for ch_id, ch_info in channels_list if ch_id in active_ids
    ]

    if active_names:
        message += "🟢 <b>Currently tracking:</b>\n"
        for name in active_names[:40]:  # Limit to 40 in text to avoid message length limits
            message += f"• {html.escape(name[:50])}\n"
        if len(active_names) > 40:
            message += f"…and {len(active_names) - 40} more\n"
    else:
        message += "🔴 Not tracking any channels yet.\n"

    if search:
        message += (f"\n🔍 <b>Search:</b> <code>{html.escape(search)}</code> — "
                    f"{len(visible)} match(es)\n")
    else:
        filters_str = html.escape(', '.join(CHANNEL_NAME_FILTERS))
        message += (
            f"\n🔎 <i>Showing channels named</i> <code>{filters_str}</code> — "
            f"{len(visible)} of {len(channels_list)} joined.\n"
        )

    message += f"\n<b>Tap to toggle (page {page+1}/{total_pages}):</b>\n"

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
    else:
        keyboard.append([
            InlineKeyboardButton("🔍 Search all", callback_data="ask_searchchannel"),
            InlineKeyboardButton("➕ Add channel", callback_data="ask_addchannel"),
        ])

    # Untrack All button
    if active_ids:
        keyboard.append([InlineKeyboardButton("🛑 Untrack all", callback_data="untrack_all_channels")])

    keyboard.append([InlineKeyboardButton('« Menu', callback_data='menu_home')])

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
        await _reply_view(update, await _get_channels_page(page=0))
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
        await _prompt(update, context, 'searchchannel')
        return
    await _apply_search_channel(update, context, ' '.join(context.args))


async def _apply_search_channel(update, context, text):
    if not monitor:
        await update.effective_message.reply_text(
            "⏳ Channel monitor loading, please try again in a moment.")
        return

    query = text.strip()
    context.user_data[_SEARCH_KEY] = query

    try:
        await _reply_view(update, await _get_channels_page(page=0, search=query))
    except Exception as e:
        logger.error(f"Error searching channels: {e}")
        await update.effective_message.reply_text(f"❌ Error searching channels: {str(e)}")


@owner_only
async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a channel by username or link."""
    if not monitor:
        await update.message.reply_text("⏳ Channel monitor loading, please try again in a moment.")
        return

    if not context.args:
        await _prompt(update, context, 'addchannel')
        return
    await _apply_add_channel(update, context, context.args[0])


async def _apply_add_channel(update, context, text):
    if not monitor:
        await update.effective_message.reply_text(
            "⏳ Channel monitor loading, please try again in a moment.")
        return

    identifier = text.strip().split()[0] if text.strip() else ''
    if not identifier:
        await update.effective_message.reply_text("❌ I need a username or link.")
        return

    await update.effective_message.reply_text(
        f"⏳ Joining <code>{html.escape(identifier)}</code>…", parse_mode='HTML')

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
            await monitor.refresh_channels()

            await update.effective_message.reply_text(
                f"✅ Joined and now tracking "
                f"<b>{html.escape(channel_info['channel_name'])}</b>!",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('📢 All channels', callback_data='menu_channels')]]))
        else:
            await update.effective_message.reply_text(
                "❌ Could not resolve this channel. Make sure it's a public channel "
                "or a valid invite link.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Failed to join channel: {str(e)}")


@owner_only
async def untrack_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Untrack a specific channel by name or username."""
    if not context.args:
        await _prompt(update, context, 'untrackchannel')
        return
    await _apply_untrack_channel(update, context, ' '.join(context.args))


async def _apply_untrack_channel(update, context, text):
    identifier = text.strip()
    success = await db.untrack_specific_channel(identifier)

    if success:
        if monitor:
            await monitor.refresh_channels()
        await update.effective_message.reply_text(
            f"🛑 Untracked channel matching: <code>{html.escape(identifier)}</code>",
            parse_mode='HTML')
    else:
        await update.effective_message.reply_text(
            f"❌ No active channel matches <code>{html.escape(identifier)}</code>.\n"
            f"Try /channels to toggle one directly.", parse_mode='HTML')

@owner_only
async def deals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent matched deals."""
    await _reply_view(update, await _render_deals())


async def _render_deals():
    """
    Recent deals, shared by the command and the menu button.

    HTML with html.escape(), matching the notifier. Product names and channel titles
    are arbitrary text from a deal channel — one `*` or `_` in a Markdown message and
    Telegram rejects the whole thing with "can't parse entities", so the list fails
    to render rather than looking slightly wrong.
    """
    deals = await db.get_recent_deals(hours=24)
    back = InlineKeyboardMarkup([[InlineKeyboardButton('« Menu', callback_data='menu_home')]])

    if not deals:
        return ("📥 No matched deals in the last 24 hours.\n\n"
                "That's normal if your watchlist is small — /watchlist to check, "
                "or /channels to add sources.", back)

    message = f"🔥 <b>RECENT DEALS</b> (last 24h — {len(deals)} found)\n\n"

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
        link_str = f" <a href=\"{html.escape(link, quote=True)}\">Link</a>" if link else ""

        message += (f"• <b>{html.escape(product[:40])}</b>{price_str} "
                    f"({html.escape(channel)}) — {time_ago}{link_str}\n")

    return message, back


@owner_only
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause channel monitoring."""
    if monitor:
        await monitor.pause()
        await update.message.reply_text(
            "⏸️ Monitoring <b>paused</b>. Use /resume to start again.",
            parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Monitor is not running.")


@owner_only
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume channel monitoring."""
    if monitor:
        await monitor.resume()
        await update.message.reply_text(
            "▶️ Monitoring <b>resumed</b>!", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Monitor is not running.")


@owner_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status and statistics."""
    await _reply_view(update, await _render_stats())


async def _render_stats():
    """HTML like every other view — see _reply_view."""
    stats = await db.get_stats()
    is_paused = monitor.paused if monitor else True

    status_str = "⏸️ Paused" if is_paused else "🟢 Active & Monitoring"
    link_str = "🟢 Connected" if (monitor and monitor.is_connected()) else "🔴 Disconnected"
    uptime = format_time_ago(time.time() - _started_at)

    msg = f"""📊 <b>NDT STATUS</b>

<b>Status:</b> {status_str}
🔌 <b>Telethon uplink:</b> {link_str}
⏱️ <b>Uptime:</b> {uptime.replace(' ago', '')}
🎯 <b>Tracked keywords:</b> {stats['watchlist_count']}
📢 <b>Monitored channels:</b> {stats['active_channels']} / {stats['total_channels']}
🔥 <b>Deals found (24h):</b> {stats['deals_24h']}
📦 <b>Deals all-time:</b> {stats['total_deals']}"""
    toggle = ('▶️ Resume', 'act_resume') if is_paused else ('⏸️ Pause', 'act_pause')
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle[0], callback_data=toggle[1]),
         InlineKeyboardButton('📊 Channel report', callback_data='menu_report')],
        [InlineKeyboardButton('« Menu', callback_data='menu_home')],
    ])
    return msg, markup


# ══════════════════════════════════════════════════════════════════════
#  Menu & quick keyboard
# ══════════════════════════════════════════════════════════════════════

def _menu_markup(is_paused):
    toggle = ('▶️ Resume', 'act_resume') if is_paused else ('⏸️ Pause', 'act_pause')
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📋 Watchlist', callback_data='menu_watchlist'),
         InlineKeyboardButton('➕ Add keyword', callback_data='ask_watch')],
        [InlineKeyboardButton('📢 Channels', callback_data='menu_channels'),
         InlineKeyboardButton('🔍 Find channel', callback_data='ask_searchchannel')],
        [InlineKeyboardButton('🔥 Recent deals', callback_data='menu_deals'),
         InlineKeyboardButton('📊 Channel report', callback_data='menu_report')],
        [InlineKeyboardButton('🧪 Test a message', callback_data='ask_testmatch'),
         InlineKeyboardButton('⚙️ Status', callback_data='menu_stats')],
        [InlineKeyboardButton(toggle[0], callback_data=toggle[1]),
         InlineKeyboardButton('❓ Help', callback_data='menu_help')],
    ])


def _menu_text(is_paused):
    state = '⏸️ Paused' if is_paused else '🟢 Monitoring'
    return f"☰ <b>NDT MENU</b> — {state}\n\nPick something, or type a command."


def _quick_keyboard():
    """
    Persistent buttons under the input box.

    The command menu is hidden behind a ⁄ tap and sends commands bare; these are
    always visible and one tap each, which is what makes the bot usable one-handed
    on a phone.
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton('📋 Watchlist'), KeyboardButton('🔥 Deals')],
            [KeyboardButton('📢 Channels'), KeyboardButton('📊 Report')],
            [KeyboardButton('➕ Watch'), KeyboardButton('☰ Menu')],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder='Tap a button or type a command',
    )


@owner_only
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The button hub — everything reachable without typing a command."""
    is_paused = monitor.paused if monitor else True
    await update.effective_message.reply_text(
        _menu_text(is_paused), parse_mode='HTML',
        reply_markup=_menu_markup(is_paused))


@owner_only
async def keyboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show or hide the persistent quick-action keyboard."""
    arg = (context.args[0].lower() if context.args else 'on')
    if arg in ('off', 'hide', 'no'):
        await update.message.reply_text(
            "⌨️ Quick keyboard hidden. Bring it back with /keyboard.",
            reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text(
            "⌨️ Quick keyboard on.", reply_markup=_quick_keyboard())


# Text sent by the persistent keyboard, mapped to what it should do. Checked before
# the pending-question handler, so a button tap always wins over a stale prompt.
_QUICK_ACTIONS = {
    '📋 Watchlist': lambda u, c: watchlist_command(u, c),
    '🔥 Deals': lambda u, c: deals_command(u, c),
    '📢 Channels': lambda u, c: channels_command(u, c),
    '📊 Report': lambda u, c: channel_report_command(u, c),
    '➕ Watch': lambda u, c: _prompt(u, c, 'watch'),
    '☰ Menu': lambda u, c: menu_command(u, c),
}

# Pending-question action -> what consumes the user's answer.
_APPLIERS = {
    'watch': _apply_watch,
    'unwatch': _apply_unwatch,
    'synonyms': _apply_synonyms,
    'updatesynonyms': _apply_update_synonyms,
    'exclude': _apply_exclude,
    'exclude_terms': _apply_exclude_terms,
    'testmatch': _apply_testmatch,
    'addchannel': _apply_add_channel,
    'untrackchannel': _apply_untrack_channel,
    'searchchannel': _apply_search_channel,
}


@owner_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data

    # ── Menu navigation ──
    if data == 'menu_home':
        is_paused = monitor.paused if monitor else True
        await query.edit_message_text(
            _menu_text(is_paused), parse_mode='HTML',
            reply_markup=_menu_markup(is_paused))
        return

    if data in ('menu_watchlist', 'menu_deals', 'menu_stats', 'menu_channels',
                'menu_report', 'menu_help'):
        back = InlineKeyboardMarkup(
            [[InlineKeyboardButton('« Menu', callback_data='menu_home')]])
        try:
            if data == 'menu_watchlist':
                view = await _render_watchlist()
            elif data == 'menu_deals':
                view = await _render_deals()
            elif data == 'menu_stats':
                view = await _render_stats()
            elif data == 'menu_channels':
                context.user_data.pop(_SEARCH_KEY, None)
                view = await _get_channels_page(0)
            elif data == 'menu_report':
                view = (build_channel_report(await db.get_channel_report()), back)
            else:
                view = (HELP_TEXT, back)
            # Deliberately no per-branch parse mode: every renderer above is HTML,
            # and picking one here is how menu_stats and menu_help ended up claiming
            # Markdown for HTML text — a 400, so the screen never opened.
            await _edit_view(query, view)
        except Exception as e:
            logger.error(f"Menu render failed for {data}: {e}")
            await query.answer("❌ Couldn't open that.", show_alert=True)
        return

    # ── A button that needs typed input: ask, then let the text handler finish ──
    if data.startswith('ask_'):
        await _prompt(update, context, data[len('ask_'):])
        return

    # ── Pause / resume from a button ──
    if data in ('act_pause', 'act_resume'):
        if not monitor:
            await query.answer("❌ Monitor is not running.", show_alert=True)
            return
        if data == 'act_pause':
            await monitor.pause()
        else:
            await monitor.resume()
        is_paused = monitor.paused
        await query.edit_message_text(
            _menu_text(is_paused), parse_mode='HTML',
            reply_markup=_menu_markup(is_paused))
        await query.answer('⏸️ Paused' if is_paused else '▶️ Resumed')
        return

    # ── Keyword picked from a list ──
    if data.startswith('syn_kw_'):
        await _apply_synonyms(update, context, data[len('syn_kw_'):])
        return

    if data.startswith('exc_kw_'):
        keyword = data[len('exc_kw_'):]
        await _prompt(update, context, 'exclude_terms', keyword=keyword)
        return

    # Delete keyword callback
    if data.startswith('del_kw_'):
        keyword = data[len('del_kw_'):]
        success = await db.remove_keyword(keyword)
        if not success:
            await query.answer(f"❌ Couldn't remove {keyword}.", show_alert=True)
            return
        _invalidate_watchlist()
        await query.answer(f"🗑️ Removed {keyword}")
        # Re-render in place instead of replacing the list with a one-line
        # confirmation — deleting two keywords used to mean re-running /watchlist.
        await _edit_view(query, await _render_watchlist())

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
            await _edit_view(query, await _get_channels_page(
                page, context.user_data.get(_SEARCH_KEY)))
        except Exception as e:
            logger.error(f"Error updating channels message: {e}")
            await query.edit_message_text("❌ Error updating channels.")

    # Untrack all channels — confirm first. This wipes every source in one tap and
    # sat directly under the per-channel toggles, which is a mis-tap away.
    elif data == 'untrack_all_channels':
        await query.edit_message_reply_markup(InlineKeyboardMarkup([
            [InlineKeyboardButton('⚠️ Yes, untrack everything',
                                  callback_data='untrack_all_confirm')],
            [InlineKeyboardButton('« Keep them', callback_data='page_ch_0')],
        ]))
        await query.answer('Are you sure?')

    elif data == 'untrack_all_confirm':
        await db.untrack_all_channels()
        if monitor:
            await monitor.refresh_channels()

        try:
            await _edit_view(query, await _get_channels_page(
                0, context.user_data.get(_SEARCH_KEY)))
            await query.answer("🛑 Untracked all channels!")
        except Exception as e:
            logger.error(f"Error untracking all channels: {e}")
            await query.edit_message_text("❌ Error untracking channels.")

    # Leave search mode, back to the deal/sale list
    elif data == 'ch_clear_search':
        context.user_data.pop(_SEARCH_KEY, None)
        try:
            await _edit_view(query, await _get_channels_page(0))
        except Exception as e:
            logger.error(f"Error clearing channel search: {e}")
            await query.edit_message_text("❌ Error clearing search.")

    # Pagination callback
    elif data.startswith('page_ch_'):
        page = int(data.replace('page_ch_', ''))
        try:
            await _edit_view(query, await _get_channels_page(
                page, context.user_data.get(_SEARCH_KEY)))
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
    # Menu order = how often you'd reach for it. Descriptions say what happens when
    # the command is tapped bare, because that is how the menu sends it — every one
    # of these either acts immediately or asks for what it needs.
    await application.bot.set_my_commands([
        BotCommand("menu", "☰ Everything, as buttons"),
        BotCommand("watchlist", "📋 Your keywords — edit or delete"),
        BotCommand("watch", "➕ Add a keyword (I'll ask)"),
        BotCommand("channels", "📢 Pick channels to monitor"),
        BotCommand("deals", "🔥 Deals matched in the last 24h"),
        BotCommand("channelreport", "📊 Which channels are worth keeping"),
        BotCommand("testmatch", "🧪 Would this message alert? (I'll ask)"),
        BotCommand("exclude", "🚫 Block terms for a keyword"),
        BotCommand("synonyms", "💡 What a keyword actually matches"),
        BotCommand("updatesynonyms", "✏️ Change a keyword's synonyms"),
        BotCommand("unwatch", "➖ Drop a keyword"),
        BotCommand("searchchannel", "🔍 Search all joined channels"),
        BotCommand("addchannel", "🔗 Join & track a new channel"),
        BotCommand("untrackchannel", "🛑 Untrack a specific channel"),
        BotCommand("stats", "⚙️ Status & uptime"),
        BotCommand("pause", "⏸️ Pause monitoring"),
        BotCommand("resume", "▶️ Resume monitoring"),
        BotCommand("keyboard", "⌨️ Show/hide the quick buttons"),
        BotCommand("cancel", "🚫 Cancel what I'm waiting for"),
        BotCommand("help", "❓ How to use"),
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
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("keyboard", keyboard_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    # Registered LAST: it is the catch-all for plain text, and must not shadow a
    # command. It answers whatever question the bot asked, or runs a quick-keyboard
    # button.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    logger.info("🚀 NDT Telegram Bot initializing...")

    # Start bot polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

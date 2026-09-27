import logging
import os
from dotenv import load_dotenv
from supabase import Client, create_client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Setup & Configurations
# ---------------------------------------------------------------------------
load_dotenv()

LOGGING_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=LOGGING_FORMAT)
logger = logging.getLogger(__name__)

# Supabase Credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Pre-defined Theaters List (Includes "ALL" option)
THEATERS = {
    "ALL": "🌟 ALL THEATERS",
    "CD1020778": "INOX Marina Mall, Egatoor",
    "CD9505": "Cinepolis BSR Mall, Thoraipakkam",
    "CD1030432": "Miraj Cinemas Sekaran Mall, Perumbakkam",
}

# Conversation States
SELECT_THEATER, ENTER_MOVIE, ENTER_PRICES, ENTER_TIMES, ENTER_NTFY = range(5)
EDIT_FIELD_SELECT, EDIT_PRICES, EDIT_TIMES, EDIT_NTFY = range(5, 9)

# ---------------------------------------------------------------------------
# Core Menu Handlers
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"👋 Welcome <b>{user.first_name}</b>!\n\n"
        "I monitor real-time movie ticket openings so you never miss a show.\n\n"
        "<b>Choose an option below:</b>"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Create New Alert", callback_data="btn_newalert")],
        [InlineKeyboardButton("📋 My Active Alerts", callback_data="btn_myalerts")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=reply_markup)


async def list_alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    
    user_id = update.effective_user.id

    try:
        res = (
            supabase.table("user_alerts")
            .select("*")
            .eq("telegram_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        alerts = res.data

        if not alerts:
            text = "⚠️ You have no saved alerts."
            keyboard = [[InlineKeyboardButton("➕ Add New Alert", callback_data="btn_newalert")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if query:
                await query.edit_message_text(text, reply_markup=reply_markup)
            else:
                await update.message.reply_text(text, reply_markup=reply_markup)
            return

        text = "📋 <b>YOUR MONITORED ALERTS</b>\n<i>Tap an alert button to manage:</i>\n\n"
        keyboard = []

        for idx, alert in enumerate(alerts, 1):
            t_name = THEATERS.get(alert["theater_id"], alert["theater_id"])
            m_name = "ANY MOVIE" if alert["movie_name"].upper() == "ALL" else alert["movie_name"]
            status_icon = "🟢" if alert.get("is_active", True) else "⏸️"
            ntfy_status = f"<code>{alert['ntfy_topic']}</code>" if alert.get("ntfy_topic") else "None"
            
            text += (
                f"{status_icon} <b>{idx}. {m_name}</b>\n"
                f"📍 <i>{t_name}</i>\n"
                f"💰 ₹{alert['min_price']} - ₹{alert['max_price']} | 🕒 {alert['start_time'][:5]}-{alert['end_time'][:5]}\n"
                f"📢 Backup ntfy: {ntfy_status}\n\n"
            )
            keyboard.append([
                InlineKeyboardButton(f"⚙️ Manage Alert #{idx} ({m_name[:12]}...)", callback_data=f"manage_{alert['id']}")
            ])

        keyboard.append([InlineKeyboardButton("➕ Add New Alert", callback_data="btn_newalert")])
        keyboard.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="btn_mainmenu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        if query:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
        err_msg = "❌ Failed to fetch alerts from database."
        if query:
            await query.edit_message_text(err_msg)
        else:
            await update.message.reply_text(err_msg)


async def manage_alert_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    alert_id = query.data.replace("manage_", "")
    res = supabase.table("user_alerts").select("*").eq("id", alert_id).execute()
    
    if not res.data:
        await query.edit_message_text("❌ Alert not found.")
        return

    alert = res.data[0]
    context.user_data["editing_alert_id"] = alert_id
    t_name = THEATERS.get(alert["theater_id"], alert["theater_id"])
    m_name = "ANY MOVIE (ALL)" if alert["movie_name"].upper() == "ALL" else alert["movie_name"]
    status_label = "🟢 Active" if alert.get("is_active", True) else "⏸️ Paused"
    toggle_text = "⏸️ Pause Monitoring" if alert.get("is_active", True) else "▶️ Resume Monitoring"

    text = (
        f"⚙️ <b>MANAGE ALERT DETAILS</b>\n\n"
        f"🎬 <b>Movie:</b> {m_name}\n"
        f"📍 <b>Theater:</b> {t_name}\n"
        f"💰 <b>Price:</b> ₹{alert['min_price']} - ₹{alert['max_price']}\n"
        f"🕒 <b>Timing:</b> {alert['start_time'][:5]} to {alert['end_time'][:5]}\n"
        f"📢 <b>ntfy Topic:</b> {alert.get('ntfy_topic') or 'None'}\n"
        f"📊 <b>Status:</b> {status_label}\n"
    )

    keyboard = [
        [InlineKeyboardButton(toggle_text, callback_data=f"toggle_{alert_id}")],
        [InlineKeyboardButton("✏️ Edit Rules", callback_data=f"editstart_{alert_id}")],
        [InlineKeyboardButton("🗑️ Delete Alert", callback_data=f"delconfirm_{alert_id}")],
        [InlineKeyboardButton("⬅️ Back to List", callback_data="btn_myalerts")]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def toggle_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    alert_id = query.data.replace("toggle_", "")
    res = supabase.table("user_alerts").select("is_active").eq("id", alert_id).execute()
    if res.data:
        curr = res.data[0].get("is_active", True)
        supabase.table("user_alerts").update({"is_active": not curr}).eq("id", alert_id).execute()
    
    res = supabase.table("user_alerts").select("*").eq("id", alert_id).execute()
    if not res.data:
        await query.edit_message_text("❌ Alert not found.")
        return

    alert = res.data[0]
    context.user_data["editing_alert_id"] = alert_id
    t_name = THEATERS.get(alert["theater_id"], alert["theater_id"])
    m_name = "ANY MOVIE (ALL)" if alert["movie_name"].upper() == "ALL" else alert["movie_name"]
    status_label = "🟢 Active" if alert.get("is_active", True) else "⏸️ Paused"
    toggle_text = "⏸️ Pause Monitoring" if alert.get("is_active", True) else "▶️ Resume Monitoring"

    text = (
        f"⚙️ <b>MANAGE ALERT DETAILS</b>\n\n"
        f"🎬 <b>Movie:</b> {m_name}\n"
        f"📍 <b>Theater:</b> {t_name}\n"
        f"💰 <b>Price:</b> ₹{alert['min_price']} - ₹{alert['max_price']}\n"
        f"🕒 <b>Timing:</b> {alert['start_time'][:5]} to {alert['end_time'][:5]}\n"
        f"📢 <b>ntfy Topic:</b> {alert.get('ntfy_topic') or 'None'}\n"
        f"📊 <b>Status:</b> {status_label}\n"
    )

    keyboard = [
        [InlineKeyboardButton(toggle_text, callback_data=f"toggle_{alert_id}")],
        [InlineKeyboardButton("✏️ Edit Rules", callback_data=f"editstart_{alert_id}")],
        [InlineKeyboardButton("🗑️ Delete Alert", callback_data=f"delconfirm_{alert_id}")],
        [InlineKeyboardButton("⬅️ Back to List", callback_data="btn_myalerts")]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def confirm_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    alert_id = query.data.replace("delconfirm_", "")
    text = "⚠️ <b>Are you sure you want to delete this alert rule?</b>"
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"deldo_{alert_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"manage_{alert_id}")
        ]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def perform_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    alert_id = query.data.replace("deldo_", "")
    try:
        supabase.table("user_alerts").delete().eq("id", alert_id).execute()
        await query.edit_message_text("🗑️ <b>Alert deleted successfully!</b>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error deleting: {e}")
        await query.edit_message_text("❌ Failed to delete alert.")

# ---------------------------------------------------------------------------
# EDIT FLOW HANDLERS
# ---------------------------------------------------------------------------
async def edit_alert_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    alert_id = query.data.replace("editstart_", "")
    context.user_data["editing_alert_id"] = alert_id

    text = "✏️ <b>Which field would you like to update?</b>"
    keyboard = [
        [InlineKeyboardButton("💰 Edit Price Range", callback_data="field_prices")],
        [InlineKeyboardButton("🕒 Edit Time Window", callback_data="field_times")],
        [InlineKeyboardButton("📢 Edit Backup ntfy Topic", callback_data="field_ntfy")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data=f"manage_{alert_id}")]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_FIELD_SELECT


async def edit_field_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "field_prices":
        await query.edit_message_text("💰 Send new price range: <code>min max</code> (e.g., <code>0 150</code>)", parse_mode="HTML")
        return EDIT_PRICES
    elif choice == "field_times":
        await query.edit_message_text("🕒 Send new time window: <code>start_time end_time</code> (e.g., <code>10:00 21:00</code>)", parse_mode="HTML")
        return EDIT_TIMES
    elif choice == "field_ntfy":
        keyboard = [[InlineKeyboardButton("❌ Remove ntfy Topic", callback_data="remove_ntfy")]]
        await query.edit_message_text("📢 Send new <b>ntfy topic name</b> (or tap remove below):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return EDIT_NTFY

    return ConversationHandler.END


async def save_edited_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alert_id = context.user_data.get("editing_alert_id")
    try:
        parts = update.message.text.strip().split()
        min_p, max_p = float(parts[0]), float(parts[1])
        supabase.table("user_alerts").update({"min_price": min_p, "max_price": max_p}).eq("id", alert_id).execute()
        await update.message.reply_text("✅ Price range updated successfully!")
    except Exception:
        await update.message.reply_text("❌ Invalid format! Please enter values like <code>0 150</code>:", parse_mode="HTML")
        return EDIT_PRICES
    return ConversationHandler.END


async def save_edited_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alert_id = context.user_data.get("editing_alert_id")
    try:
        parts = update.message.text.strip().split()
        start_t = parts[0] + ":00" if len(parts[0]) == 5 else parts[0]
        end_t = parts[1] + ":00" if len(parts[1]) == 5 else parts[1]
        supabase.table("user_alerts").update({"start_time": start_t, "end_time": end_t}).eq("id", alert_id).execute()
        await update.message.reply_text("✅ Time window updated successfully!")
    except Exception:
        await update.message.reply_text("❌ Invalid format! Please enter hours like <code>10:00 21:00</code>:", parse_mode="HTML")
        return EDIT_TIMES
    return ConversationHandler.END


async def save_edited_ntfy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alert_id = context.user_data.get("editing_alert_id")
    if update.callback_query:
        await update.callback_query.answer()
        new_topic = None
    else:
        new_topic = update.message.text.strip().replace(" ", "_")

    supabase.table("user_alerts").update({"ntfy_topic": new_topic}).eq("id", alert_id).execute()
    
    msg = "✅ Backup ntfy topic updated!"
    if update.callback_query:
        await update.callback_query.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# CREATE NEW ALERT FLOW
# ---------------------------------------------------------------------------
async def new_alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    context.user_data.clear()

    keyboard = [[InlineKeyboardButton(name, callback_data=code)] for code, name in THEATERS.items()]
    keyboard.append([InlineKeyboardButton("⬅️ Cancel", callback_data="btn_mainmenu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = "📍 <b>Step 1/5: Select a Theater</b>\n<i>Choose a specific theater or 'ALL THEATERS':</i>"
    if query:
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=reply_markup)
        
    return SELECT_THEATER


async def theater_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["theater_id"] = query.data
    theater_name = THEATERS.get(query.data)

    await query.edit_message_text(
        f"Selected: <b>{theater_name}</b>\n\n"
        "🎬 <b>Step 2/5: Type Movie Name</b>\n"
        "<i>(e.g., Meesaya Murukku 2 or type <code>ALL</code> for any movie)</i>",
        parse_mode="HTML",
    )
    return ENTER_MOVIE


async def movie_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["movie_name"] = update.message.text.strip()
    await update.message.reply_text(
        "💰 <b>Step 3/5: Enter Price Range (Min Max)</b>\n"
        "<i>Example: 0 100</i>",
        parse_mode="HTML",
    )
    return ENTER_PRICES


async def prices_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parts = text.split()
        context.user_data["min_price"] = float(parts[0])
        context.user_data["max_price"] = float(parts[1])
    except Exception:
        await update.message.reply_text("❌ Invalid format! Type two numbers like <code>0 100</code>:", parse_mode="HTML")
        return ENTER_PRICES

    await update.message.reply_text(
        "🕒 <b>Step 4/5: Enter Time Range (24-hr format)</b>\n"
        "<i>Example: 09:00 22:00</i>",
        parse_mode="HTML",
    )
    return ENTER_TIMES


async def times_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parts = text.split()
        context.user_data["start_time"] = parts[0] + ":00" if len(parts[0]) == 5 else parts[0]
        context.user_data["end_time"] = parts[1] + ":00" if len(parts[1]) == 5 else parts[1]
    except Exception:
        await update.message.reply_text("❌ Invalid format! Enter hours like <code>09:00 22:00</code>:", parse_mode="HTML")
        return ENTER_TIMES

    keyboard = [[InlineKeyboardButton("⏭️ Skip ntfy setup", callback_data="skip_ntfy")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📢 <b>Step 5/5: Backup Push Alert (Optional)</b>\n\n"
        "Type your unique <b>ntfy topic name</b> (e.g., <code>my_movie_updates_123</code>), or click Skip below:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )
    return ENTER_NTFY


async def save_new_alert(update: Update, context: ContextTypes.DEFAULT_TYPE, ntfy_topic: str = None):
    user = update.effective_user
    payload = {
        "telegram_id": user.id,
        "username": user.username or user.first_name,
        "theater_id": context.user_data["theater_id"],
        "movie_name": context.user_data["movie_name"],
        "min_price": context.user_data["min_price"],
        "max_price": context.user_data["max_price"],
        "start_time": context.user_data["start_time"],
        "end_time": context.user_data["end_time"],
        "ntfy_topic": ntfy_topic,
        "is_active": True,
    }

    try:
        supabase.table("user_alerts").upsert(
            payload, on_conflict="telegram_id, movie_name, theater_id"
        ).execute()

        t_name = THEATERS.get(payload["theater_id"])
        m_display = "ANY MOVIE (ALL)" if payload["movie_name"].upper() == "ALL" else payload["movie_name"]
        ntfy_str = f"<code>{ntfy_topic}</code>" if ntfy_topic else "None"

        summary = (
            "✅ <b>ALERT CREATED SUCCESSFULLY!</b>\n\n"
            f"🎬 Movie: {m_display}\n"
            f"📍 Theater: {t_name}\n"
            f"💰 Price: ₹{payload['min_price']} - ₹{payload['max_price']}\n"
            f"🕒 Timing: {payload['start_time'][:5]} to {payload['end_time'][:5]}\n"
            f"📢 Backup ntfy: {ntfy_str}\n"
        )
        keyboard = [[InlineKeyboardButton("📋 View All Alerts", callback_data="btn_myalerts")]]
        
        if update.callback_query:
            await update.callback_query.edit_message_text(summary, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(summary, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"Database error: {e}")
        err_msg = "❌ Database error! Alert could not be saved."
        if update.callback_query:
            await update.callback_query.edit_message_text(err_msg)
        else:
            await update.message.reply_text(err_msg)

    return ConversationHandler.END


async def ntfy_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip().replace(" ", "_")
    return await save_new_alert(update, context, ntfy_topic=topic)


async def ntfy_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await save_new_alert(update, context, ntfy_topic=None)


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Action cancelled.")
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# App Runner
# ---------------------------------------------------------------------------
def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("CRITICAL ERROR: Please add your TELEGRAM_BOT_TOKEN!")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    create_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newalert", new_alert_start),
            CallbackQueryHandler(new_alert_start, pattern="^btn_newalert$")
        ],
        states={
            SELECT_THEATER: [CallbackQueryHandler(theater_selected)],
            ENTER_MOVIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, movie_entered)],
            ENTER_PRICES: [MessageHandler(filters.TEXT & ~filters.COMMAND, prices_entered)],
            ENTER_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, times_entered)],
            ENTER_NTFY: [
                CallbackQueryHandler(ntfy_skipped, pattern="^skip_ntfy$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ntfy_entered),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_alert_menu, pattern="^editstart_")],
        states={
            EDIT_FIELD_SELECT: [CallbackQueryHandler(edit_field_choice, pattern="^field_")],
            EDIT_PRICES: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_prices)],
            EDIT_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_times)],
            EDIT_NTFY: [
                CallbackQueryHandler(save_edited_ntfy, pattern="^remove_ntfy$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_ntfy),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myalerts", list_alerts_command))
    app.add_handler(CallbackQueryHandler(start_command, pattern="^btn_mainmenu$"))
    app.add_handler(CallbackQueryHandler(list_alerts_command, pattern="^btn_myalerts$"))
    app.add_handler(CallbackQueryHandler(manage_alert_detail, pattern="^manage_"))
    app.add_handler(CallbackQueryHandler(toggle_status_callback, pattern="^toggle_"))
    app.add_handler(CallbackQueryHandler(confirm_delete_callback, pattern="^delconfirm_"))
    app.add_handler(CallbackQueryHandler(perform_delete_callback, pattern="^deldo_"))
    
    app.add_handler(create_conv)
    app.add_handler(edit_conv)

    print("🤖 Telegram Bot active with 'ALL' Theater and 'ALL' Movie support...")
    app.run_polling()


if __name__ == "__main__":
    main()
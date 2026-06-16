import os
import json
import logging
import asyncio
from datetime import datetime
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Google GenAI, Sheets, & Telegram dependencies
from google import genai
from google.genai import types
import gspread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# 1. Initialize environments and system logging infrastructure
load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- FIX: Declare variables globally ---
gc = None
sheet = None

# 2. Refined strict Pydantic Schema for Multi-Hotel Management
class HotelIssue(BaseModel):
    hotel_name: str = Field(description="The specific hotel branch named (e.g., 'Mango Valley', 'Apex', etc.). Use 'Unknown' if not stated.")
    room_number: str = Field(description="The specific room number, floor, building block, or area mentioned within that hotel. Use 'Unknown' if not stated.")
    issue_type: str = Field(description="Must be strictly one of: Plumbing, Electrical, HVAC, Housekeeping, Maintenance, Other")
    description: str = Field(description="A clean, highly professional summary of the problem reported.")
    urgency: str = Field(description="Must be strictly one of: Low, Medium, High based on immediate property damage risk or guest luxury impact.")

# 3. Instantiate Google API Clients
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# --- FIX: Connection logic in a function to allow re-connection ---
def connect_google():
    global gc, sheet
    try:
        google_creds = os.environ.get('GOOGLE_CREDENTIALS')
        if google_creds:
            creds_dict = json.loads(google_creds)
            gc = gspread.service_account_from_dict(creds_dict)
            spreadsheet = gc.open("Hotel_Operations_Log")
            sheet = spreadsheet.worksheet("Active_Issues")
            print("✅ Database Connection Established. Google Sheets connected successfully!")
        else:
            print("❌ Error: GOOGLE_CREDENTIALS environment variable not found. Check Render Dashboard.")
            sheet = None
    except Exception as e:
        logger.error(f"Google Sheets Auth Error: {str(e)}.")
        sheet = None

# Initial connection call
connect_google()

# Pull operational variables safely from runtime configurations
AUNT_ID = int(os.environ.get("AUNT_TELEGRAM_ID", 0))

SYSTEM_INSTRUCTION = (
    "You are an expert multi-property hotel operations routing manager. Your task is to analyze raw "
    "messages from floor staff, normalize formatting, and extract properties into the specified schema. "
    "Pay extremely close attention to which specific hotel property/branch is mentioned in the text (such as "
    "Mango Valley) and isolate it cleanly from the room number or physical area."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes system onboarding commands."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"🛎️ **HotelOps Control Bot Engaged.**\n\n"
        f"Your Telegram User ID is: `{user_id}`\n"
        f"Add this ID to your server's roster configurations to receive direct dispatches."
    )

async def process_staff_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercepts messy worker strings, uses Gemini to clean up records, and generates logs."""
    global sheet
    # --- FIX: Re-check connection if lost ---
    if sheet is None: connect_google()

    raw_text = update.message.text
    sender = update.message.from_user.first_name

    status_indicator = await update.message.reply_text("🔄 AI processing incident parameters...")

    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Staff Member: {sender}\nReport: {raw_text}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=HotelIssue,
                system_instruction=SYSTEM_INSTRUCTION
            ),
        )

        parsed_data = json.loads(response.text)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        combined_location = f"{parsed_data.get('hotel_name')} - {parsed_data.get('room_number')}"

        target_row_index = 0
        if sheet:
            update_res = sheet.append_row([
                timestamp,
                combined_location,
                parsed_data.get('issue_type'),
                parsed_data.get('urgency'),
                parsed_data.get('description'),
                sender,
                "Pending"
            ])
            try:
                update_res_dict = dict(update_res) if not isinstance(update_res, dict) else update_res
                updated_range = update_res_dict.get('updates', {}).get('updatedRange', '')
                target_row_index = int(updated_range.split('A')[-1].split(':')[0])
            except Exception:
                target_row_index = len(sheet.get_all_values())

        alert_payload = (
            f"🚨 **New Operational Report**\n\n"
            f"🏨 **Hotel Property:** {parsed_data.get('hotel_name')}\n"
            f"📍 **Room / Area:** {parsed_data.get('room_number')}\n"
            f"🏷️ **Category:** {parsed_data.get('issue_type')}\n"
            f"⚠️ **Urgency Level:** {parsed_data.get('urgency')}\n"
            f"📝 **Description:** {parsed_data.get('description')}\n"
            f"👤 **Log Source:** {sender}\n"
            f"⏰ **Timestamp:** {timestamp}"
        )

        await status_indicator.edit_text("✅ Report successfully registered into administration console.")

        keyboard = [
            [
                InlineKeyboardButton("🧹 Housekeeping", callback_data=f"disp_housekeeping_{target_row_index}"),
                InlineKeyboardButton("🔧 Maintenance", callback_data=f"disp_maintenance_{target_row_index}")
            ],
            [
                InlineKeyboardButton("👤 Forward To Garet", callback_data=f"disp_admin_{target_row_index}"),
                InlineKeyboardButton("👤 Forward To Mariel", callback_data=f"disp_admin2_{target_row_index}")
            ],
            [InlineKeyboardButton("❌ Dismiss / Archive Task", callback_data=f"disp_dismiss_{target_row_index}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=AUNT_ID,
            text=alert_payload,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Failed to process message step: {str(e)}")
        await status_indicator.edit_text("❌ Data tracking failure. System failed to structure input.")

async def execute_dispatch_routing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Monitors clicks on administrative dispatch menus and worker resolutions."""
    global gc, sheet
    if gc is None: connect_google()
    
    query = update.callback_query
    await query.answer()

    original_text = query.message.text
    selection_path = query.data
    current_keyboard = query.message.reply_markup

    if selection_path.startswith("worker_resolve_"):
        sheet_row = selection_path.replace("worker_resolve_", "")
        try:
            if sheet and sheet_row.isdigit():
                sheet.update_cell(int(sheet_row), 7, "Resolved")
            await query.edit_message_text(text=f"{original_text}\n\n✅ **Status: Marked as Resolved**")
            await context.bot.send_message(chat_id=AUNT_ID, text=f"🍏 **Update:** A task has been marked as **Resolved**!\n\n{original_text}")
        except Exception as sheet_err:
            logger.error(f"Failed to patch resolved cell row status: {sheet_err}")
            await query.edit_message_text(text=f"{original_text}\n\n❌ Database connection timeout.", reply_markup=current_keyboard)
        return

    try:
        roster_sheet = gc.open("Hotel_Operations_Log").worksheet("Staff_Roster")
        all_rows = roster_sheet.get_all_values()
        roster = {}
        for row in all_rows:
            if len(row) >= 2:
                role_key = str(row[0]).strip().lower()
                id_val = str(row[1]).strip()
                if id_val.isdigit(): roster[role_key] = int(id_val)
    except Exception as err:
        logger.error(f"Spreadsheet Roster read error: {err}")
        roster = {}

    parts = selection_path.split("_")
    action_type = f"{parts[0]}_{parts[1]}"
    sheet_row = parts[2] if len(parts) > 2 else "0"

    mapping = {
        "disp_maintenance": ("maintenance", "Maintenance", "🔧 **Routed directly to Maintenance Lead.**"),
        "disp_housekeeping": ("housekeeping", "Housekeeping", "🧹 **Routed directly to Housekeeping Lead.**"),
        "disp_admin": ("garet", "Admin (Garet)", "👤 **Escalated straight to Admin (Kuya Garet).**"),
        "disp_admin2": ("mariel", "Admin (Mariel)", "👤 **Escalated straight to Admin (Mariel).**")
    }

    if action_type in mapping:
        key, name, msg = mapping[action_type]
        target_id = roster.get(key)
        if target_id:
            worker_keyboard = [[InlineKeyboardButton("✅ Mark as Resolved", callback_data=f"worker_resolve_{sheet_row}")]]
            await context.bot.send_message(chat_id=target_id, text=f"📥 **Incoming Work Order Assignment:**\n\n{original_text}", reply_markup=InlineKeyboardMarkup(worker_keyboard))
            await query.edit_message_text(text=f"{original_text}\n\n✅ {msg}")
        else:
            await query.edit_message_text(text=f"{original_text}\n\n⚠️ **Routing Failed:** Could not find ID for '{name}' in Roster.", reply_markup=current_keyboard)
    else:
        await query.edit_message_text(text=f"{original_text}\n\n🗑️ **Alert closed without routing.**")

# --- REWORKED: Flask implementation running alongside the async loop ---
app_flask = Flask('')

@app_flask.route('/')
def home(): 
    return "Bot is alive!"

def run_flask_sync():
    """Runs Flask synchronously within an independent system thread cleanly."""
    port = int(os.environ.get("PORT", 8080))
    # use_reloader=False prevents Flask from spinning up extra child processes/threads
    app_flask.run(host='0.0.0.0', port=port, use_reloader=False)

async def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    # Start Flask entirely separate from the asynchronous event loop runtime
    flask_thread = Thread(target=run_flask_sync, daemon=True)
    flask_thread.start()
    print("🌐 Web dummy endpoint successfully spawned.")

    # Initialize Telegram Application
    app = Application.builder().token(token).drop_pending_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(execute_dispatch_routing))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_staff_report))

    # Initialize, start, and dynamically yield processing execution control loops
    async with app:
        await app.initialize()
        await app.start()
        print("🚀 Multi-Hotel Tracking System Engine Online. Polling updates asynchronously...")
        await app.updater.start_polling(drop_pending_updates=True)
        
        # Keeps the async loop alive continuously without freezing up system processes
        while True:
            await asyncio.sleep(3600)

if __name__ == '__main__':
    # Force python-telegram-bot to run on a clean, decoupled execution stack
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🤖 Bot cleanly disconnected.")
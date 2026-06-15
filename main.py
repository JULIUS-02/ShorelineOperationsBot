import os
import json
import logging
from datetime import datetime
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

# 2. Refined strict Pydantic Schema for Multi-Hotel Management
class HotelIssue(BaseModel):
    hotel_name: str = Field(description="The specific hotel branch named (e.g., 'Mango Valley', 'Apex', etc.). Use 'Unknown' if not stated.")
    room_number: str = Field(description="The specific room number, floor, building block, or area mentioned within that hotel. Use 'Unknown' if not stated.")
    issue_type: str = Field(description="Must be strictly one of: Plumbing, Electrical, HVAC, Housekeeping, Maintenance, Other")
    description: str = Field(description="A clean, highly professional summary of the problem reported.")
    urgency: str = Field(description="Must be strictly one of: Low, Medium, High based on immediate property damage risk or guest luxury impact.")

# 3. Instantiate Google API Clients
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

try:
    gc = gspread.service_account(filename="credentials.json")
    spreadsheet = gc.open("Hotel_Operations_Log")
    sheet = spreadsheet.worksheet("Active_Issues")
    print("✅ Database Connection Established. Google Sheets connected successfully!")
except Exception as e:
    import traceback
    logger.error(f"Google Sheets Auth Error: {str(e)}.")
    sheet = None

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

        # Step 2: Log data into Google Sheets and find out exactly what row it landed on
        target_row_index = 0
        if sheet:
            # Appending returns information about the updated spreadsheet range
            update_res = sheet.append_row([
                timestamp,
                combined_location,
                parsed_data.get('issue_type'),
                parsed_data.get('urgency'),
                parsed_data.get('description'),
                sender,
                "Pending"
            ])
            # Parse row tracking number from the gspread grid updates response
            try:
                updated_range = update_res.get('updates', {}).get('updatedRange', '')
                target_row_index = int(updated_range.split('A')[-1].split(':')[0])
            except Exception:
                # Fallback calculation if gspread API range shapes change
                target_row_index = len(sheet.get_all_values())

        # Build message string block
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

        # Embed the row index into the button data path strings so the dispatch function can remember it
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
    query = update.callback_query
    await query.answer()

    original_text = query.message.text
    selection_path = query.data
    current_keyboard = query.message.reply_markup

    # Close-loop check: If a worker clicked 'Resolve' from their direct assignment box
    if selection_path.startswith("worker_resolve_"):
        sheet_row = selection_path.replace("worker_resolve_", "")

        try:
            if sheet and sheet_row.isdigit():
                # Column G is row element index 7 (Status Column)
                sheet.update_cell(int(sheet_row), 7, "Resolved")

            # Wipe buttons out of the worker's DM entirely to prevent accidental re-clicking
            await query.edit_message_text(text=f"{original_text}\n\n✅ **Status: Marked as Resolved**")

            # Send an explicit notification loop update to your Aunt's console automatically!
            await context.bot.send_message(
                chat_id=AUNT_ID,
                text=f"🍏 **Update:** A task has been marked as **Resolved**!\n\n{original_text}"
            )
        except Exception as sheet_err:
            logger.error(f"Failed to patch resolved cell row status: {sheet_err}")
            await query.edit_message_text(text=f"{original_text}\n\n❌ Database connection timeout. Could not patch status.", reply_markup=current_keyboard)
        return

    # Dynamic Roster parsing from the spreadsheet
    try:
        roster_sheet = gc.open("Hotel_Operations_Log").worksheet("Staff_Roster")
        all_rows = roster_sheet.get_all_values()
        roster = {}
        for row in all_rows:
            if len(row) >= 2:
                role_key = str(row[0]).strip().lower()
                id_val = str(row[1]).strip()
                if id_val.isdigit():
                    roster[role_key] = int(id_val)
    except Exception as err:
        logger.error(f"Spreadsheet Roster read error: {err}")
        roster = {}

    # Extract target row index from the incoming prefix string pattern
    # format strings look like: "disp_housekeeping_14"
    parts = selection_path.split("_")
    action_type = f"{parts[0]}_{parts[1]}" # e.g. "disp_housekeeping"
    sheet_row = parts[2] if len(parts) > 2 else "0"

    if action_type == "disp_maintenance":
        target_id = roster.get("maintenance")
        department_name = "Maintenance"
        confirmation_msg = "🔧 **Routed directly to Maintenance Lead.**"
    elif action_type == "disp_housekeeping":
        target_id = roster.get("housekeeping")
        department_name = "Housekeeping"
        confirmation_msg = "🧹 **Routed directly to Housekeeping Lead.**"
    elif action_type == "disp_admin":
        target_id = roster.get("garet")
        department_name = "Admin (Garet)"
        confirmation_msg = "👤 **Escalated straight to Admin (Kuya Garet).**"
    elif action_type == "disp_admin2":
        target_id = roster.get("mariel")
        department_name = "Admin (Mariel)"
        confirmation_msg = "👤 **Escalated straight to Admin (Mariel).**"
    else:
        await query.edit_message_text(text=f"{original_text}\n\n🗑️ **Alert closed without routing.**")
        return

    if not target_id or target_id == 0:
        await query.edit_message_text(
            text=f"{original_text}\n\n⚠️ **Routing Failed:** Could not find an ID mapping for '{department_name}' in the Staff Roster tab.",
            reply_markup=current_keyboard
        )
        return

    # Forward instruction packet to the department, and hand them a dynamic 'Resolve' button
    try:
        worker_keyboard = [[InlineKeyboardButton("✅ Mark as Resolved", callback_data=f"worker_resolve_{sheet_row}")]]
        worker_markup = InlineKeyboardMarkup(worker_keyboard)

        await context.bot.send_message(
            chat_id=target_id,
            text=f"📥 **Incoming Work Order Assignment:**\n\n{original_text}",
            reply_markup=worker_markup # Attach resolution option
        )

        # Confirm action completion cleanly on your Aunt's phone screen interface
        await query.edit_message_text(text=f"{original_text}\n\n✅ {confirmation_msg}")
    except Exception:
        await query.edit_message_text(
            text=f"{original_text}\n\n❌ **Routing Blocked:** {department_name} has not initialized or pressed 'Start' on this bot yet.",
            reply_markup=current_keyboard
        )

if __name__ == '__main__':
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    proxy_url = "http://proxy.server:3128"
    app = (
        Application.builder()
        .token(token)
        .proxy(proxy_url)
        .get_updates_proxy(proxy_url)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(execute_dispatch_routing))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_staff_report))

    print("🚀 Multi-Hotel Tracking System Engine Online. Running...")
    app.run_polling()
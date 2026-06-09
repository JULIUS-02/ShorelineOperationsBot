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

# 2. Define strict Pydantic Output Formatting Expectations for Gemini
class HotelIssue(BaseModel):
    room_number: str = Field(description="The specific room number, building block, or area mentioned. Use 'Unknown' if not stated.")
    issue_type: str = Field(description="Must be strictly one of: Plumbing, Electrical, HVAC, Housekeeping, Maintenance, Other")
    description: str = Field(description="A clean, highly professional summary of the problem reported.")
    urgency: str = Field(description="Must be strictly one of: Low, Medium, High based on immediate property damage risk or guest luxury impact.")

# 3. Instantiate Google API Clients
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

try:
    # Explicitly enforce gspread native service account parsing
    gc, authorized_client = gspread.oauth_from_dict, None
    gc = gspread.service_account(filename="credentials.json")
    
    # Open the spreadsheet and grab handles for BOTH tabs
    spreadsheet = gc.open("Hotel_Operations_Log")
    sheet = spreadsheet.worksheet("Active_Issues")
    roster_sheet = spreadsheet.worksheet("Staff_Roster")
    print("✅ Database Connection Established. Google Sheets connected successfully!")
except Exception as e:
    import traceback
    logger.error(f"Google Sheets Auth Error: {str(e)}. Sheet writing will be bypassed locally.")
    traceback.print_exc()
    sheet = None

# Pull operational variables safely from runtime configurations
AUNT_ID = int(os.environ.get("AUNT_TELEGRAM_ID", 0))

SYSTEM_INSTRUCTION = (
    "You are an expert hotel operations routing manager. Your task is to analyze raw "
    "messages from floor staff, normalize formatting, and extract properties into the specified schema."
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
    
    # Notify reporter that request is being parsed asynchronously
    status_indicator = await update.message.reply_text("🔄 AI processing incident parameters...")

    try:
        # Step 1: Query Gemini 2.5 Flash for Data Normalization
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

        # Step 2: Safely log data into Google Sheets Backup Database
        if sheet:
            sheet.append_row([
                timestamp,
                parsed_data.get('room_number'),
                parsed_data.get('issue_type'),
                parsed_data.get('urgency'),
                parsed_data.get('description'),
                sender,
                "Pending" # Default status configuration
            ])
        
        # Format the beautiful, concise string block your aunt will read on her phone
        alert_payload = (
            f"🚨 **New Operational Report**\n\n"
            f"📍 **Room / Loc:** {parsed_data.get('room_number')}\n"
            f"🏷️ **Category:** {parsed_data.get('issue_type')}\n"
            f"⚠️ **Urgency Level:** {parsed_data.get('urgency')}\n"
            f"📝 **Description:** {parsed_data.get('description')}\n"
            f"👤 **Log Source:** {sender}\n"
            f"⏰ **Timestamp:** {timestamp}"
        )

        await status_indicator.edit_text("✅ Report successfully registered into administration console.")

        # Step 3: Dynamically generate Inline Interactive Option Rows for your Aunt
        keyboard = [
            [
                InlineKeyboardButton("🧹 Housekeeping", callback_data="dispatch_housekeeping"),
                InlineKeyboardButton("🔧 Maintenance", callback_data="dispatch_maintenance")
            ],
            [InlineKeyboardButton("❌ Dismiss / Archive Task", callback_data="dispatch_dismiss")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Send the finalized dispatch package into your Aunt's private DM
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
    """Monitors clicks on administrative dispatch menus, dynamically altering routes."""
    query = update.callback_query
    await query.answer() # Immediately drop selection wheel UI animations

    original_text = query.message.text
    selection_path = query.data

    # Read the staff roster from your running database or sheet mappings dynamically
    try:
        roster_sheet = gc.open("Hotel_Operations_Log").worksheet("Staff_Roster")
        # Structure formats read from sheet columns: [Role, Telegram_ID]
        records = roster_sheet.get_all_records()
        roster = {row['Role'].lower(): int(row['Telegram_ID']) for row in records}
    except Exception:
        # Local static mock fallback if sheet mapping fails during initialization
        roster = {"housekeeping": AUNT_ID, "maintenance": AUNT_ID} 

    if selection_path == "dispatch_maintenance":
        target_id = roster.get("maintenance", AUNT_ID)
        confirmation_msg = "🔧 **Routed directly to Maintenance Lead.**"
    elif selection_path == "dispatch_housekeeping":
        target_id = roster.get("housekeeping", AUNT_ID)
        confirmation_msg = "🧹 **Routed directly to Housekeeping Lead.**"
    else:
        # Dismiss selected
        await query.edit_message_text(text=f"{original_text}\n\n🗑️ **Alert closed without routing.**")
        return

    # Forward the clear work instructions directly to the specific worker's private DM box
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"📥 **Incoming Work Order Assignment:**\n\n{original_text}"
        )
        # Re-render your aunt's chat screen to cleanly wipe buttons and display clear dispatch status
        await query.edit_message_text(text=f"{original_alert}\n\n✅ {confirmation_msg}")
    except Exception:
        await query.edit_message_text(
            text=f"{original_text}\n\n❌ **Routing Blocked:** Target employee has not initialized or pressed 'Start' on this bot yet."
        )

if __name__ == '__main__':
    # Initialize the engine runner
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    print(f"DEBUG: Token loaded = {repr(token)}")
    print(f"DEBUG: Token length = {len(token) if token else 'None'}")
    app = Application.builder().token(token).build()

    # Link incoming message signatures to core handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(execute_dispatch_routing))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_staff_report))

    print("🚀 System Online. Awaiting live incoming telemetry updates...")
    app.run_polling()
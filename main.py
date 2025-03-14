import os
import logging
import json
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Union, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
from telegram.error import TelegramError, Forbidden, RetryAfter

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

AWAITING_IMAGE = 1
AWAITING_BUTTONS = 2
AWAITING_FILTER_TRIGGER = 3

DATA_DIR = "data"
IMAGES_DIR = os.path.join(DATA_DIR, "images")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

FILTERS_FILE = os.path.join(DATA_DIR, "filters.json")

RATE_LIMIT = 5
TIME_WINDOW = 60
user_request_tracker = {}

filters_cache = {}

def load_filters():
    if os.path.exists(FILTERS_FILE):
        try:
            with open(FILTERS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error loading filters: {e}")
            return {}
    return {}

def save_filters(filters_data):
    try:
        with open(FILTERS_FILE, 'w', encoding='utf-8') as f:
            json_data = {str(k): v for k, v in filters_data.items()}
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        return True
    except IOError as e:
        logger.error(f"Error saving filters: {e}")
        return False

def check_rate_limit(user_id: int) -> bool:
    current_time = time.time()
    
    for uid in list(user_request_tracker.keys()):
        user_request_tracker[uid] = [t for t in user_request_tracker[uid] if current_time - t < TIME_WINDOW]
        if not user_request_tracker[uid]:
            del user_request_tracker[uid]
    
    if user_id not in user_request_tracker:
        user_request_tracker[user_id] = [current_time]
        return True
    
    if len(user_request_tracker[user_id]) < RATE_LIMIT:
        user_request_tracker[user_id].append(current_time)
        return True
    
    return False

async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        user = await context.bot.get_chat_member(chat_id, user_id)
        return user.status in ["administrator", "creator"]
    except TelegramError:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_message = (
        "🤖 *Welcome to Multi-Group Filter Bot!* 🤖\n\n"
        "This bot allows you to create message filters in any group, including private ones.\n\n"
        "*How to use:*\n"
        "1️⃣ Add this bot to your group (must be admin)\n"
        "2️⃣ Use /filter to create a new filter (admin only)\n"
        "3️⃣ Send an image within 60 seconds (or skip with /skip)\n"
        "4️⃣ Create buttons in format: `text|url, text2|url2`\n"
        "5️⃣ Set the trigger word for your filter\n\n"
        "*Filter Management:*\n"
        "• /filters - List all filters in current chat\n"
        "• /stop - Cancel current operation\n"
        "• /deletefilter <trigger> - Remove a filter (admin only)\n\n"
        "*Advanced Features:*\n"
        "• Supports unlimited filters per group\n"
        "• Works in private groups\n"
        "• Image and button support\n\n"
        "Made with ❤️ by @YourUsername"
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def stop_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if 'filter_image_path' in context.user_data and os.path.exists(context.user_data['filter_image_path']):
        try:
            os.remove(context.user_data['filter_image_path'])
        except Exception as e:
            logger.error(f"Error removing temporary image: {e}")
    
    context.user_data.clear()
    await update.message.reply_text("Operation canceled.")
    return ConversationHandler.END

async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    
    if not check_rate_limit(user_id):
        await update.message.reply_text(
            "⚠️ You're making requests too quickly. Please wait a minute and try again.")
        return ConversationHandler.END
    
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id
    
    if chat_type in ["group", "supergroup"]:
        if not await is_admin(chat_id, user_id, context):
            await update.message.reply_text(
                "❌ You need to be an admin to create filters in this group.")
            return ConversationHandler.END
    
    context.user_data['filter_chat_id'] = chat_id
    
    await update.message.reply_text(
        "📸 Please send an image for your filter within 60 seconds.\n"
        "Or use /skip if you don't want to include an image.")
    
    context.job_queue.run_once(
        timeout_callback,
        60,
        data={"chat_id": chat_id, "user_id": user_id},
        name=f"filter_timeout_{user_id}"
    )
    
    context.user_data['timeout_job'] = f"filter_timeout_{user_id}"
    return AWAITING_IMAGE

async def timeout_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    await context.bot.send_message(
        chat_id=job_data["chat_id"],
        text="⏰ Time's up! Filter creation canceled. Use /filter to start again."
    )

async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job_name = context.user_data.get('timeout_job')
    if job_name:
        current_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in current_jobs:
            job.schedule_removal()
    
    photo = update.message.photo[-1]
    file_id = photo.file_id
    
    try:
        file = await context.bot.get_file(file_id)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chat_id = context.user_data['filter_chat_id']
        filename = f"{chat_id}_{timestamp}.jpg"
        image_path = os.path.join(IMAGES_DIR, filename)
        
        await file.download_to_drive(image_path)
        context.user_data['filter_image_path'] = image_path
        context.user_data['filter_image_filename'] = filename
        
        await update.message.reply_text(
            "✅ Image received! Now, please send the buttons you want to include in format:\n"
            "`text|url, text2|url2`\n\n"
            "Or send /skip if you don't want buttons.")
        return AWAITING_BUTTONS
        
    except TelegramError as e:
        logger.error(f"Error downloading image: {e}")
        await update.message.reply_text(
            "❌ Failed to download image. Please try again with /filter command.")
        return ConversationHandler.END

async def skip_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job_name = context.user_data.get('timeout_job')
    if job_name:
        current_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in current_jobs:
            job.schedule_removal()
    
    context.user_data['filter_image_path'] = None
    context.user_data['filter_image_filename'] = None
    
    await update.message.reply_text(
        "🔄 Skipped image. Now, please send the buttons you want to include in format:\n"
        "`text|url, text2|url2`\n\n"
        "Or send /skip if you don't want buttons.")
    return AWAITING_BUTTONS

async def receive_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "/skip":
        context.user_data['filter_buttons'] = []
        await update.message.reply_text(
            "🔄 Skipped buttons. Finally, please send the trigger word or phrase for this filter.")
        return AWAITING_FILTER_TRIGGER
    
    try:
        buttons_text = update.message.text.strip()
        buttons_data = []
        
        button_definitions = [b.strip() for b in buttons_text.split(',')]
        
        for button_def in button_definitions:
            if '|' not in button_def:
                await update.message.reply_text(
                    "❌ Invalid button format. Please use `text|url, text2|url2` format or /skip.")
                return AWAITING_BUTTONS
            
            parts = button_def.split('|', 1)
            text = parts[0].strip()
            url = parts[1].strip()
            
            if not (url.startswith('http://') or url.startswith('https://')):
                await update.message.reply_text(
                    f"❌ Invalid URL: {url}\nPlease enter a valid URL starting with http:// or https://")
                return AWAITING_BUTTONS
            
            buttons_data.append({"text": text, "url": url})
        
        context.user_data['filter_buttons'] = buttons_data
        
        keyboard = []
        row = []
        for button in buttons_data:
            row.append(InlineKeyboardButton(text=button["text"], url=button["url"]))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "✅ Buttons configured! Here's a preview:", 
            reply_markup=reply_markup)
        
        await update.message.reply_text(
            "Finally, please send the trigger word or phrase for this filter.")
        return AWAITING_FILTER_TRIGGER
        
    except Exception as e:
        logger.error(f"Error processing buttons: {e}")
        await update.message.reply_text(
            "❌ There was an error processing your buttons. Please try again with format: `text|url, text2|url2`")
        return AWAITING_BUTTONS

async def receive_filter_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    trigger = update.message.text.strip().lower()
    
    chat_id = context.user_data['filter_chat_id']
    
    global filters_cache
    if not filters_cache:
        filters_cache = load_filters()
    
    chat_filters = filters_cache.get(chat_id, {})
    
    if trigger in chat_filters:
        await update.message.reply_text(
            f"❌ A filter with trigger '{trigger}' already exists in this chat. "
            f"Please use /deletefilter {trigger} first or choose a different trigger.")
        return AWAITING_FILTER_TRIGGER
    
    filter_data = {
        "image": context.user_data.get('filter_image_filename'),
        "buttons": context.user_data.get('filter_buttons', []),
        "created_by": update.effective_user.id,
        "created_at": datetime.now().isoformat()
    }
    
    if chat_id not in filters_cache:
        filters_cache[chat_id] = {}
    
    filters_cache[chat_id][trigger] = filter_data
    
    if not save_filters(filters_cache):
        await update.message.reply_text(
            "⚠️ There was an issue saving your filter, but it will work until the bot restarts.")
    
    confirmation = f"✅ Filter created successfully!\n\n" \
                   f"• Trigger: `{trigger}`\n" \
                   f"• Image: {'Yes' if filter_data['image'] else 'No'}\n" \
                   f"• Buttons: {len(filter_data['buttons'])}\n\n" \
                   f"Users can now trigger this filter by typing: `{trigger}`"
    
    await update.message.reply_text(confirmation, parse_mode='Markdown')
    context.user_data.clear()
    return ConversationHandler.END

async def list_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    global filters_cache
    if not filters_cache:
        filters_cache = load_filters()
    
    chat_filters = filters_cache.get(chat_id, {})
    
    if not chat_filters:
        await update.message.reply_text("No filters found in this chat.")
        return
    
    response = "📋 *List of filters in this chat:*\n\n"
    
    for i, (trigger, data) in enumerate(chat_filters.items(), 1):
        response += f"{i}. `{trigger}`"
        if data.get("image"):
            response += " (with image)"
        if data.get("buttons"):
            response += f" ({len(data['buttons'])} buttons)"
        response += "\n"
    
    response += "\nTo use a filter, simply type its trigger word in the chat."
    
    await update.message.reply_text(response, parse_mode='Markdown')

async def delete_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "❌ Please specify the filter trigger to delete.\n"
            "Example: `/deletefilter hello`")
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if update.effective_chat.type in ["group", "supergroup"]:
        if not await is_admin(chat_id, user_id, context):
            await update.message.reply_text("❌ Only admins can delete filters.")
            return
    
    trigger = context.args[0].lower()
    
    global filters_cache
    if not filters_cache:
        filters_cache = load_filters

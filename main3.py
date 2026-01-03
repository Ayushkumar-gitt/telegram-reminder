from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.tl.custom import Button
import re
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import dateparser
import requests
from zoneinfo import ZoneInfo
import string
from difflib import SequenceMatcher
import sqlite3
import json
import google.generativeai as genai
from typing import Optional, Dict, List, Tuple
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
gemini_api_key = os.getenv("GEMINI_API_KEY")

# Configure Gemini
genai.configure(api_key=gemini_api_key)
model = genai.GenerativeModel('gemini-2.5-flash')  # Using stable model with better rate limits

app = FastAPI()
client = TelegramClient('session_name', api_id, api_hash)

DB_PATH = "reminders.db"


# ================== DB SETUP ==================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            objective TEXT,
            execution_id TEXT,
            user TEXT,
            phone TEXT,
            scheduled_at TEXT,
            created_at TEXT,
            recurrence_pattern TEXT,
            priority TEXT DEFAULT 'normal',
            status TEXT DEFAULT 'active',
            last_triggered TEXT,
            notes TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            phone TEXT,
            created_at TEXT,
            timezone TEXT DEFAULT 'Asia/Kolkata',
            preferred_call_start TEXT DEFAULT '09:00',
            preferred_call_end TEXT DEFAULT '21:00'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_phone (
            username TEXT PRIMARY KEY
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS reminder_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reminder_id INTEGER,
            objective TEXT,
            user TEXT,
            scheduled_at TEXT,
            triggered_at TEXT,
            status TEXT,
            notes TEXT
        )
    """)

    # Create indexes for better query performance
    c.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reminders_phone ON reminders(phone)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON reminder_history(user)")

    conn.commit()
    conn.close()


init_db()


# ================= USER HELPERS =================
def get_user_phone(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT phone FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def get_user_preferences(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT timezone, preferred_call_start, preferred_call_end 
        FROM users WHERE username = ?
    """, (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'timezone': row[0],
            'call_start': row[1],
            'call_end': row[2]
        }
    return {
        'timezone': 'Asia/Kolkata',
        'call_start': '09:00',
        'call_end': '21:00'
    }


def save_or_update_user(username, phone):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        INSERT INTO users (username, phone, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET phone=excluded.phone
    """, (username, phone, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def set_pending_phone(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO pending_phone (username) VALUES (?)", (username,))
    conn.commit()
    conn.close()


def clear_pending_phone(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM pending_phone WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def is_waiting_for_phone(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username FROM pending_phone WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    return bool(row)


# ================= CONVERSATION STATE =================
conversation_states = {}

def set_conversation_state(username, state, data=None):
    """Store conversation state for multi-step interactions"""
    conversation_states[username] = {
        'state': state,
        'data': data,
        'timestamp': datetime.now()
    }

def get_conversation_state(username):
    """Get current conversation state"""
    state = conversation_states.get(username)
    if state:
        # Expire state after 5 minutes
        if datetime.now() - state['timestamp'] > timedelta(minutes=5):
            clear_conversation_state(username)
            return None
    return state

def clear_conversation_state(username):
    """Clear conversation state"""
    if username in conversation_states:
        del conversation_states[username]


# ================= REMINDER DB =================
def save_reminder(objective, execution_id, user, phone, scheduled_at, 
                  recurrence_pattern=None, priority='normal', notes=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO reminders (objective, execution_id, user, phone, scheduled_at, 
                              created_at, recurrence_pattern, priority, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
    """, (objective, execution_id, user, phone, scheduled_at, 
          datetime.now().isoformat(), recurrence_pattern, priority, notes))
    reminder_id = c.lastrowid
    conn.commit()
    conn.close()
    return reminder_id


def update_reminder(reminder_id, **kwargs):
    """Update specific fields of a reminder"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    updates = []
    values = []
    for key, value in kwargs.items():
        updates.append(f"{key} = ?")
        values.append(value)
    
    values.append(reminder_id)
    query = f"UPDATE reminders SET {', '.join(updates)} WHERE id = ?"
    
    c.execute(query, values)
    conn.commit()
    conn.close()


def delete_reminder(execution_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Archive to history before deleting
    c.execute("""
        INSERT INTO reminder_history (reminder_id, objective, user, scheduled_at, 
                                     triggered_at, status, notes)
        SELECT id, objective, user, scheduled_at, datetime('now'), 'cancelled', notes
        FROM reminders WHERE execution_id = ?
    """, (execution_id,))
    
    c.execute("DELETE FROM reminders WHERE execution_id = ?", (execution_id,))
    conn.commit()
    conn.close()


def delete_all_user_reminders(user_identifier):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Archive to history
    c.execute("""
        INSERT INTO reminder_history (reminder_id, objective, user, scheduled_at, 
                                     triggered_at, status, notes)
        SELECT id, objective, user, scheduled_at, datetime('now'), 'cancelled', 'bulk cancel'
        FROM reminders WHERE (user = ? OR phone = ?) AND status = 'active'
    """, (user_identifier, user_identifier))
    
    c.execute("""
        DELETE FROM reminders
        WHERE (user = ? OR phone = ?) AND status = 'active'
    """, (user_identifier, user_identifier))
    conn.commit()
    conn.close()


def fetch_user_reminders_with_ids(user_identifier):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, objective, execution_id, scheduled_at, priority, recurrence_pattern
        FROM reminders
        WHERE (user = ? OR phone = ?) AND status = 'active'
        ORDER BY scheduled_at
    """, (user_identifier, user_identifier))
    rows = c.fetchall()
    conn.close()
    return rows


def fetch_all_reminders(user_identifier):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT objective, scheduled_at, priority, recurrence_pattern
        FROM reminders
        WHERE (user = ? OR phone = ?) AND status = 'active'
        ORDER BY scheduled_at
    """, (user_identifier, user_identifier))

    rows = c.fetchall()
    conn.close()

    future_rows = []
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))

    for objective, scheduled_at, priority, recurrence in rows:
        parsed = dateparser.parse(
            scheduled_at,
            languages=["en", "hi"],
            settings={
                "TIMEZONE": "Asia/Kolkata",
                "TO_TIMEZONE": "Asia/Kolkata",
                "RETURN_AS_TIMEZONE_AWARE": True,
            }
        )

        if parsed and parsed > now_ist:
            future_rows.append((objective, scheduled_at, priority, recurrence))

    return future_rows


def get_reminder_by_id(reminder_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, objective, execution_id, scheduled_at, priority, 
               recurrence_pattern, phone, user
        FROM reminders WHERE id = ?
    """, (reminder_id,))
    row = c.fetchone()
    conn.close()
    return row


# ================= GEMINI AI PARSER =================
def parse_reminder_with_gemini(message: str, user: str) -> Dict:
    """Use Gemini to parse reminder intent, datetime, and details"""
    
    current_time = datetime.now(ZoneInfo("Asia/Kolkata"))
    
    prompt = f"""You are a reminder parsing assistant. Parse the following message and extract reminder details.

Current datetime: {current_time.strftime("%Y-%m-%d %H:%M %A")}
User timezone: Asia/Kolkata

Message: "{message}"

Extract and return a JSON object with these fields:
- intent: one of ["create_reminder", "cancel_reminder", "cancel_all", "list_reminders", "edit_reminder", "unknown"]
- objective: what the user wants to be reminded about (null if not applicable)
- datetime: ISO format datetime string (YYYY-MM-DD HH:MM) in Asia/Kolkata timezone
- recurrence: one of ["once", "daily", "weekly", "monthly", "yearly"] or null
- priority: one of ["low", "normal", "high", "urgent"] or null
- cancel_target: text describing what to cancel (null if not cancel intent)
- confidence: 0-1 score of how confident you are in the parsing

Rules:
1. Support relative times: "in 2 hours", "tomorrow morning", "next Monday at 5pm", "in 30 minutes"
2. Support hinglish: "kal subah" = tomorrow morning, "aaj shaam" = today evening
3. Default time: if no time specified, use 9:00 AM for morning, 6:00 PM for evening
4. If time is in past, assume next occurrence (tomorrow/next week)
5. Recurrence: "every day" = daily, "weekly" = weekly, "every monday" = weekly, etc.
6. Priority: "urgent" or "important" = high, "asap" = urgent, default = normal

Examples:
"remind me to call mom tomorrow at 5pm" -> {{"intent": "create_reminder", "objective": "call mom", "datetime": "[tomorrow 17:00]", "recurrence": "once", "priority": "normal", "confidence": 0.95}}
"set reminder for gym every day at 6am" -> {{"intent": "create_reminder", "objective": "gym", "datetime": "[tomorrow 06:00]", "recurrence": "daily", "priority": "normal", "confidence": 0.9}}
"cancel gym reminder" -> {{"intent": "cancel_reminder", "cancel_target": "gym", "confidence": 0.85}}
"kal subah 7 baje meeting yaad dilana" -> {{"intent": "create_reminder", "objective": "meeting", "datetime": "[tomorrow 07:00]", "recurrence": "once", "priority": "normal", "confidence": 0.88}}

Return only the JSON object, no other text."""

    try:
        response = model.generate_content(prompt)
        result_text = response.text.strip()
        
        # Remove markdown code blocks if present
        result_text = re.sub(r'^```json\s*|\s*```$', '', result_text, flags=re.MULTILINE)
        
        result = json.loads(result_text)
        
        # Validate required fields
        if 'intent' not in result:
            result['intent'] = 'unknown'
        if 'confidence' not in result:
            result['confidence'] = 0.5
            
        logger.info(f"Gemini parsed: {result}")
        return result
        
    except Exception as e:
        logger.error(f"Gemini parsing error: {e}")
        return {
            'intent': 'unknown',
            'objective': None,
            'datetime': None,
            'recurrence': None,
            'priority': None,
            'confidence': 0,
            'error': str(e)
        }


# ================= RECURRENCE HANDLER =================
def calculate_next_occurrence(base_datetime: datetime, pattern: str) -> datetime:
    """Calculate next occurrence based on recurrence pattern"""
    if pattern == "daily":
        return base_datetime + timedelta(days=1)
    elif pattern == "weekly":
        return base_datetime + timedelta(weeks=1)
    elif pattern == "monthly":
        # Add approximately 1 month
        next_month = base_datetime.month + 1
        next_year = base_datetime.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        try:
            return base_datetime.replace(year=next_year, month=next_month)
        except ValueError:
            # Handle day overflow (e.g., Jan 31 -> Feb 31)
            return base_datetime.replace(year=next_year, month=next_month, day=28)
    elif pattern == "yearly":
        return base_datetime.replace(year=base_datetime.year + 1)
    else:
        return None


def reschedule_recurring_reminder(reminder_id: int):
    """Reschedule a recurring reminder after it fires"""
    reminder = get_reminder_by_id(reminder_id)
    if not reminder:
        return
    
    _, objective, old_execution_id, scheduled_at, priority, recurrence, phone, user = reminder
    
    if not recurrence or recurrence == "once":
        return
    
    # Calculate next occurrence
    current_dt = dateparser.parse(scheduled_at)
    next_dt = calculate_next_occurrence(current_dt, recurrence)
    
    if next_dt:
        next_str = next_dt.strftime("%Y-%m-%d %H:%M")
        scheduled_iso = (next_dt - timedelta(hours=5, minutes=30)).isoformat()
        
        # Trigger new call
        result = trigger_call(user, objective, phone, scheduled_iso)
        new_execution_id = result.get("execution_id")
        
        # Update reminder with new execution_id and scheduled time
        update_reminder(
            reminder_id,
            execution_id=new_execution_id,
            scheduled_at=next_str,
            last_triggered=datetime.now().isoformat()
        )
        
        logger.info(f"Rescheduled recurring reminder {reminder_id} to {next_str}")


# ================= FUZZY MATCHING =================
def normalize_text(text):
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def find_best_matching_reminder(user_identifier, cancel_text) -> Optional[Tuple]:
    """Find best matching reminder with suggestions if score is medium"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, objective, execution_id, scheduled_at
        FROM reminders
        WHERE (user = ? OR phone = ?) AND status = 'active'
    """, (user_identifier, user_identifier))
    reminders = c.fetchall()
    conn.close()

    if not reminders:
        return None

    cancel_norm = normalize_text(cancel_text)
    matches = []

    for rem_id, obj, exec_id, sched in reminders:
        obj_norm = normalize_text(obj)
        score = similarity(cancel_norm, obj_norm)
        matches.append((score, rem_id, obj, exec_id, sched))

    # Sort by score descending
    matches.sort(reverse=True, key=lambda x: x[0])
    
    best_score = matches[0][0]
    
    if best_score >= 0.7:
        # High confidence match
        return ('exact', matches[0][1:])
    elif best_score >= 0.4:
        # Medium confidence - return top 3 suggestions
        suggestions = matches[:3]
        return ('suggestions', [(m[1], m[2], m[3], m[4]) for m in suggestions])
    else:
        return None


# ================= BOLNA API =================
def trigger_call(user, objective, phone, schedule_iso):
    url = "https://api.bolna.ai/call"

    payload = {
        "agent_id": "fa97b4d6-1a23-4a76-901f-23248d2de793",
        "recipient_phone_number": phone,
        "scheduled_at": schedule_iso,
        "user_data": {
            "user": user,
            "objective": objective
        }
    }

    headers = {
        "Authorization": "Bearer bn-0e8607c5312643ffa50f22d80baba8e8",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        return response.json()
    except Exception as e:
        logger.error(f"Bolna API error: {e}")
        return {"message": str(e), "status": "failed"}


def cancel_bolna_call(execution_id):
    url = f"https://api.bolna.ai/call/{execution_id}/stop"
    headers = {
        "Authorization": "Bearer bn-0e8607c5312643ffa50f22d80baba8e8",
        "Content-Type": "application/json"
    }
    try:
        return requests.post(url, headers=headers, timeout=10)
    except Exception as e:
        logger.error(f"Bolna cancel error: {e}")
        return None


# ================= TELEGRAM LISTENER =================
@app.get("/start-listening")
async def start_listening():
    await client.start()

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):

        message_text = event.message.text or ""
        sender = await event.get_sender()

        username = getattr(sender, "username", None)
        tg_phone = getattr(sender, "phone", None)

        display_name = username if username else "Unknown"

        # ——— CHECK CONVERSATION STATE FIRST (BEFORE ANYTHING ELSE) ———
        conv_state = get_conversation_state(display_name)
        
        if conv_state:
            state_type = conv_state['state']
            state_data = conv_state['data']
            
            # Handle confirmation responses
            if state_type == 'confirm_cancel':
                user_response = message_text.lower().strip()
                
                if user_response in ['yes', 'y', 'confirm', 'ok', 'sure', 'haan', 'ha']:
                    reminder_id = state_data['reminder_id']
                    reminder = get_reminder_by_id(reminder_id)
                    
                    if reminder:
                        _, objective, execution_id, _, _, _, _, _ = reminder
                        
                        if execution_id:
                            cancel_bolna_call(execution_id)
                        
                        delete_reminder(execution_id)
                        await event.respond(f"✅ Cancelled: {objective}")
                        clear_conversation_state(display_name)
                    else:
                        await event.respond("❌ Reminder not found.")
                        clear_conversation_state(display_name)
                    return
                
                elif user_response in ['no', 'n', 'cancel', 'nahi', 'nope']:
                    await event.respond("✅ Kept the reminder.")
                    clear_conversation_state(display_name)
                    return
                else:
                    await event.respond("Please reply with 'yes' or 'no', or click the buttons above.")
                    return
            
            elif state_type == 'confirm_cancel_all':
                user_response = message_text.lower().strip()
                
                if user_response in ['yes', 'y', 'confirm', 'ok', 'sure', 'haan', 'ha']:
                    reminders = fetch_user_reminders_with_ids(display_name)
                    
                    for _, _, execution_id, _, _, _ in reminders:
                        if execution_id:
                            cancel_bolna_call(execution_id)

                    delete_all_user_reminders(display_name)
                    await event.respond(f"✅ Cancelled all {len(reminders)} reminders.")
                    clear_conversation_state(display_name)
                    return
                
                elif user_response in ['no', 'n', 'cancel', 'nahi', 'nope']:
                    await event.respond("✅ Kept all reminders.")
                    clear_conversation_state(display_name)
                    return
                else:
                    await event.respond("Please reply with 'yes' or 'no', or click the buttons above.")
                    return
            
            elif state_type == 'select_suggestion':
                # Handle numeric selection for suggestions
                user_response = message_text.strip()
                suggestions = state_data['suggestions']
                
                if user_response.isdigit():
                    index = int(user_response) - 1
                    
                    if 0 <= index < len(suggestions):
                        rem_id, objective, execution_id, scheduled = suggestions[index]
                        
                        # Ask for confirmation
                        set_conversation_state(display_name, 'confirm_cancel', {'reminder_id': rem_id})
                        
                        buttons = [
                            [Button.inline("✅ Yes, cancel it", f"cancel_{rem_id}".encode())],
                            [Button.inline("❌ No, keep it", b"cancel_action")]
                        ]
                        
                        await event.respond(
                            f"❓ Cancel this reminder?\n\n📝 {objective}\n⏰ {scheduled}",
                            buttons=buttons
                        )
                        return
                    else:
                        await event.respond("❌ Invalid number. Please select a valid option.")
                        return
                else:
                    await event.respond("Please reply with the number (1, 2, or 3) or click the buttons.")
                    return

        # ——— NOW CHECK PHONE VERIFICATION ———
        phone = None

        # ——— 1) USE TELEGRAM PHONE IF AVAILABLE ———
        if tg_phone and tg_phone.startswith("91"):
            phone = "+" + tg_phone
            save_or_update_user(display_name, phone)

        else:
            stored = get_user_phone(display_name)

            if stored:
                phone = stored

            else:
                # ——— USER IS PROVIDING PHONE ———
                if is_waiting_for_phone(display_name):

                    cleaned = re.sub(r"\D", "", message_text)

                    if not re.match(r"^91\d{10}$", cleaned):
                        await event.respond(
                            "Please send phone number in this format:\n\n91XXXXXXXXXX"
                        )
                        return

                    phone = "+" + cleaned
                    save_or_update_user(display_name, phone)
                    clear_pending_phone(display_name)

                    await event.respond(
                        f"✅ Saved your number: {phone}\n\nYou can now set reminders!"
                    )
                    return

                # ——— ASK FOR PHONE ———
                else:
                    set_pending_phone(display_name)
                    await event.respond(
                        "📱 To schedule calls, please send your phone number:\n\n"
                        "Format: 91XXXXXXXXXX\n"
                        "Example: 919876543210"
                    )
                    return

        # ——— PARSE WITH GEMINI ———
        parsed = parse_reminder_with_gemini(message_text, display_name)
        intent = parsed.get('intent')
        
        # ——— HANDLE CANCEL ALL ———
        if intent == 'cancel_all':
            reminders = fetch_user_reminders_with_ids(display_name)

            if not reminders:
                await event.respond("❌ No active reminders to cancel.")
                return

            # Set conversation state
            set_conversation_state(display_name, 'confirm_cancel_all', None)
            
            buttons = [
                [Button.inline("✅ Yes, cancel all", b"confirm_cancel_all")],
                [Button.inline("❌ No, keep them", b"cancel_action")]
            ]

            await event.respond(
                f"⚠️ Are you sure you want to cancel all {len(reminders)} reminders?\n\n"
                f"Reply with 'yes' or 'no', or click a button.",
                buttons=buttons
            )
            return

        # ——— HANDLE LIST ———
        elif intent == 'list_reminders':
            reminders = fetch_all_reminders(display_name)

            if not reminders:
                await event.respond("📭 No upcoming reminders.")
                return

            reply = "📋 *Your Upcoming Reminders:*\n\n"
            for i, (obj, sched, priority, recurrence) in enumerate(reminders, start=1):
                priority_icon = "🔴" if priority == "urgent" else "🟡" if priority == "high" else "🟢"
                recurrence_text = f" ({recurrence})" if recurrence and recurrence != "once" else ""
                reply += f"{priority_icon} {i}. {obj}\n   ⏰ {sched}{recurrence_text}\n\n"

            await event.respond(reply)
            return

        # ——— HANDLE CANCEL SPECIFIC ———
        elif intent == 'cancel_reminder':
            cancel_target = parsed.get('cancel_target', message_text)
            match_result = find_best_matching_reminder(display_name, cancel_target)

            if not match_result:
                await event.respond("❌ Could not find a matching reminder.\n\nUse /list to see all reminders.")
                return

            match_type, data = match_result

            if match_type == 'exact':
                rem_id, objective, execution_id, scheduled = data
                
                # Set conversation state
                set_conversation_state(display_name, 'confirm_cancel', {'reminder_id': rem_id})
                
                buttons = [
                    [Button.inline("✅ Yes, cancel it", f"cancel_{rem_id}".encode())],
                    [Button.inline("❌ No, keep it", b"cancel_action")]
                ]

                await event.respond(
                    f"❓ Cancel this reminder?\n\n📝 {objective}\n⏰ {scheduled}\n\n"
                    f"Reply with 'yes' or 'no', or click a button.",
                    buttons=buttons
                )
                return

            elif match_type == 'suggestions':
                # Multiple possible matches
                # Set conversation state
                set_conversation_state(display_name, 'select_suggestion', {'suggestions': data})
                
                reply = "🤔 Did you mean one of these?\n\n"
                buttons = []
                
                for i, (rem_id, obj, exec_id, sched) in enumerate(data, 1):
                    reply += f"{i}. {obj} — {sched}\n"
                    buttons.append([Button.inline(f"Cancel #{i}", f"cancel_{rem_id}".encode())])
                
                buttons.append([Button.inline("❌ None of these", b"cancel_action")])
                
                reply += "\nReply with the number (1, 2, 3) or click a button."
                
                await event.respond(reply, buttons=buttons)
                return

        # ——— CREATE NEW REMINDER ———
        elif intent == 'create_reminder':
            objective = parsed.get('objective')
            dt_str = parsed.get('datetime')
            recurrence = parsed.get('recurrence', 'once')
            priority = parsed.get('priority', 'normal')
            confidence = parsed.get('confidence', 0)

            if confidence < 0.6:
                await event.respond(
                    "🤔 I'm not sure I understood that correctly.\n\n"
                    "Please try again with format:\n"
                    "• remind me to [task] at [time]\n"
                    "• [task] tomorrow at 5pm\n"
                    "• every day at 9am [task]"
                )
                return

            if not dt_str or not objective:
                await event.respond(
                    "⚠️ I need both a task and a time.\n\n"
                    "Example: 'remind me to call mom tomorrow at 5pm'"
                )
                return

            # Parse datetime
            parsed_dt = dateparser.parse(
                dt_str,
                languages=["en", "hi"],
                settings={
                    "TIMEZONE": "Asia/Kolkata",
                    "TO_TIMEZONE": "Asia/Kolkata",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                }
            )

            if not parsed_dt:
                await event.respond("❌ Could not understand the time. Please try again.")
                return

            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))

            if parsed_dt <= now_ist:
                await event.respond("⚠️ Reminders must be scheduled for the future.")
                return

            # Convert to UTC for Bolna API (subtract 5:30)
            scheduled_iso = (parsed_dt.replace(tzinfo=None) - timedelta(hours=5, minutes=30)).isoformat()

            # Trigger call
            result = trigger_call(display_name, objective, phone, scheduled_iso)
            
            if result.get("status") == "failed":
                await event.respond(
                    f"❌ Failed to schedule reminder: {result.get('message', 'Unknown error')}"
                )
                return

            execution_id = result.get("execution_id")
            scheduled_str = parsed_dt.strftime("%Y-%m-%d %H:%M")

            # Save to database
            save_reminder(
                objective, 
                execution_id, 
                display_name, 
                phone, 
                scheduled_str,
                recurrence_pattern=recurrence if recurrence != 'once' else None,
                priority=priority
            )

            # Format response
            priority_icon = "🔴" if priority == "urgent" else "🟡" if priority == "high" else "🟢"
            recurrence_text = f"\n🔁 Repeats: {recurrence}" if recurrence != "once" else ""
            
            await event.respond(
                f"✅ Reminder set!\n\n"
                f"{priority_icon} {objective}\n"
                f"⏰ {parsed_dt.strftime('%A, %B %d at %I:%M %p')}"
                f"{recurrence_text}"
            )

        # ——— UNKNOWN INTENT ———
        else:
            await event.respond(
                "🤷‍♂️ I didn't understand that.\n\n"
                "*Available commands:*\n"
                "• Set reminder: 'remind me to [task] at [time]'\n"
                "• List: 'show my reminders' or /list\n"
                "• Cancel: 'cancel [task]'\n"
                "• Cancel all: 'cancel all reminders'"
            )

    # ——— HANDLE BUTTON CALLBACKS ———
    @client.on(events.CallbackQuery())
    async def callback_handler(event):
        data = event.data.decode()
        sender = await event.get_sender()
        username = getattr(sender, "username", "Unknown")
        
        if data == "cancel_action":
            clear_conversation_state(username)
            await event.edit("✅ Action cancelled.")
            return
        
        if data == "confirm_cancel_all":
            sender = await event.get_sender()
            username = getattr(sender, "username", "Unknown")
            
            reminders = fetch_user_reminders_with_ids(username)
            
            for _, _, execution_id, _, _, _ in reminders:
                if execution_id:
                    cancel_bolna_call(execution_id)

            delete_all_user_reminders(username)
            clear_conversation_state(username)
            await event.edit(f"✅ Cancelled all {len(reminders)} reminders.")
            return
        
        if data.startswith("cancel_"):
            reminder_id = int(data.split("_")[1])
            reminder = get_reminder_by_id(reminder_id)
            
            if reminder:
                _, objective, execution_id, scheduled, _, _, _, _ = reminder
                
                if execution_id:
                    cancel_bolna_call(execution_id)
                
                delete_reminder(execution_id)
                clear_conversation_state(username)
                await event.edit(f"✅ Cancelled: {objective}")
            else:
                await event.edit("❌ Reminder not found.")
            return

    client.loop.create_task(client.run_until_disconnected())
    return {"status": "running"}


# ================== HEALTH CHECK ==================
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }

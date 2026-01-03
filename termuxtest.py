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
from typing import Optional, Dict, List, Tuple
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
gemini_api_key = os.getenv("GEMINI_API_KEY")

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
    conversation_states[username] = {
        'state': state,
        'data': data,
        'timestamp': datetime.now()
    }

def get_conversation_state(username):
    state = conversation_states.get(username)
    if state:
        if datetime.now() - state['timestamp'] > timedelta(minutes=5):
            clear_conversation_state(username)
            return None
    return state

def clear_conversation_state(username):
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


# ================= FETCH REMINDERS =================
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


# ================= RECURRENCE =================
def calculate_next_occurrence(base_datetime: datetime, pattern: str) -> datetime:
    if pattern == "daily":
        return base_datetime + timedelta(days=1)
    elif pattern == "weekly":
        return base_datetime + timedelta(weeks=1)
    elif pattern == "monthly":
        next_month = base_datetime.month + 1
        next_year = base_datetime.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        try:
            return base_datetime.replace(year=next_year, month=next_month)
        except ValueError:
            return base_datetime.replace(year=next_year, month=next_month, day=28)
    elif pattern == "yearly":
        return base_datetime.replace(year=base_datetime.year + 1)
    else:
        return None


# ================= FUZZY MATCH HELPERS =================
def normalize_text(text):
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def find_best_matching_reminder(user_identifier, cancel_text):
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

    matches.sort(reverse=True, key=lambda x: x[0])

    best_score = matches[0][0]

    if best_score >= 0.7:
        return ('exact', matches[0][1:])
    elif best_score >= 0.4:
        suggestions = matches[:3]
        return ('suggestions', [(m[1], m[2], m[3], m[4]) for m in suggestions])
    else:
        return None


# ================= GEMINI REST HELPER =================
GEMINI_MODEL = "gemini-2.0-flash"

def gemini_generate(prompt: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={gemini_api_key}"

    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ]
    }

    try:
        res = requests.post(url, json=payload, timeout=30)
        res.raise_for_status()
        data = res.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return None


# ================= GEMINI AI PARSER =================
def parse_reminder_with_gemini(message: str, user: str):
    current_time = datetime.now(ZoneInfo("Asia/Kolkata"))

    prompt = f"""
You are a reminder parsing assistant and a polite AI helper.

Your ONLY job is to return VALID JSON.

Current datetime: {current_time.strftime("%Y-%m-%d %H:%M")}
Timezone: Asia/Kolkata

Message: "{message}"

Return JSON with exactly these keys:

  "intent": one of ["create_reminder","cancel_reminder","cancel_all","list_reminders","greeting","help","unknown"]
  "objective": string or null
  "datetime": string (YYYY-MM-DD HH:MM) or null
  "recurrence": one of ["once","daily","weekly","monthly","yearly"] or null
  "priority": one of ["low","normal","high","urgent"] or null
  "cancel_target": string or null
  "ai_reply": string or null
  "confidence": number 0–1

Rules:
- If user says "hi", "hello", "how are you", "weather", or makes small talk, intent = "greeting".
    - Populate "ai_reply" with a polite, short response.
    - Be friendly but remind them you are a "Call Reminder AI" and don't track weather/news but can schedule calls.
- If user says "help", "options", "commands", "menu" or ambiguous 1-2 words like "what do", intent = "help".
- If user is clearly creating reminder, intent = "create_reminder"
- If saying cancel all reminders, use "cancel_all"
- If saying show/list, use "list_reminders"
- If cancelling a specific task, use "cancel_reminder"
- If unsure, use "unknown"
- If no explicit time but clear date, choose reasonable default:
    morning = 09:00
    evening = 18:00
- If time is past, move to next day automatically
- Do NOT use markdown or ``` in the response.
Return ONLY JSON.
"""

    raw = gemini_generate(prompt)

    if not raw:
        return {
            "intent": "unknown",
            "objective": None,
            "datetime": None,
            "recurrence": None,
            "priority": None,
            "cancel_target": None,
            "ai_reply": None,
            "confidence": 0
        }

    try:
        # remove accidental code formatting
        raw = raw.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        result = json.loads(raw)

        # defaults fallback
        result.setdefault("intent", "unknown")
        result.setdefault("confidence", 0)

        return result

    except Exception as e:
        logger.error(f"GEMINI JSON PARSE ERROR: {e}\nRAW: {raw}")

        return {
            "intent": "unknown",
            "objective": None,
            "datetime": None,
            "recurrence": None,
            "priority": None,
            "cancel_target": None,
            "ai_reply": None,
            "confidence": 0
        }

# ================= BOLNA API =================
def trigger_call(user, objective, phone, schedule_iso):
    url = "[https://api.bolna.ai/call](https://api.bolna.ai/call)"

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
        return {"status": "failed", "message": str(e)}


def cancel_bolna_call(execution_id):
    url = f"[https://api.bolna.ai/call/](https://api.bolna.ai/call/){execution_id}/stop"
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

        # ——— CHECK CONVERSATION STATE ———
        conv_state = get_conversation_state(display_name)

        if conv_state:
            state_type = conv_state['state']
            state_data = conv_state['data']

            # Confirm cancel single reminder
            if state_type == 'confirm_cancel':
                user_response = message_text.lower().strip()

                if user_response in ['yes', 'y', 'ok', 'haan', 'ha', 'confirm']:
                    reminder_id = state_data['reminder_id']
                    reminder = get_reminder_by_id(reminder_id)

                    if reminder:
                        _, objective, execution_id, _, _, _, _, _ = reminder

                        if execution_id:
                            cancel_bolna_call(execution_id)

                        delete_reminder(execution_id)
                        await event.respond(f"✅ Cancelled: {objective}")
                    else:
                        await event.respond("❌ Reminder not found.")

                    clear_conversation_state(display_name)
                    return

                elif user_response in ['no', 'n', 'cancel', 'nahi']:
                    await event.respond("👍 Okay, keeping the reminder.")
                    clear_conversation_state(display_name)
                    return
                else:
                    await event.respond("Please reply with yes/no.")
                    return

            # Confirm cancel all reminders
            if state_type == 'confirm_cancel_all':
                user_response = message_text.lower().strip()

                if user_response in ['yes', 'y', 'ok', 'haan', 'ha']:
                    reminders = fetch_user_reminders_with_ids(display_name)

                    for _, _, execution_id, _, _, _ in reminders:
                        if execution_id:
                            cancel_bolna_call(execution_id)

                    delete_all_user_reminders(display_name)
                    await event.respond("✅ All reminders cancelled.")

                    clear_conversation_state(display_name)
                    return

                elif user_response in ['no', 'n', 'cancel', 'nahi']:
                    await event.respond("👍 Okay, keeping all reminders.")
                    clear_conversation_state(display_name)
                    return

        # ——— PHONE HANDLING ———
        phone = None

        if tg_phone and tg_phone.startswith("91"):
            phone = "+" + tg_phone
            save_or_update_user(display_name, phone)

        else:
            stored = get_user_phone(display_name)

            if stored:
                phone = stored
            else:
                if is_waiting_for_phone(display_name):
                    cleaned = re.sub(r"\D", "", message_text)

                    if not re.match(r"^91\d{10}$", cleaned):
                        await event.respond("Send phone like: 91XXXXXXXXXX")
                        return

                    phone = "+" + cleaned
                    save_or_update_user(display_name, phone)
                    clear_pending_phone(display_name)

                    await event.respond(f"Saved: {phone}\nNow send your reminder.")
                    return
                else:
                    set_pending_phone(display_name)
                    await event.respond("Please send your phone number (91XXXXXXXXXX).")
                    return

        # ——— PARSE MESSAGE WITH GEMINI ———
        parsed = parse_reminder_with_gemini(message_text, display_name)
        intent = parsed.get("intent")

        # HELP INTENT
        if intent == "help":
            await event.respond(
                "🤖 **I am your Call Reminder AI.**\n\n"
                "Here is what I can do:\n"
                "1️⃣ **Set a Call:** 'Remind me to call John at 5pm'\n"
                "2️⃣ **List Tasks:** 'Show my reminders'\n"
                "3️⃣ **Cancel:** 'Cancel the meeting reminder'\n"
                "4️⃣ **Clear All:** 'Delete all reminders'"
            )
            return

        # GREETING INTENT
        if intent == "greeting":
            reply = parsed.get("ai_reply") or "Hello! I am ready to schedule your calls."
            await event.respond(reply)
            return

        # CANCEL ALL
        if intent == "cancel_all":
            reminders = fetch_user_reminders_with_ids(display_name)

            if not reminders:
                await event.respond("No reminders found.")
                return

            set_conversation_state(display_name, "confirm_cancel_all", None)

            await event.respond(
                f"⚠️ Cancel ALL {len(reminders)} reminders?\nReply yes / no"
            )
            return

        # LIST REMINDERS
        if intent == "list_reminders":
            reminders = fetch_all_reminders(display_name)

            if not reminders:
                await event.respond("📭 No upcoming reminders.")
                return

            reply = "📋 Upcoming reminders:\n\n"
            for i, (obj, sched, priority, recurrence) in enumerate(reminders, start=1):
                recurrence_text = f" ({recurrence})" if recurrence else ""
                reply += f"{i}) {obj}\n   {sched}{recurrence_text}\n\n"

            await event.respond(reply)
            return

        # CANCEL SPECIFIC
        if intent == "cancel_reminder":
            target = parsed.get("cancel_target", message_text)

            match = find_best_matching_reminder(display_name, target)

            if not match:
                await event.respond("Could not find similar reminder. Try /list")
                return

            match_type, data = match

            if match_type == "exact":
                rem_id, objective, execution_id, sched = data

                set_conversation_state(display_name, "confirm_cancel", {
                    "reminder_id": rem_id
                })

                await event.respond(
                    f"Cancel this?\n\n{objective}\n{sched}\n\nReply yes / no"
                )
                return

            elif match_type == "suggestions":
                reply = "Did you mean:\n\n"
                for i, (rem_id, obj, exec_id, sched) in enumerate(data, 1):
                    reply += f"{i}) {obj} — {sched}\n"
                reply += "\nReply with cancel text again if none match."

                await event.respond(reply)
                return

        # CREATE REMINDER
        if intent == "create_reminder":
            objective = parsed.get("objective")
            dt_str = parsed.get("datetime")

            if not objective or not dt_str:
                await event.respond("Need task + time.")
                return

            parsed_dt = dateparser.parse(
                dt_str,
                settings={
                    "TIMEZONE": "Asia/Kolkata",
                    "TO_TIMEZONE": "Asia/Kolkata",
                    "RETURN_AS_TIMEZONE_AWARE": True
                }
            )

            if not parsed_dt:
                await event.respond("Could not understand time.")
                return

            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))

            if parsed_dt <= now_ist:
                await event.respond("Time must be in future.")
                return

            scheduled_iso = (parsed_dt.replace(tzinfo=None) - timedelta(hours=5, minutes=30)).isoformat()

            result = trigger_call(display_name, objective, phone, scheduled_iso)
            execution_id = result.get("execution_id")
            sched_str = parsed_dt.strftime("%Y-%m-%d %H:%M")

            save_reminder(objective, execution_id, display_name, phone, sched_str)

            await event.respond(f"Reminder set:\n{objective}\n⏰ {sched_str}")
            return

        # UNKNOWN
        await event.respond("I did not understand. Try:\n'remind me to drink water at 3pm'")

    # ================= CALLBACK HANDLER =================
    @client.on(events.CallbackQuery())
    async def callback_handler(event):
        data = event.data.decode()
        sender = await event.get_sender()
        username = getattr(sender, "username", "Unknown")

        if data == "cancel_action":
            clear_conversation_state(username)
            await event.edit("Action cancelled.")
            return

    client.loop.create_task(client.run_until_disconnected())
    return {"status": "running"}


# ================= HEALTH =================
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

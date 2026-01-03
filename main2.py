from fastapi import FastAPI
from telethon import TelegramClient, events
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

load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")

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
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            phone TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_phone (
            username TEXT PRIMARY KEY
        )
    """)

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


# ================= REMINDER DB =================
def save_reminder(objective, execution_id, user, phone, scheduled_at):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO reminders (objective, execution_id, user, phone, scheduled_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (objective, execution_id, user, phone, scheduled_at, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def delete_reminder(execution_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE execution_id = ?", (execution_id,))
    conn.commit()
    conn.close()


def delete_all_user_reminders(user_identifier):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        DELETE FROM reminders
        WHERE user = ? OR phone = ?
    """, (user_identifier, user_identifier))
    conn.commit()
    conn.close()


def fetch_user_reminders_with_ids(user_identifier):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT objective, execution_id, scheduled_at
        FROM reminders
        WHERE user = ? OR phone = ?
    """, (user_identifier, user_identifier))
    rows = c.fetchall()
    conn.close()
    return rows


def fetch_all_reminders(user_identifier):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT objective, scheduled_at
        FROM reminders
        WHERE user = ? OR phone = ?
    """, (user_identifier, user_identifier))

    rows = c.fetchall()
    conn.close()

    future_rows = []
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))

    for objective, scheduled_at in rows:
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
            future_rows.append((objective, scheduled_at))

    return future_rows


# ================= UTILS =================
def normalize_text(text):
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


# ================= HINGLISH NORMALIZER =================
def hinglish_normalize(msg: str):
    msg = msg.lower()

    combo_patterns = {
        r"aaj\s+(shaam|sham|raat|rat|subah|sobah)": "today",
        r"kal\s+(shaam|sham|raat|rat|subah|sobah)": "tomorrow",
    }

    for pat, rep in combo_patterns.items():
        msg = re.sub(pat, rep, msg)

    replacements = {
        r"\bkal\b": "tomorrow",
        r"\baaj\b": "today",
        r"\bsubah|sobah\b": "morning",
        r"\bshaam|sham\b": "evening",
        r"\braat|rat\b": "night",
        r"\bbaje|bajje|bj\b": "pm",
        r"\bye|yaad dila|yaad dilao\b": "remind",
        r"\blaga do|laga dena|set kar\b": "set reminder",
    }

    for pat, rep in replacements.items():
        msg = re.sub(pat, rep, msg)

    return msg


# ================= EXTRACT AI FIELDS (FIXED) =================
def extract_ai_fields(sender_name: str, message: str):
    original = message
    message = hinglish_normalize(message)

    user = sender_name
    lower = message.lower()

    explicit_today = "today" in lower
    explicit_tomorrow = "tomorrow" in lower

    time_patterns = [
        r"\d{1,2}:\d{2}\s*(am|pm)?",
        r"\d{1,2}\s*(am|pm)",
        r"at\s+\d{1,2}:\d{2}\s*(am|pm)?",
        r"at\s+\d{1,2}\s*(am|pm)"
    ]

    time_text = None
    for p in time_patterns:
        m = re.search(p, message, re.IGNORECASE)
        if m:
            time_text = m.group(0)
            break

    parsed = None
    if time_text:
        parsed = dateparser.parse(
            time_text,
            languages=["en", "hi"],
            settings={
                "TIMEZONE": "Asia/Kolkata",
                "TO_TIMEZONE": "Asia/Kolkata",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )

    if parsed:
        if explicit_today:
            parsed = parsed.replace(
                year=datetime.now().year,
                month=datetime.now().month,
                day=datetime.now().day
            )
        elif explicit_tomorrow:
            t = datetime.now() + timedelta(days=1)
            parsed = parsed.replace(
                year=t.year,
                month=t.month,
                day=t.day
            )
        elif parsed <= datetime.now():
            parsed = parsed + timedelta(days=1)

    datetime_str = parsed.strftime("%Y-%m-%d %H:%M") if parsed else None

    text = original
    text = re.sub(r"\b(today|tomorrow)\b", "", text, flags=re.IGNORECASE)

    if time_text:
        text = text.replace(time_text, "")

    objective_patterns = [
        r"(?:set\s+(?:a\s+)?.*reminder\s+(?:to|for)\s+)(.+)",
        r"(?:remind\s+me\s+(?:to|for)\s+)(.+)",
        r"(?:note\s+to\s+)(.+)",
        r"(?:for\s+)(.+)"
    ]

    objective = None
    for p in objective_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            objective = m.group(1).strip()
            break

    if objective:
        objective = re.sub(
            r"\b(at|on|by)\b.*$",
            "",
            objective,
            flags=re.IGNORECASE
        ).strip()

    return {
        "user": user,
        "objective": objective,
        "datetime": datetime_str
    }


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

    response = requests.post(url, json=payload, headers=headers)

    try:
        return response.json()
    except:
        return {"message": "Unknown error", "status": "failed"}


def cancel_bolna_call(execution_id):
    url = f"https://api.bolna.ai/call/{execution_id}/stop"
    headers = {
        "Authorization": "Bearer bn-0e8607c5312643ffa50f22d80baba8e8",
        "Content-Type": "application/json"
    }
    return requests.post(url, headers=headers)


# ================= INTENTS =================
def is_cancel_all(message):
    m = message.lower()
    return any(x in m for x in [
        "cancel all",
        "delete all",
        "clear reminders"
    ])


def extract_cancel_objective(message):
    patterns = [
        r"cancel\s+(.+)",
        r"stop\s+(.+)",
        r"delete\s+(.+)",
        r"hata\s+(.+)"
    ]
    for p in patterns:
        m = re.search(p, message, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def is_list_request(message):
    m = message.lower()
    return any(x in m for x in [
        "list reminders",
        "show reminders",
        "my reminders",
        "list calls"
    ])


def find_best_matching_reminder(user_identifier, cancel_text):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT objective, execution_id
        FROM reminders
        WHERE user = ? OR phone = ?
    """, (user_identifier, user_identifier))
    reminders = c.fetchall()
    conn.close()

    if not reminders:
        return None

    cancel_norm = normalize_text(cancel_text)
    best_match = None
    best_score = 0

    for obj, exec_id in reminders:
        obj_norm = normalize_text(obj)
        score = similarity(cancel_norm, obj_norm)

        if score > best_score:
            best_score = score
            best_match = (obj, exec_id)

    return best_match if best_score >= 0.45 else None


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
                    cleaned = message_text.replace(" ", "")

                    if re.match(r"^\+?\d{10,15}$", cleaned):
                        save_or_update_user(display_name, cleaned)
                        clear_pending_phone(display_name)

                        await event.respond(
                            f"Saved your number: {cleaned}\nYou can continue setting reminders."
                        )
                        return

                    await event.respond(
                        "Send valid phone with country code:\n+919876543210"
                    )
                    return

                else:
                    set_pending_phone(display_name)
                    await event.respond(
                        "To schedule calls, send your phone number:\n\n+919876543210"
                    )
                    return

        # CANCEL ALL
        if is_cancel_all(message_text):
            reminders = fetch_user_reminders_with_ids(display_name)

            if not reminders:
                await event.respond("No reminders to cancel.")
                return

            for _, execution_id, _ in reminders:
                if execution_id:
                    cancel_bolna_call(execution_id)

            delete_all_user_reminders(display_name)
            await event.respond("All reminders cancelled.")
            return

        # LIST
        if is_list_request(message_text):
            reminders = fetch_all_reminders(display_name)

            if not reminders:
                await event.respond("No upcoming reminders.")
                return

            reply = "Upcoming reminders:\n\n"
            for i, (obj, sched) in enumerate(reminders, start=1):
                reply += f"{i}) {obj} — {sched}\n"

            await event.respond(reply)
            return

        # CANCEL SPECIFIC
        cancel_obj = extract_cancel_objective(message_text)
        if cancel_obj:
            match = find_best_matching_reminder(display_name, cancel_obj)

            if not match:
                await event.respond("Could not find matching reminder.")
                return

            objective, execution_id = match

            if execution_id:
                cancel_bolna_call(execution_id)

            delete_reminder(execution_id)
            await event.respond(f"Cancelled: {objective}")
            return

        # NEW REMINDER
        extracted = extract_ai_fields(display_name, message_text)

        user = extracted["user"]
        objective = extracted["objective"]
        dt = extracted["datetime"]

        if not dt:
            await event.respond("I could not understand the time.")
            return

        parsed_dt = datetime.strptime(dt, "%Y-%m-%d %H:%M")
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        parsed_ist = parsed_dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))

        if parsed_ist <= now_ist:
            await event.respond("Calls must be scheduled for future.")
            return

        scheduled_iso = (parsed_dt - timedelta(hours=5, minutes=30)).isoformat()

        result = trigger_call(user, objective, phone, scheduled_iso)
        execution_id = result.get("execution_id")

        save_reminder(objective, execution_id, user, phone, dt)

        await event.respond(f"Reminder set for {dt}\n{objective}")

    client.loop.create_task(client.run_until_disconnected())
    return {"status": "running"}

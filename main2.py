from fastapi import FastAPI
from telethon import TelegramClient, events
import re
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import dateparser
import requests
from zoneinfo import ZoneInfo   # <--- important for timezone fix

load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")

app = FastAPI()
client = TelegramClient('session_name', api_id, api_hash)


# ================== AI EXTRACTION ==================
def extract_ai_fields(sender_name: str, message: str):
    user = sender_name

    lower_msg = message.lower()

    # detect day hints
    is_tomorrow = "tomorrow" in lower_msg
    is_today = "today" in lower_msg

    # -------- TIME PHRASE DETECTION --------
    time_patterns = [
        r"(at\s+[0-9: ]+\s*(am|pm))",
        r"(\d{1,2}:\d{2}\s*(am|pm)?)",
        r"(\d{1,2}\s*(am|pm))"
    ]

    time_text = None
    for p in time_patterns:
        m = re.search(p, message, re.IGNORECASE)
        if m:
            time_text = m.group(0)
            break

    parsed_date = None

    if time_text:
        parsed_date = dateparser.parse(
            time_text,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": "Asia/Kolkata",
                "TO_TIMEZONE": "Asia/Kolkata",
                "RETURN_AS_TIMEZONE_AWARE": False
            }
        )

    # apply day modifiers
    if parsed_date:
        if is_tomorrow:
            parsed_date = parsed_date + timedelta(days=1)
        elif is_today:
            parsed_date = parsed_date

    datetime_str = parsed_date.strftime("%Y-%m-%d %H:%M") if parsed_date else None

    # -------- OBJECTIVE EXTRACTION --------
    text_for_objective = message
    if time_text:
        text_for_objective = text_for_objective.replace(time_text, "")

    objective_patterns = [
        r"(?:set|create|make)\s+(?:a\s+)?reminder\s*(?:to|for)?\s*(.+)",
        r"(?:remind\s+me\s+(?:to|for)\s+)(.+)",
        r"(?:note\s+to)\s+(.+)",
    ]

    objective = None
    for p in objective_patterns:
        match = re.search(p, text_for_objective, re.IGNORECASE)
        if match:
            objective = match.group(1).strip()
            break

    if objective:
        objective = re.sub(
            r"\b(at|on|by|today|tomorrow)\b.*$",
            "",
            objective,
            flags=re.IGNORECASE
        ).strip()

    # fallback like “for calling mom”
    if not objective:
        fallback = re.search(r"\bfor\s+(.+)", message, re.IGNORECASE)
        if fallback:
            objective = fallback.group(1).strip()

    return {
        "user": user,
        "objective": objective,
        "datetime": datetime_str
    }


# ================== BOLNA CALL FUNCTION ==================
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
        "Authorization": "Bearer ",
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=payload, headers=headers)
    print("Bolna Response:", response.text)
    return response


# ================== TELEGRAM LISTENER ==================
@app.get("/start-listening")
async def start_listening():
    await client.start()

    print("Listening to ALL chats, groups, channels, and DMs...")

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        sender = await event.get_sender()

        username = getattr(sender, "username", None)
        phone = getattr(sender, "phone", None)

        display_name = username if username else (phone if phone else "Unknown")

        phone_for_call = None
        if phone and phone.startswith("91"):
            phone_for_call = "+" + phone

        message_text = event.message.text or ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        extracted = extract_ai_fields(display_name, message_text)

        # URLs — only log
        urls = re.findall(r'(https?://[^\s]+)', message_text)
        if urls:
            for url in urls:
                print(f"[{now}] [{display_name}] URL found: {url}")

        print("\n--- NEW MESSAGE ---")
        print(f"Display name: {display_name}")
        print(f"Raw message: {message_text}")
        print(f"AI Extracted -> {extracted}")

        # ---------- ONLY CALL IF ALL VALUES EXIST ----------
        user = extracted["user"]
        objective = extracted["objective"]
        dt = extracted["datetime"]

        scheduled_iso = None
        if dt:
            parsed_dt = datetime.strptime(dt, "%Y-%m-%d %H:%M")

            # Keep -5:30 conversion
            adjusted_dt = parsed_dt - timedelta(hours=5, minutes=30)
            scheduled_iso = adjusted_dt.isoformat()

        if user and objective and scheduled_iso and phone_for_call:
            print("Triggering Bolna call...")
            trigger_call(user, objective, phone_for_call, scheduled_iso)
        else:
            print("Skipping Bolna call — missing required data")
            print({
                "user": user,
                "objective": objective,
                "datetime": scheduled_iso,
                "phone": phone_for_call
            })

        print("-------------------\n")

    client.loop.create_task(client.run_until_disconnected())

    return {"status": "Listening (timezone fixed + -5:30 kept)"}

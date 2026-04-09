"""
SMScribe — Telegram webhook handler
POST /telegram/webhook

Handles incoming Telegram messages:
  /start          → welcome + request phone number
  contact share   → link phone number to chat_id, create/find user
  audio/voice     → check plan limits, trigger Modal transcription
  anything else   → help message
"""

import json
import os
import sys
import requests

sys.path.insert(0, "/opt/shared")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../shared"))

import db

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MODAL_TRANSCRIBE_URL = os.environ.get("MODAL_TRANSCRIBE_URL", "")
TELEGRAM_API      = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ─────────────────────────────────────────────────────────────────────────────
# Lambda entry point
# ─────────────────────────────────────────────────────────────────────────────-

def handler(event, context):
    print("RAW EVENT:", json.dumps(event.get("body", "")[:500]))
    
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "body": "ok"}

    try:
        body   = json.loads(event.get("body") or "{}")
        print("PARSED BODY KEYS:", list(body.keys()))
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid json"}

    message = body.get("message") or body.get("edited_message")
    print("MESSAGE:", json.dumps(message)[:300] if message else "None")

    chat_id = message["chat"]["id"]

    # ── Phone number shared (contact) ─────────────────────────────────────────
    if "contact" in message:
        _handle_contact(chat_id, message["contact"])
        return {"statusCode": 200, "body": "ok"}

    # ── Audio or voice message ────────────────────────────────────────────────
    if "audio" in message or "voice" in message:
        _handle_audio(chat_id, message)
        return {"statusCode": 200, "body": "ok"}

    # ── Text commands ─────────────────────────────────────────────────────────
    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        _handle_start(chat_id)
    elif text.startswith("/help") or text.startswith("/status"):
        _handle_help(chat_id)
    else:
        _send_message(chat_id, (
            "Send me an audio file or voice message and I'll transcribe it for you.\n\n"
            "Need help? Type /help"
        ))

    return {"statusCode": 200, "body": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_start(chat_id: int):
    """Send welcome message and request phone number."""
    payload = {
        "chat_id": chat_id,
        "text": (
            "👋 Welcome to SMScribe!\n\n"
            "I transcribe your lectures and audio files — just send me an audio message.\n\n"
            "First, tap the button below to share your phone number and link your account."
        ),
        "reply_markup": {
            "keyboard": [[{
                "text": "📱 Share my phone number",
                "request_contact": True,
            }]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    }
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)


def _handle_contact(chat_id: int, contact: dict):
    """User shared their phone number — link to DynamoDB account."""
    raw_phone = contact.get("phone_number", "")

    # Normalise to E.164
    phone = _normalise_phone(raw_phone)
    if not phone:
        _send_message(chat_id, "Could not read your phone number. Please try again.")
        return

    # Create user if not exists, then store telegram_chat_id
    user = db.get_user(phone)
    if not user:
        user = db.create_user(phone)

    # Save chat_id on the user record
    db.link_telegram(phone, str(chat_id))

    plan  = user.get("plan", "free")
    limits = db.PLAN_LIMITS.get(plan, db.PLAN_LIMITS["free"])

    _send_message(chat_id, (
        f"✅ You're linked! Phone: {phone}\n"
        f"Plan: {plan.capitalize()} · "
        f"{'Unlimited' if limits['transcriptions'] == -1 else str(limits['transcriptions']) + ' transcriptions'}/mo\n\n"
        "Send me any audio file or voice message to get started."
    ), remove_keyboard=True)


def _handle_audio(chat_id: int, message: dict):
    """Receive audio, check limits, trigger transcription."""

    # Look up user by chat_id
    phone = db.get_phone_by_telegram(str(chat_id))
    if not phone:
        _send_message(chat_id, (
            "Please link your account first by sending /start and sharing your phone number."
        ))
        return

    user = db.get_user(phone)
    if not user:
        _send_message(chat_id, "Account not found. Please send /start to set up your account.")
        return

    plan = user.get("plan", "free")

    # Check plan limit
    allowed, current, limit = db.check_plan_limit(phone, plan)
    if not allowed:
        _send_message(chat_id, (
            f"You've used all {limit} transcriptions for this month on the {plan.capitalize()} plan.\n\n"
            f"Upgrade at smscribe.com/account to get more."
        ))
        return

    # Get file info from Telegram
    audio  = message.get("audio") or message.get("voice")
    file_id = audio.get("file_id")
    duration = audio.get("duration", 0)

    # Check max duration for plan
    max_minutes = db.PLAN_LIMITS[plan]["max_minutes"]
    if duration > max_minutes * 60:
        _send_message(chat_id, (
            f"That file is {duration // 60} min long. "
            f"Your {plan.capitalize()} plan supports up to {max_minutes} min per file.\n\n"
            "Upgrade at smscribe.com/account"
        ))
        return

    # Get Telegram download URL for the file
    file_info = requests.get(
        f"{TELEGRAM_API}/getFile",
        params={"file_id": file_id},
        timeout=10,
    ).json()

    if not file_info.get("ok"):
        _send_message(chat_id, "Could not retrieve your audio file. Please try again.")
        return

    file_path = file_info["result"]["file_path"]
    file_url  = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

    # Determine content type
    content_type = "audio/ogg" if "voice" in message else (
        message.get("audio", {}).get("mime_type", "audio/mpeg")
    )

    # Increment usage
    db.increment_usage(phone)

    # Notify user
    _send_message(chat_id, "Got it — transcribing now ⚡ I'll message you when it's ready.")

    # Trigger Modal transcription async
    if MODAL_TRANSCRIBE_URL:
        try:
            requests.post(
                MODAL_TRANSCRIBE_URL,
                json={
                    "file_url":      file_url,
                    "phone_number":  phone,
                    "chat_id":       str(chat_id),
                    "content_type":  content_type,
                    "source":        "telegram",
                },
                timeout=5,  # fire and forget — Modal handles the rest
            )
        except requests.exceptions.Timeout:
            pass  # expected — Modal is async
        except Exception as e:
            print(f"Error triggering Modal: {e}")
            db.increment_usage(phone)  # refund usage on trigger failure
            _send_message(chat_id, "Something went wrong starting transcription. Please try again.")
    else:
        print("MODAL_TRANSCRIBE_URL not set — skipping transcription trigger")


def _handle_help(chat_id: int):
    phone = db.get_phone_by_telegram(str(chat_id))
    if not phone:
        _send_message(chat_id, "Send /start to link your account first.")
        return

    user    = db.get_user(phone)
    plan    = user.get("plan", "free") if user else "free"
    current = db.get_usage(phone)
    limits  = db.PLAN_LIMITS.get(plan, db.PLAN_LIMITS["free"])
    limit   = limits["transcriptions"]

    usage_str = f"{current} used this month" if limit == -1 else f"{current}/{limit} used this month"

    _send_message(chat_id, (
        f"SMScribe Help\n\n"
        f"Plan: {plan.capitalize()}\n"
        f"Usage: {usage_str}\n"
        f"Max file length: {limits['max_minutes']} min\n\n"
        "Commands:\n"
        "/start — set up or re-link your account\n"
        "/help — show this message\n\n"
        "Manage your plan: smscribe.com/account\n"
        "Support: support@smscribe.com"
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _send_message(chat_id: int, text: str, remove_keyboard: bool = False):
    payload = {"chat_id": chat_id, "text": text}
    if remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")


def _normalise_phone(raw: str) -> str:
    """Convert any phone format to E.164 +1XXXXXXXXXX."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return ""
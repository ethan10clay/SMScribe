"""
SMScribe — Twilio inbound SMS/MMS webhook
POST /twilio/webhook

Handles incoming messages from the production SMS number:
  media message  -> check plan limits, trigger Modal transcription
  HELP / STATUS  -> usage + plan help
  anything else  -> prompt user to send an audio attachment
"""

import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, "/opt/shared")
import db
import security as sec

MODAL_TRANSCRIBE_URL = os.environ.get("MODAL_TRANSCRIBE_URL", "")


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return _twiml_response()

    if not sec.validate_twilio_signature(event):
        return _twiml_response(
            "We could not verify this request.",
            status=403,
        )

    params = _parse_params(event.get("body") or "")
    phone_number = (params.get("From") or "").strip()
    body = (params.get("Body") or "").strip()
    num_media = _safe_int(params.get("NumMedia", "0"))

    if not phone_number:
        return _twiml_response("Missing sender phone number.", status=400)

    user = db.get_user(phone_number)
    if not user:
        return _twiml_response(
            "Your number is not set up yet. Sign in at smscribe.com first, then text us your audio file."
        )

    if num_media > 0:
        return _handle_media(phone_number, user, params)

    command = body.upper()
    if command in {"HELP", "STATUS"}:
        return _handle_help(phone_number, user)

    return _twiml_response(
        "Send an audio file or voice memo as an attachment and we'll text back the transcript."
    )


def _handle_media(phone_number: str, user: dict, params: dict):
    plan = user.get("plan", "free")

    allowed, _, limit = db.check_plan_limit(phone_number, plan)
    if not allowed:
        return _twiml_response(
            f"You've used all {limit} transcriptions for this month on the {plan.capitalize()} plan. "
            "Upgrade at smscribe.com/account to get more."
        )

    media_url = (params.get("MediaUrl0") or "").strip()
    content_type = (params.get("MediaContentType0") or "").strip().lower()
    reply_from_number = (params.get("To") or "").strip()
    if not media_url:
        return _twiml_response("We couldn't read your attachment. Please try again.")

    if not _is_supported_audio(content_type, media_url):
        return _twiml_response(
            "Please send an audio attachment such as m4a, mp3, wav, ogg, or amr."
        )

    db.increment_usage(phone_number)

    if not MODAL_TRANSCRIBE_URL:
        print("MODAL_TRANSCRIBE_URL not set")
        try:
            db.decrement_usage(phone_number)
        except Exception as refund_error:
            print(f"Error refunding usage: {refund_error}")
        return _twiml_response("SMScribe is not fully configured yet. Please try again shortly.")

    try:
        import requests

        requests.post(
            MODAL_TRANSCRIBE_URL,
            json={
                "file_url": media_url,
                "phone_number": phone_number,
                "content_type": content_type or "audio/mpeg",
                "reply_from_number": reply_from_number,
                "source": "twilio",
            },
            timeout=5,
        )
    except requests.exceptions.Timeout:
        pass
    except Exception as exc:
        print(f"Error triggering Modal: {exc}")
        try:
            db.decrement_usage(phone_number)
        except Exception as refund_error:
            print(f"Error refunding usage: {refund_error}")
        return _twiml_response(
            "Something went wrong starting transcription. Please try again."
        )

    return _twiml_response("Got it — transcribing now. We'll text you when it's ready.")


def _handle_help(phone_number: str, user: dict):
    plan = user.get("plan", "free")
    current = db.get_usage(phone_number)
    limits = db.PLAN_LIMITS.get(plan, db.PLAN_LIMITS["free"])
    limit = limits["transcriptions"]
    usage_str = (
        f"{current} used this month"
        if limit == -1
        else f"{current}/{limit} used this month"
    )

    return _twiml_response(
        f"SMScribe Help\n"
        f"Plan: {plan.capitalize()}\n"
        f"Usage: {usage_str}\n"
        f"Max file length: {limits['max_minutes']} min\n"
        f"Manage your plan: smscribe.com/account\n"
        f"Support: support@smscribe.com"
    )


def _parse_params(body: str) -> dict:
    params = {}
    for key, values in parse_qs(body, keep_blank_values=True).items():
        params[key] = values[0] if values else ""
    return params


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _is_supported_audio(content_type: str, media_url: str) -> bool:
    supported_types = (
        "audio/",
        "video/quicktime",
        "application/octet-stream",
    )
    if any(content_type.startswith(prefix) for prefix in supported_types):
        return True

    lowered = media_url.lower()
    return lowered.endswith((".m4a", ".mp3", ".wav", ".ogg", ".oga", ".amr", ".aac"))


def _twiml_response(message: str = "", status: int = 200) -> dict:
    body = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Response>"
        + (f"<Message>{_xml_escape(message)}</Message>" if message else "")
        + "</Response>"
    )
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/xml; charset=utf-8"},
        "body": body,
    }


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
        .replace("'", "&apos;")
    )

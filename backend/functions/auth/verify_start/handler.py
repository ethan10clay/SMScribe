"""
POST /auth/verify/start
Body: { "phone_number": "+15555550100" }

Sends a one-time passcode to the user's phone via Twilio Verify.
"""

import json
import os
import sys

sys.path.insert(0, "/opt/shared")  # Lambda layer path
import security as sec


def handler(event, context):
    # Handle CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return sec.ok({})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return sec.err("Invalid JSON body")

    phone_number = (body.get("phone_number") or "").strip()
    if not phone_number:
        return sec.err("phone_number is required")

    # Basic E.164 check
    if not phone_number.startswith("+") or not phone_number[1:].isdigit():
        return sec.err("phone_number must be in E.164 format: +15555550100")

    try:
        from twilio.rest import Client
        client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
        verification = client.verify \
            .v2 \
            .services(os.environ["TWILIO_VERIFY_SERVICE_SID"]) \
            .verifications \
            .create(to=phone_number, channel="sms")

        return sec.ok({
            "status":   verification.status,
            "message":  "Verification code sent.",
        })

    except Exception as e:
        print(f"Twilio Verify error: {e}")
        return sec.err(f"Could not send verification code: {str(e)}", 500)

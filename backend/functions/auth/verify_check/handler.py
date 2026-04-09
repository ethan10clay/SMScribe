"""
POST /auth/verify/check
Body: { "phone_number": "+15555550100", "code": "123456" }

Verifies the OTP, creates user in DynamoDB if new, returns JWT.
"""

import json
import os
import sys

sys.path.insert(0, "/opt/shared")
import db
import security as sec


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return sec.ok({})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return sec.err("Invalid JSON body")

    phone_number = (body.get("phone_number") or "").strip()
    code         = (body.get("code") or "").strip()

    if not phone_number or not code:
        return sec.err("phone_number and code are required")

    # Verify OTP with Twilio
    try:
        from twilio.rest import Client
        client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
        check = client.verify \
            .v2 \
            .services(os.environ["TWILIO_VERIFY_SERVICE_SID"]) \
            .verification_checks \
            .create(to=phone_number, code=code)

        if check.status != "approved":
            return sec.err("Invalid or expired verification code", 401)

    except Exception as e:
        print(f"Twilio Verify check error: {e}")
        return sec.err(f"Verification failed: {str(e)}", 500)

    # Create user if they don't exist yet
    is_new_user = not db.user_exists(phone_number)
    user        = db.create_user(phone_number)

    # Issue JWT
    token = sec.create_jwt(phone_number)

    return sec.ok({
        "token":       token,
        "is_new_user": is_new_user,
        "user": {
            "phone_number": user["phone_number"],
            "plan":         user["plan"],
        },
    }, status=201 if is_new_user else 200)

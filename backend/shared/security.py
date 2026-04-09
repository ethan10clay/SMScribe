"""
SMScribe — Security utilities
JWT session tokens, signature validation, CORS headers.
"""

import hashlib
import hmac
import json
import os
import time
import base64
from typing import Optional

JWT_SECRET   = os.environ.get("JWT_SECRET", "")
JWT_EXPIRY   = 60 * 60 * 24 * 30  # 30 days
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://smscribe.com")


# ─────────────────────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────────────────────

def cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin":  FRONTEND_URL,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Credentials": "true",
        "Content-Type": "application/json",
    }


def ok(body: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": cors_headers(),
        "body": json.dumps(body),
    }


def err(message: str, status: int = 400) -> dict:
    return {
        "statusCode": status,
        "headers": cors_headers(),
        "body": json.dumps({"error": message}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# JWT  (minimal — no external library needed)
# ─────────────────────────────────────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def create_jwt(phone_number: str) -> str:
    """Create a signed JWT containing the user's phone number."""
    header  = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(json.dumps({
        "sub": phone_number,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRY,
    }).encode())
    signing_input = f"{header}.{payload}"
    sig = hmac.new(
        JWT_SECRET.encode(),
        signing_input.encode(),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


def verify_jwt(token: str) -> Optional[str]:
    """
    Verify JWT and return phone_number, or None if invalid/expired.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, sig = parts
        signing_input = f"{header}.{payload}"
        expected_sig = hmac.new(
            JWT_SECRET.encode(),
            signing_input.encode(),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(
            _b64url_decode(sig),
            expected_sig,
        ):
            return None
        claims = json.loads(_b64url_decode(payload))
        if claims.get("exp", 0) < time.time():
            return None
        return claims.get("sub")
    except Exception:
        return None


def require_auth(event: dict) -> tuple:
    """
    Extract and verify JWT from Authorization header.
    Returns (phone_number, None) on success or (None, error_response) on failure.
    """
    headers = event.get("headers") or {}
    # API Gateway lowercases header names
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None, err("Missing or invalid Authorization header", 401)
    token = auth[len("Bearer "):]
    phone = verify_jwt(token)
    if not phone:
        return None, err("Token expired or invalid", 401)
    return phone, None


# ─────────────────────────────────────────────────────────────────────────────
# TWILIO signature validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_twilio_signature(event: dict) -> bool:
    """
    Validate that inbound webhook is genuinely from Twilio.
    https://www.twilio.com/docs/usage/webhooks/webhooks-security
    """
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not auth_token:
        return False

    headers        = event.get("headers") or {}
    twilio_sig     = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature") or ""
    request_url    = _reconstruct_url(event)
    body           = event.get("body") or ""

    # Parse POST params and sort
    params = {}
    if body:
        from urllib.parse import parse_qs
        for k, v in parse_qs(body).items():
            params[k] = v[0]

    # Build validation string: URL + sorted param key/value pairs
    s = request_url
    for key in sorted(params.keys()):
        s += key + params[key]

    expected = base64.b64encode(
        hmac.new(auth_token.encode(), s.encode(), hashlib.sha1).digest()
    ).decode()

    return hmac.compare_digest(expected, twilio_sig)


def _reconstruct_url(event: dict) -> str:
    ctx    = event.get("requestContext", {})
    domain = event.get("headers", {}).get("Host", "")
    stage  = ctx.get("stage", "")
    path   = event.get("path", "")
    if stage and not path.startswith(f"/{stage}"):
        path = f"/{stage}{path}"
    return f"https://{domain}{path}"


# ─────────────────────────────────────────────────────────────────────────────
# STRIPE signature validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_stripe_signature(event: dict) -> tuple:
    """
    Validate Stripe webhook signature.
    Returns (payload_dict, None) on success or (None, error_response) on failure.
    """
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    headers = event.get("headers") or {}
    sig     = headers.get("stripe-signature") or headers.get("Stripe-Signature") or ""
    payload = event.get("body") or ""

    try:
        stripe_event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
        return stripe_event, None
    except stripe.error.SignatureVerificationError:
        return None, err("Invalid Stripe signature", 400)
    except Exception as e:
        return None, err(f"Webhook error: {str(e)}", 400)
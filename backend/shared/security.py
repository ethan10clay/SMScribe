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
from urllib.parse import parse_qs
from decimal import Decimal
from typing import Optional

class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)

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
        "body": json.dumps(body, cls=_DecimalEncoder),
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

    from twilio.request_validator import RequestValidator

    headers = event.get("headers") or {}
    twilio_sig = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature") or ""
    if not twilio_sig:
        return False

    body = event.get("body") or ""
    params = {}
    if body:
        for k, v in parse_qs(body, keep_blank_values=True).items():
            params[k] = v[0] if v else ""

    validator = RequestValidator(auth_token)
    for url in _candidate_twilio_urls(event):
        try:
            if validator.validate(url, params, twilio_sig):
                return True
        except Exception:
            continue
    return False


def _candidate_twilio_urls(event: dict) -> list[str]:
    ctx = event.get("requestContext", {})
    headers = event.get("headers") or {}
    domain = (
        headers.get("X-Forwarded-Host")
        or headers.get("x-forwarded-host")
        or headers.get("Host")
        or headers.get("host")
        or ctx.get("domainName")
        or ""
    )
    proto = (
        headers.get("X-Forwarded-Proto")
        or headers.get("x-forwarded-proto")
        or "https"
    )
    stage = ctx.get("stage", "")

    raw_paths = [
        event.get("path") or "",
        ctx.get("path") or "",
        ctx.get("resourcePath") or "",
    ]

    candidates = []
    for raw_path in raw_paths:
        if not raw_path:
            continue
        if raw_path.startswith("http://") or raw_path.startswith("https://"):
            candidates.append(raw_path)
            continue

        normalized = raw_path if raw_path.startswith("/") else f"/{raw_path}"
        candidates.append(f"{proto}://{domain}{normalized}")
        if stage and not normalized.startswith(f"/{stage}"):
            candidates.append(f"{proto}://{domain}/{stage}{normalized}")

    # Preserve order but remove duplicates.
    seen = set()
    unique = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


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

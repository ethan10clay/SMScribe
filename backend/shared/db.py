"""
SMScribe — DynamoDB helpers
Shared across all Lambda functions.
"""

import boto3
import os
from datetime import datetime, timezone
from typing import Optional

AWS_REGION   = os.environ.get("AWS_REGION", "us-east-1")
TABLE_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "smscribe-")

USERS_TABLE = f"{TABLE_PREFIX}users"
JOBS_TABLE  = f"{TABLE_PREFIX}jobs"
USAGE_TABLE = f"{TABLE_PREFIX}usage"

PLAN_LIMITS = {
    "free":    {"transcriptions": 3,   "max_minutes": 30},
    "student": {"transcriptions": 30,  "max_minutes": 180},
    "pro":     {"transcriptions": -1,  "max_minutes": 360},  # -1 = unlimited
}

DEFAULT_CONSENT = (
    "By checking this box, you agree to receive text messages from SMScribe "
    "at the number provided, including transcription results, account notifications, "
    "and service updates. Message and data rates may apply. Message frequency varies. "
    "Reply STOP to unsubscribe or HELP for support."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")

def _resource():
    return boto3.resource("dynamodb", region_name=AWS_REGION)

def _table(name: str):
    return _resource().Table(name)


# ─────────────────────────────────────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────────────────────────────────────

def create_user(phone_number: str, plan: str = "free") -> dict:
    """Create user after OTP verification. Idempotent — won't overwrite existing."""
    now = _now()
    item = {
        "phone_number":       phone_number,
        "plan":               plan,
        "stripe_customer_id": "",
        "stripe_sub_id":      "",
        "consent_text":       DEFAULT_CONSENT,
        "consent_timestamp":  now,
        "created_at":         now,
        "updated_at":         now,
    }
    try:
        _table(USERS_TABLE).put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(phone_number)",
        )
    except _resource().meta.client.exceptions.ConditionalCheckFailedException:
        # User already exists — return existing
        return get_user(phone_number)
    return item


def get_user(phone_number: str) -> Optional[dict]:
    resp = _table(USERS_TABLE).get_item(Key={"phone_number": phone_number})
    return resp.get("Item")


def user_exists(phone_number: str) -> bool:
    return get_user(phone_number) is not None


def update_user_plan(
    phone_number: str,
    plan: str,
    stripe_customer_id: str = "",
    stripe_sub_id: str = "",
):
    _table(USERS_TABLE).update_item(
        Key={"phone_number": phone_number},
        UpdateExpression=(
            "SET #plan = :plan, stripe_customer_id = :cid, "
            "stripe_sub_id = :sid, updated_at = :ts"
        ),
        ExpressionAttributeNames={"#plan": "plan"},
        ExpressionAttributeValues={
            ":plan": plan,
            ":cid":  stripe_customer_id,
            ":sid":  stripe_sub_id,
            ":ts":   _now(),
        },
    )


def cancel_user_plan(phone_number: str):
    """Downgrade to free on subscription cancellation."""
    _table(USERS_TABLE).update_item(
        Key={"phone_number": phone_number},
        UpdateExpression=(
            "SET #plan = :free, stripe_sub_id = :empty, updated_at = :ts"
        ),
        ExpressionAttributeNames={"#plan": "plan"},
        ExpressionAttributeValues={
            ":free":  "free",
            ":empty": "",
            ":ts":    _now(),
        },
    )


def get_user_by_stripe_customer(stripe_customer_id: str) -> Optional[dict]:
    """Scan for user by Stripe customer ID (used in webhook handler)."""
    resp = _table(USERS_TABLE).scan(
        FilterExpression="stripe_customer_id = :cid",
        ExpressionAttributeValues={":cid": stripe_customer_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


# ─────────────────────────────────────────────────────────────────────────────
# JOBS
# ─────────────────────────────────────────────────────────────────────────────

JOB_STATUS = {
    "PENDING":    "pending",
    "PROCESSING": "processing",
    "DONE":       "done",
    "FAILED":     "failed",
}


def create_job(
    job_id: str,
    phone_number: str,
    s3_audio_key: str,
    content_type: str,
) -> dict:
    now = _now()
    item = {
        "job_id":             job_id,
        "phone_number":       phone_number,
        "status":             JOB_STATUS["PENDING"],
        "s3_audio_key":       s3_audio_key,
        "s3_transcript_key":  "",
        "content_type":       content_type,
        "duration_min":       "0",
        "word_count":         0,
        "presigned_url":      "",
        "error":              "",
        "created_at":         now,
        "updated_at":         now,
    }
    _table(JOBS_TABLE).put_item(Item=item)
    return item


def update_job_processing(job_id: str):
    _table(JOBS_TABLE).update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, updated_at = :ts",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s":  JOB_STATUS["PROCESSING"],
            ":ts": _now(),
        },
    )


def update_job_done(
    job_id: str,
    s3_transcript_key: str,
    presigned_url: str,
    duration_min: float,
    word_count: int,
):
    _table(JOBS_TABLE).update_item(
        Key={"job_id": job_id},
        UpdateExpression=(
            "SET #s = :s, s3_transcript_key = :tkey, presigned_url = :url, "
            "duration_min = :dur, word_count = :wc, updated_at = :ts"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s":    JOB_STATUS["DONE"],
            ":tkey": s3_transcript_key,
            ":url":  presigned_url,
            ":dur":  str(round(duration_min, 1)),
            ":wc":   word_count,
            ":ts":   _now(),
        },
    )


def update_job_failed(job_id: str, error: str):
    _table(JOBS_TABLE).update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, #e = :e, updated_at = :ts",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={
            ":s":  JOB_STATUS["FAILED"],
            ":e":  error[:500],
            ":ts": _now(),
        },
    )


def get_job(job_id: str) -> Optional[dict]:
    resp = _table(JOBS_TABLE).get_item(Key={"job_id": job_id})
    return resp.get("Item")


def get_user_jobs(phone_number: str, limit: int = 20) -> list:
    resp = _table(JOBS_TABLE).query(
        IndexName="phone-index",
        KeyConditionExpression="phone_number = :p",
        ExpressionAttributeValues={":p": phone_number},
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


# ─────────────────────────────────────────────────────────────────────────────
# USAGE  (plan limit enforcement)
# ─────────────────────────────────────────────────────────────────────────────

def increment_usage(phone_number: str) -> int:
    """Atomically increment transcription count. Returns new count."""
    resp = _table(USAGE_TABLE).update_item(
        Key={"phone_number": phone_number, "month": _current_month()},
        UpdateExpression="ADD transcription_count :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["transcription_count"])


def get_usage(phone_number: str, month: Optional[str] = None) -> int:
    month = month or _current_month()
    resp = _table(USAGE_TABLE).get_item(
        Key={"phone_number": phone_number, "month": month}
    )
    item = resp.get("Item")
    return int(item["transcription_count"]) if item else 0


def check_plan_limit(phone_number: str, plan: str) -> tuple:
    """Returns (allowed: bool, current: int, limit: int). -1 = unlimited."""
    limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["transcriptions"]
    current = get_usage(phone_number)
    if limit == -1:
        return True, current, limit
    return current < limit, current, limit
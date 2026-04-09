"""
GET /user/me
Headers: Authorization: Bearer <jwt>

Returns user profile, plan, and current month usage.
"""

import sys

sys.path.insert(0, "/opt/shared")
import db
import security as sec


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return sec.ok({})

    phone_number, error = sec.require_auth(event)
    if error:
        return error

    user = db.get_user(phone_number)
    if not user:
        return sec.err("User not found", 404)

    plan         = user.get("plan", "free")
    current      = db.get_usage(phone_number)
    plan_limits  = db.PLAN_LIMITS.get(plan, db.PLAN_LIMITS["free"])
    limit        = plan_limits["transcriptions"]

    return sec.ok({
        "phone_number":     user["phone_number"],
        "plan":             plan,
        "created_at":       user.get("created_at", ""),
        "usage": {
            "current_month":       current,
            "limit":               limit,           # -1 = unlimited
            "max_minutes_per_file": plan_limits["max_minutes"],
        },
        "recent_jobs": db.get_user_jobs(phone_number, limit=10),
    })

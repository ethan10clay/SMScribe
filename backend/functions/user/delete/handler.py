"""
DELETE /user/me
Headers: Authorization: Bearer <jwt>

Permanently deletes the user's account:
  1. Cancels Stripe subscription if active
  2. Deletes all S3 audio + transcript files
  3. Deletes all DynamoDB records (users, jobs, usage)
  4. Returns 200 — client should clear JWT and redirect
"""

import os
import sys

sys.path.insert(0, "/opt/shared")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../shared"))

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

    errors = []

    # 1. Cancel Stripe subscription
    stripe_sub_id = user.get("stripe_sub_id", "")
    if stripe_sub_id:
        try:
            import stripe
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            stripe.Subscription.cancel(stripe_sub_id)
            print(f"Cancelled Stripe subscription {stripe_sub_id}")
        except Exception as e:
            print(f"Stripe cancel error (non-fatal): {e}")
            errors.append(f"Stripe: {str(e)[:100]}")

    # 2. Delete S3 files
    try:
        import boto3
        s3      = boto3.client("s3")
        bucket  = os.environ.get("S3_BUCKET", "")
        jobs    = db.get_user_jobs(phone_number, limit=200)

        for job in jobs:
            for key_field in ("s3_audio_key", "s3_transcript_key"):
                key = job.get(key_field, "")
                if key:
                    try:
                        s3.delete_object(Bucket=bucket, Key=key)
                    except Exception as e:
                        print(f"S3 delete error for {key}: {e}")

        print(f"Deleted S3 files for {db._mask(phone_number) if hasattr(db, '_mask') else phone_number}")
    except Exception as e:
        print(f"S3 cleanup error (non-fatal): {e}")
        errors.append(f"S3: {str(e)[:100]}")

    # 3. Delete DynamoDB records
    try:
        # Delete all jobs
        jobs = db.get_user_jobs(phone_number, limit=200)
        for job in jobs:
            db._table(db.JOBS_TABLE).delete_item(Key={"job_id": job["job_id"]})

        # Delete usage records
        from boto3.dynamodb.conditions import Key as DKey
        resp = db._table(db.USAGE_TABLE).query(
            KeyConditionExpression=DKey("phone_number").eq(phone_number)
        )
        for item in resp.get("Items", []):
            db._table(db.USAGE_TABLE).delete_item(
                Key={"phone_number": phone_number, "month": item["month"]}
            )

        # Delete user last
        db._table(db.USERS_TABLE).delete_item(Key={"phone_number": phone_number})
        print(f"Deleted DynamoDB records for user")

    except Exception as e:
        print(f"DynamoDB delete error: {e}")
        return sec.err(f"Failed to delete account data: {str(e)[:200]}", 500)

    return sec.ok({
        "message": "Account deleted successfully.",
        "errors":  errors,  # non-fatal issues (e.g. S3 partial failure)
    })
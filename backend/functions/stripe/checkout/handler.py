"""
POST /stripe/checkout
Headers: Authorization: Bearer <jwt>
Body: { "plan": "student" | "pro", "interval": "month" }

Creates a Stripe Checkout session and returns the URL.
"""

import json
import os
import sys

sys.path.insert(0, "/opt/shared")
import db
import security as sec


PRICE_IDS = {
    "student": {
        "month": "STRIPE_STUDENT_MONTHLY_PRICE_ID",
    },
    "pro": {
        "month": "STRIPE_PRO_MONTHLY_PRICE_ID",
    },
}


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return sec.ok({})

    phone_number, error = sec.require_auth(event)
    if error:
        return error

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return sec.err("Invalid JSON body")

    plan     = (body.get("plan") or "").lower()
    interval = (body.get("interval") or "month").lower()

    if plan not in ("student", "pro"):
        return sec.err("plan must be 'student' or 'pro'")
    if interval != "month":
        return sec.err("interval must be 'month'")

    # Look up the Stripe Price ID from environment
    price_env_key = PRICE_IDS[plan][interval]
    price_id      = os.environ.get(price_env_key, "")
    if not price_id:
        return sec.err(f"Price not configured for {plan}/{interval}", 500)

    user = db.get_user(phone_number)
    if not user:
        return sec.err("User not found", 404)

    try:
        import stripe
        stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
        frontend_url   = os.environ.get("FRONTEND_URL", "https://smscribe.com")

        # Reuse existing Stripe customer or create new one
        customer_id = user.get("stripe_customer_id") or ""
        if not customer_id:
            customer    = stripe.Customer.create(metadata={"phone_number": phone_number})
            customer_id = customer.id
            # Save immediately so we can match on webhook
            db.update_user_plan(
                phone_number,
                plan=user.get("plan", "free"),
                stripe_customer_id=customer_id,
            )

        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{frontend_url}/?checkout=success",
            cancel_url=f"{frontend_url}/?checkout=cancelled",
            metadata={
                "phone_number": phone_number,
                "plan":         plan,
            },
            subscription_data={
                "metadata": {
                    "phone_number": phone_number,
                    "plan":         plan,
                }
            },
        )

        return sec.ok({"checkout_url": session.url})

    except Exception as e:
        print(f"Stripe checkout error: {e}")
        return sec.err(f"Could not create checkout session: {str(e)}", 500)

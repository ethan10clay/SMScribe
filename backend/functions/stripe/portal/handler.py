"""
POST /stripe/portal
Headers: Authorization: Bearer <jwt>

Creates a Stripe billing portal session and returns the URL.
Users are redirected there to cancel, update payment, or manage their subscription.
"""

import os
import sys

sys.path.insert(0, "/opt/shared")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../shared"))

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

    customer_id = user.get("stripe_customer_id", "")
    if not customer_id:
        return sec.err("No billing account found. Please subscribe to a plan first.", 400)

    try:
        import stripe
        stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
        frontend_url   = os.environ.get("FRONTEND_URL", "https://smscribe.com")

        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{frontend_url}/account",
        )

        return sec.ok({"portal_url": session.url})

    except Exception as e:
        print(f"Stripe portal error: {e}")
        return sec.err(f"Could not create portal session: {str(e)}", 500)
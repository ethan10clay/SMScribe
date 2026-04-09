"""
POST /stripe/webhook
No auth header — validated via Stripe signature instead.

Handles:
  checkout.session.completed      → activate plan
  customer.subscription.updated   → plan change
  customer.subscription.deleted   → downgrade to free
  invoice.payment_failed          → notify user (TODO: send SMS)
"""

import json
import os
import sys

sys.path.insert(0, "/opt/shared")
import db
import security as sec

# Map Stripe Product/Price metadata plan names to our plan keys
VALID_PLANS = {"student", "pro", "free"}


def handler(event, context):
    stripe_event, error = sec.validate_stripe_signature(event)
    if error:
        print("Stripe signature validation failed")
        return error

    event_type = stripe_event["type"]
    data       = stripe_event["data"]["object"]

    print(f"Stripe event received: {event_type}")

    try:
        if event_type == "checkout.session.completed":
            _handle_checkout_completed(data)

        elif event_type == "customer.subscription.updated":
            _handle_subscription_updated(data)

        elif event_type == "customer.subscription.deleted":
            _handle_subscription_deleted(data)

        elif event_type == "invoice.payment_failed":
            _handle_payment_failed(data)

        else:
            print(f"Unhandled event type: {event_type} — ignoring")

    except Exception as e:
        print(f"Error handling {event_type}: {e}")
        # Return 200 anyway so Stripe doesn't keep retrying a bug
        return sec.ok({"received": True, "error": str(e)})

    return sec.ok({"received": True})


# ─────────────────────────────────────────────────────────────────────────────

def _handle_checkout_completed(session: dict):
    """Checkout succeeded — activate the purchased plan."""
    phone_number    = session.get("metadata", {}).get("phone_number", "")
    plan            = session.get("metadata", {}).get("plan", "")
    customer_id     = session.get("customer", "")
    subscription_id = session.get("subscription", "")

    if not phone_number or plan not in VALID_PLANS:
        print(f"Missing metadata on checkout.session.completed: {session.get('id')}")
        return

    print(f"Activating {plan} plan for {_mask(phone_number)}")
    db.update_user_plan(
        phone_number,
        plan=plan,
        stripe_customer_id=customer_id,
        stripe_sub_id=subscription_id,
    )


def _handle_subscription_updated(subscription: dict):
    """Plan changed (upgrade/downgrade)."""
    customer_id = subscription.get("customer", "")
    sub_id      = subscription.get("id", "")
    status      = subscription.get("status", "")

    if status not in ("active", "trialing"):
        print(f"Subscription {sub_id} not active (status={status}) — skipping")
        return

    # Get plan from subscription metadata
    plan = subscription.get("metadata", {}).get("plan", "")
    if plan not in VALID_PLANS:
        # Fall back to looking at price nickname
        items = subscription.get("items", {}).get("data", [])
        plan  = items[0].get("price", {}).get("nickname", "").lower() if items else ""

    if not plan or plan not in VALID_PLANS:
        print(f"Could not determine plan for subscription {sub_id}")
        return

    user = db.get_user_by_stripe_customer(customer_id)
    if not user:
        print(f"No user found for Stripe customer {customer_id}")
        return

    print(f"Updating plan to {plan} for {_mask(user['phone_number'])}")
    db.update_user_plan(
        user["phone_number"],
        plan=plan,
        stripe_customer_id=customer_id,
        stripe_sub_id=sub_id,
    )


def _handle_subscription_deleted(subscription: dict):
    """Subscription cancelled — downgrade to free."""
    customer_id = subscription.get("customer", "")
    user        = db.get_user_by_stripe_customer(customer_id)
    if not user:
        print(f"No user found for Stripe customer {customer_id} on deletion")
        return

    print(f"Cancelling plan for {_mask(user['phone_number'])}")
    db.cancel_user_plan(user["phone_number"])


def _handle_payment_failed(invoice: dict):
    """Payment failed — log for now, SMS notification can be added later."""
    customer_id = invoice.get("customer", "")
    user        = db.get_user_by_stripe_customer(customer_id)
    phone       = user["phone_number"] if user else "unknown"
    print(f"Payment failed for customer {customer_id} ({_mask(phone)})")
    # TODO: send SMS via Twilio notifying user of failed payment


# ─────────────────────────────────────────────────────────────────────────────

def _mask(phone: str) -> str:
    """Mask phone number for logging — never log PII in full."""
    if len(phone) < 5:
        return "***"
    return phone[:3] + "***" + phone[-2:]

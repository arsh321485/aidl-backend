"""Stripe for plan upgrades (billing.py) — Stripe's HTTPS API, no SDK.

Checkout: a hosted Stripe Checkout Session in subscription mode, one line
item = the plan's recurring Stripe Price (STRIPE_PRICE_BASIC /
STRIPE_PRICE_ENTERPRISE, created in the Stripe dashboard) × number of users.
The plan choice (topics, schedule, users) travels in the session metadata.

Webhook: POST /api/billing/stripe/webhook/ (signed with STRIPE_WEBHOOK_SECRET)
- checkout.session.completed      → activate the plan
- customer.subscription.deleted   → back to Trial
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import requests
from django.conf import settings

from .models import Organization

logger = logging.getLogger(__name__)
API = "https://api.stripe.com/v1"
TOLERANCE = 300  # seconds a webhook signature stays valid


class StripeError(RuntimeError):
    pass


def price_for(plan: str) -> str:
    return (settings.STRIPE_PRICES.get(plan) or "").strip()


def ready(plan: str) -> bool:
    return bool(settings.STRIPE_SECRET_KEY and price_for(plan))


def _post(path: str, data: dict) -> dict:
    try:
        resp = requests.post(f"{API}{path}", data=data, auth=(settings.STRIPE_SECRET_KEY, ""), timeout=20)
    except requests.RequestException as exc:
        raise StripeError(f"Could not reach Stripe: {exc}") from exc
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        raise StripeError((body.get("error") or {}).get("message") or f"Stripe error {resp.status_code}")
    return body


def create_checkout_session(org: Organization, by, request_data: dict) -> str:
    """Return the Stripe Checkout URL for this upgrade."""
    plan = request_data["plan"]
    base = settings.FRONTEND_URL.rstrip("/")
    schedule = request_data.get("schedule") or {}
    meta = {
        "app": "aidl",  # the Stripe account is shared with other products
        "org_id": str(org.pk), "plan": plan, "topics": ",".join(request_data.get("topics") or []),
        "days": ",".join(str(d) for d in schedule.get("days") or []), "hour": str(schedule.get("hour") or ""),
        "seats": str(request_data.get("seats") or ""), "by": by.email,
    }
    data = {
        "mode": "subscription",
        "line_items[0][price]": price_for(plan),
        "line_items[0][quantity]": str(max(int(request_data.get("seats") or 1), 1)),
        "success_url": f"{base}/plans?checkout=success",
        "cancel_url": f"{base}/plans?checkout=cancelled",
        "client_reference_id": str(org.pk),
        "customer_email": by.email,
        "allow_promotion_codes": "true",
    }
    for k, v in meta.items():
        data[f"metadata[{k}]"] = v
        data[f"subscription_data[metadata][{k}]"] = v
    session = _post("/checkout/sessions", data)
    return session["url"]


def verify(payload: bytes, header: str) -> dict:
    """Check the Stripe-Signature header; return the event."""
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        raise StripeError("STRIPE_WEBHOOK_SECRET is not set")
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    signatures = [p.split("=", 1)[1] for p in (header or "").split(",") if p.startswith("v1=")]
    ts = parts.get("t", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > TOLERANCE:
        raise StripeError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise StripeError("bad signature")
    return json.loads(payload)


def handle(event: dict) -> str:
    """Apply a verified Stripe event. Returns what happened."""
    from . import billing

    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        if meta.get("app") != "aidl" or not meta.get("org_id"):
            return "ignored (not an AIDL checkout)"
        org = Organization.objects.filter(pk=meta["org_id"]).first()
        if org is None:
            return "unknown organization"
        if billing.seen_event(org, event.get("id", "")):
            return "duplicate"
        if obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "not paid"
        schedule = None
        if meta.get("days"):
            schedule = {"days": [int(d) for d in meta["days"].split(",")], "hour": int(meta.get("hour") or 10)}
        from .models import AIDLUser

        by = AIDLUser.objects.filter(email__iexact=meta.get("by", ""), organization_id=str(org.pk)).first()
        billing.activate(org, meta.get("plan", ""), [t for t in meta.get("topics", "").split(",") if t], schedule,
                         int(meta["seats"]) if meta.get("seats") else None, by, how="stripe",
                         stripe={"customer": obj.get("customer", ""), "subscription": obj.get("subscription", "")})
        if by is not None:
            billing.refresh_slack(org, by)
        return "activated"
    if kind == "customer.subscription.deleted":
        if (obj.get("metadata") or {}).get("app") != "aidl":
            return "ignored (not an AIDL subscription)"
        org = billing.org_for_subscription(obj.get("id", ""))
        if org is None:
            return "unknown subscription"
        billing.downgrade_to_trial(org, how="stripe_cancelled")
        return "cancelled"
    return "ignored"

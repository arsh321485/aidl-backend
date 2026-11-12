"""Plan upgrades from the website (plans.py has the plans themselves).

The admin opens the plans page from Slack (Admin Center → Upgrade plan),
picks the next plan, the topics its cards come from (Basic; Enterprise
covers all topics) and — for Enterprise — the delivery days and time,
previews the first cards (blurred) and goes to checkout.

Checkout:
- internal testing access (PromoCode, entered in Slack: slack_promo.py) is not
  checkout. It never involves Stripe, and an organization that has it is never
  sent to payment (see checkout)
- admins listed in BILLING_UNLIMITED_ADMIN_EMAILS upgrade without paying
- Stripe (stripe_billing.py) when STRIPE_SECRET_KEY and the plan's
  STRIPE_PRICE_<PLAN> are set → hosted Checkout; the webhook activates it
- BILLING_TEST_MODE=True activates straight away (testing)
- otherwise the request is saved as pending and BILLING_SALES_EMAIL is told
Organization.billing (JSON): {"topics": {plan: [codes]}, "schedule": {days,
hour}, "pending": {...}, "history": [...]}.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.utils import timezone

from . import plans
from .models import AIDLUser, Organization, PromoCode


class UpgradeError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("invalid upgrade")
        self.errors = errors


class ProviderNotConnected(RuntimeError):
    pass


def _load(org: Organization) -> dict:
    try:
        return json.loads(org.billing or "{}")
    except ValueError:
        return {}


def _save(org: Organization, data: dict) -> None:
    org.billing = json.dumps(data, ensure_ascii=False)
    org.save(update_fields=["billing", "updated_at"])


def _price(plan: str) -> str:
    return (settings.PLAN_PRICES.get(plan) or "").strip() or "Price on request"


# ---------- what the plans page shows ----------

def catalog(org: Organization) -> dict:
    current = plans.plan_key(org)
    data = _load(org)
    items = []
    for key in plans.ORDER:
        p = plans.PLANS[key]
        items.append({
            "key": key, "label": p["label"], "price": _price(key),
            "cards": plans.total_cards(key),
            "cadence": p["cadence"], "users": p["max_users"] or "As many as you buy",
            "picks_topics": p["picks_topics"], "all_topics": p["all_topics"],
            "custom_schedule": bool(p.get("custom_schedule")),
            "current": key == current, "available": plans.rank(key) >= plans.rank(current),
        })
    return {
        "organization": org.name,
        "current": current,
        "usage": plans.usage(org),
        "plans": items,
        "categories": plans.categories(),
        "topics": data.get("topics") or {},
        "schedule": data.get("schedule") or {"days": list(plans.PLANS["enterprise"]["schedule"]["days"]),
                                             "hour": plans.PLANS["enterprise"]["schedule"]["hour"]},
        "pending": data.get("pending"),
        "test_mode": settings.BILLING_TEST_MODE,
        "provider": settings.BILLING_PROVIDER,
        "payment_ready": {k: _stripe_ready(k) and not on_promo(org) for k in ("basic", "enterprise")},
        "sales_email": settings.BILLING_SALES_EMAIL,
        "preview_cards": PREVIEW_CARDS,
        "weekdays": list(plans.WEEKDAYS),
    }


PREVIEW_CARDS = 12


def preview(org: Organization, plan: str, topics: list[str]) -> dict:
    """The first cards `plan` adds beyond the Default 12 for these topics —
    titles only (the page shows them blurred; the card text stays on the
    server) — and how many it adds in all."""
    if plan not in plans.ORDER or plan == "trial":
        return {"cards": [], "total": 0}
    default = {c["no"] for c in plans._packages()["default"]}
    cards = [c for c in plans.cards_for_plan(org, plan, {plan: topics}) if c["no"] not in default]
    return {"cards": [{"no": c["no"], "title": c["title"], "type": c["type"], "category": c["category"]}
                      for c in cards[:PREVIEW_CARDS]],
            "total": len(cards)}


# ---------- upgrade ----------

def validate(org: Organization, plan: str, topics: list[str], schedule: dict | None, seats: int | None) -> dict:
    errors = {}
    if plan not in plans.ORDER:
        errors["plan"] = "Choose a plan."
    elif plans.rank(plan) < plans.rank(plans.plan_key(org)):
        errors["plan"] = "To move to a smaller plan, contact the AIDL team."
    elif plan == "trial":
        errors["plan"] = "You're already on the Trial plan."
    codes = {c["code"] for c in plans.categories()}
    if plan in plans.PLANS and plans.PLANS[plan]["picks_topics"]:
        if not topics:
            errors["topics"] = "Pick at least one topic."
        elif any(t not in codes for t in topics):
            errors["topics"] = "Unknown topic."
    if plan == "enterprise":
        days = sorted({int(d) for d in (schedule or {}).get("days", [])})
        hour = (schedule or {}).get("hour")
        if len(days) != 4 or any(d < 0 or d > 6 for d in days):
            errors["schedule"] = "Pick 4 delivery days."
        elif not isinstance(hour, int) or not 6 <= hour <= 20:
            errors["schedule"] = "Pick a delivery time between 06:00 and 20:00."
    if seats is not None and seats < plans.seat_holders(org).count():
        errors["seats"] = f"You already have {plans.seat_holders(org).count()} users."
    return errors


def _settle(org: Organization, by: AIDLUser | None) -> None:
    from .package_delivery import sync_organization

    sync_organization(org)
    if by is not None:
        refresh_slack(org, by)


def activate(org: Organization, plan: str, topics: list[str], schedule: dict | None, seats: int | None,
             by: AIDLUser | None, how: str, stripe: dict | None = None, promo: str = "",
             background: bool = False) -> None:
    """Switch the plan, save the picks and reschedule everyone's cards.
    `background`: a Slack popup must answer in 3 seconds, so the slow part follows."""
    data = _load(org)
    if stripe:
        data["stripe"] = {k: v for k, v in stripe.items() if v}
    if plans.PLANS[plan]["all_topics"]:
        topics = plans.all_topic_codes()
    data.setdefault("topics", {})[plan] = topics
    if plan == "enterprise" and schedule:
        data["schedule"] = {"days": sorted({int(d) for d in schedule["days"]}), "hour": int(schedule["hour"])}
    data.pop("pending", None)
    data.setdefault("history", []).append({"plan": plan, "from": plans.plan_key(org), "topics": topics, "how": how,
                                           "by": by.email if by else "", "at": timezone.now().isoformat(),
                                           **({"promo": promo} if promo else {})})
    org.billing = json.dumps(data, ensure_ascii=False)
    org.plan = plan
    fields = ["billing", "plan", "updated_at"]
    if seats:
        org.seats_purchased = seats
        fields.append("seats_purchased")
    org.save(update_fields=fields)
    if background:
        from .slack_modals import in_background

        in_background(_settle, org, by)
        return
    _settle(org, None)


def _stripe_ready(plan: str) -> bool:
    from . import stripe_billing

    return settings.BILLING_PROVIDER == "stripe" and stripe_billing.ready(plan)


def create_checkout(org: Organization, request_data: dict, by: AIDLUser) -> str:
    """The payment provider's checkout URL (activated later by its webhook)."""
    if settings.BILLING_PROVIDER == "stripe" and _stripe_ready(request_data["plan"]):
        from . import stripe_billing

        return stripe_billing.create_checkout_session(org, by, request_data)
    raise ProviderNotConnected(f"Payment provider '{settings.BILLING_PROVIDER}' is not set up for this plan.")


def unlimited(user: AIDLUser) -> bool:
    return (user.email or "").strip().lower() in settings.BILLING_UNLIMITED_ADMIN_EMAILS


# ---------- internal testing access (promo codes, Slack only) ----------

PROMO_REFUSED = "This code isn't valid for this workspace."
_FREE_ACTIVATIONS = {"promo", "complimentary", "test_mode"}


def internal_workspace(org: Organization) -> bool:
    """One of our own Slack workspaces (settings.INTERNAL_SLACK_TEAMS)."""
    return bool(org.slack_team_id) and org.slack_team_id in settings.INTERNAL_SLACK_TEAMS


def on_promo(org: Organization) -> bool:
    """This organization's current plan came from a code."""
    data = _load(org)
    last = (data.get("history") or [{}])[-1]
    return (org.plan != "trial" and last.get("how") == "promo" and last.get("plan") == org.plan
            and not (data.get("stripe") or {}).get("subscription"))


def _real_subscription(org: Organization) -> bool:
    """Paying (Stripe), or on a paid plan set up by hand for a customer."""
    data = _load(org)
    if (data.get("stripe") or {}).get("subscription"):
        return True
    history = data.get("history") or []
    return org.plan != "trial" and (not history or history[-1].get("how") not in _FREE_ACTIVATIONS)


def promo_available(org: Organization, user: AIDLUser) -> bool:
    """Whether the Slack Admin Center shows Enter promo code: our workspace, and a code is ON."""
    if not internal_workspace(org) or org.plan == "enterprise" or _real_subscription(org):
        return False
    return PromoCode.objects.filter(is_active=True).exists()


def redeem(org: Organization, by: AIDLUser, code: str) -> None:
    """Turn on the code's plan. Raises UpgradeError; never reaches Stripe."""
    if not org.slack_team_id:
        raise UpgradeError({"promo_code": "Promo codes only work for organizations using Slack."})
    code = (code or "").strip().upper()
    row = PromoCode.objects.filter(code=code, is_active=True).first() if code else None
    if row is None or not internal_workspace(org):
        raise UpgradeError({"promo_code": PROMO_REFUSED})
    if on_promo(org):
        raise UpgradeError({"promo_code": "This organization already has testing access."})
    if _real_subscription(org):
        raise UpgradeError({"promo_code": "This organization has a paid subscription, so a promo code can't be used."})
    activate(org, row.plan, [], None, None, by, how="promo", promo=row.code, background=True)


def set_internal_plan(org: Organization, plan: str, by_name: str) -> None:
    """Django Admin: switch one of our own test organizations between Trial and
    Enterprise. The same activation as a code; no payment."""
    if not internal_workspace(org) or plan not in ("trial", "enterprise"):
        raise UpgradeError({"plan": "Only our own test workspaces can be switched, to Trial or Enterprise."})
    activate(org, plan, [], None, None, None, how="promo", promo=f"django-admin:{by_name}")
    from .slack_onboarding import primary_admin

    admin = primary_admin(org)
    if admin is not None:
        refresh_slack(org, admin)


def _promo_label(org: Organization) -> str:
    return ((_load(org).get("history") or [{}])[-1]).get("promo", "")


def change_testing_schedule(org: Organization, by: AIDLUser, days: list[int], hour: int) -> None:
    schedule = {"days": days, "hour": hour}
    errors = validate(org, org.plan, [], schedule, None)
    if errors:
        raise UpgradeError(errors)
    activate(org, org.plan, [], schedule, None, by, how="promo", promo=_promo_label(org), background=True)


def seen_event(org: Organization, event_id: str) -> bool:
    """Remember processed webhook events (Stripe may send one twice)."""
    if not event_id:
        return False
    data = _load(org)
    seen = data.setdefault("events", [])
    if event_id in seen:
        return True
    data["events"] = (seen + [event_id])[-50:]
    _save(org, data)
    return False


def org_for_subscription(subscription_id: str) -> Organization | None:
    if not subscription_id:
        return None
    for org in Organization.objects.filter(billing__contains=subscription_id):
        if (_load(org).get("stripe") or {}).get("subscription") == subscription_id:
            return org
    return None


def downgrade_to_trial(org: Organization, how: str) -> None:
    data = _load(org)
    data.setdefault("history", []).append({"plan": "trial", "from": plans.plan_key(org), "how": how,
                                           "at": timezone.now().isoformat()})
    data.pop("stripe", None)
    org.billing = json.dumps(data, ensure_ascii=False)
    org.plan = "trial"
    org.save(update_fields=["billing", "plan", "updated_at"])
    from .package_delivery import sync_organization

    sync_organization(org)


def checkout(org: Organization, by: AIDLUser, plan: str, topics: list[str], schedule: dict | None,
             seats: int | None) -> dict:
    errors = validate(org, plan, topics, schedule, seats)
    if errors:
        raise UpgradeError(errors)
    if on_promo(org):  # testing access is never sent to payment, whichever page it comes from
        if plans.rank(plan) > plans.rank(org.plan):
            raise UpgradeError({"plan": "Internal testing access doesn't include this plan."})
        activate(org, plan, topics, schedule, seats, by, how="promo", promo=_promo_label(org))
        refresh_slack(org, by)
        return {"status": "activated", "message": "Saved. No payment is involved with internal testing access."}
    request_data = {"plan": plan, "topics": topics, "schedule": schedule, "seats": seats}
    if unlimited(by):
        activate(org, plan, topics, schedule, seats, by, how="complimentary")
        refresh_slack(org, by)
        return {"status": "activated",
                "message": f"Your {plans.PLANS[plan]['label']} plan is active — no payment needed for this account."}
    if settings.BILLING_PROVIDER:
        try:
            return {"status": "redirect", "url": create_checkout(org, request_data, by)}
        except ProviderNotConnected:
            pass
    if settings.BILLING_TEST_MODE:
        activate(org, plan, topics, schedule, seats, by, how="test_mode")
        refresh_slack(org, by)
        return {"status": "activated",
                "message": f"Test mode: your {plans.PLANS[plan]['label']} plan is active — no payment was taken."}
    data = _load(org)
    data["pending"] = {**request_data, "by": by.email, "at": timezone.now().isoformat()}
    _save(org, data)
    _notify_aidl_team(org, by, request_data)
    return {"status": "pending",
            "message": "Online payment is being set up. Your choice is saved and the AIDL team will contact you "
                       "to complete the upgrade."}


def refresh_slack(org: Organization, by: AIDLUser) -> None:
    if not org.slack_channel_id:
        return
    try:
        from .slack_blocks import publish_admin_center

        publish_admin_center(org, by)
    except Exception:  # noqa: BLE001 — the upgrade itself already succeeded
        pass


def _notify_aidl_team(org: Organization, by: AIDLUser, request_data: dict) -> None:
    from django.core.mail import send_mail

    try:
        send_mail(subject=f"AIDL upgrade request: {org.name} → {request_data['plan']}",
                  message=f"{by.full_name or by.email} ({by.email}) asked to upgrade {org.name} to "
                          f"{request_data['plan']}.\nTopics: {', '.join(request_data['topics'])}\n"
                          f"Schedule: {request_data.get('schedule')}\nSeats: {request_data.get('seats')}",
                  from_email=settings.DEFAULT_FROM_EMAIL,
                  recipient_list=[settings.BILLING_SALES_EMAIL or settings.DEFAULT_FROM_EMAIL.split("<")[-1].strip(">")],
                  fail_silently=True)
    except Exception:  # noqa: BLE001
        pass

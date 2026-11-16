"""Internal testing access in Slack (billing.py has the rules). Codes are redeemed only on the website's plans
page; this is the popup that changes the delivery schedule afterwards, without going near the payment page."""

from __future__ import annotations

from . import billing, plans
from .models import AIDLUser, Organization

SCHEDULE = "aidl_promo_schedule"
HOURS = range(6, 21)


def _t(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _note(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _message(title: str, text: str) -> dict:
    return {"type": "modal", "title": _t(title), "close": _t("Close"), "blocks": [_note(text)]}


def closed_view() -> dict:
    return _message("Promo code", "Internal testing access isn't available for this workspace right now.")


def schedule_view(org: Organization) -> dict:
    current = plans.schedule_of(org)
    days = [{"text": _t(name), "value": str(i)} for i, name in enumerate(plans.WEEKDAYS)]
    hours = [{"text": _t(f"{h:02d}:00"), "value": str(h)} for h in HOURS]
    hour = int(current.get("hour", 10))
    return {"type": "modal", "callback_id": SCHEDULE, "title": _t("Change schedule"), "submit": _t("Save"),
            "close": _t("Cancel"), "blocks": [
        _note("Your organization has internal testing access, so nothing here involves payment."),
        {"type": "input", "block_id": "days", "label": _t("Pick 4 delivery days"),
         "element": {"type": "checkboxes", "action_id": "v", "options": days,
                     "initial_options": [d for d in days if int(d["value"]) in current.get("days", ())]}},
        {"type": "input", "block_id": "hour", "label": _t("Delivery time (each person's own time zone)"),
         "element": {"type": "static_select", "action_id": "v", "options": hours,
                     "initial_option": next((h for h in hours if int(h["value"]) == hour), hours[4])}},
    ]}


def _state(view: dict, block: str) -> dict:
    return ((view.get("state") or {}).get("values") or {}).get(block, {}).get("v") or {}


def submit_schedule(admin: AIDLUser, org: Organization, view: dict) -> dict:
    days = [int(o["value"]) for o in _state(view, "days").get("selected_options") or []]
    hour = int((_state(view, "hour").get("selected_option") or {}).get("value") or 0)
    try:
        billing.change_testing_schedule(org, admin, days, hour)
    except billing.UpgradeError as exc:
        return {"response_action": "errors", "errors": {"days": exc.errors.get("schedule", "This can't be changed.")}}
    return {"response_action": "update", "view": _message(
        "Schedule saved", "✅ Saved. People's upcoming cards are being moved to the new days.")}

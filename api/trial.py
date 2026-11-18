"""The Trial (7 days) and how a licence's validity follows the organization's plan.

* The Trial clock starts the day the programme starts (admin_setup.send_item → start()), is kept in
  Organization.billing["trial"] = {started_at, ends_at} and never restarts: an organization that upgraded and
  later cancels comes back to an already finished Trial. (Our own test workspaces can restart it, billing.py.)
* A licence is valid for as long as the plan entitles it to: on Trial until the Trial ends, on a paid plan
  PAID_LICENCE_DAYS from issue — and from every upgrade (extend_licences), which also brings back licences that
  ran out with the Trial. A licence already issued is never shortened by a plan change: cancelling a paid plan
  keeps each licence until its own date; what stops is new licences and awareness cards.
* When the Trial ends (close_trial, run daily / hourly): scheduled awareness cards are cancelled, new licences are
  no longer issued, and the admins are told. Delivery itself also refuses slots after the end
  (package_delivery.within_trial), so nothing depends on the job running on time.
* Organizations that were already on Trial with a running programme before the 7-day Trial existed get one fresh
  Trial from the first time it is looked at (window()), and their licences keep the dates they were issued with.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .models import AIDLUser, Organization

logger = logging.getLogger(__name__)


def trial_days() -> int:
    return int(getattr(settings, "TRIAL_DAYS", 7))


def licence_days() -> int:
    """How long a licence lasts on a paid plan (and for people who join the website on their own)."""
    return int(getattr(settings, "PAID_LICENCE_DAYS", 365))


def _load(org: Organization) -> dict:
    try:
        return json.loads(org.billing or "{}")
    except ValueError:
        return {}


def _save(org: Organization, data: dict) -> None:
    org.billing = json.dumps(data, ensure_ascii=False)
    org.save(update_fields=["billing", "updated_at"])


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def is_trial(org: Organization | None) -> bool:
    from .plans import plan_key

    return org is not None and plan_key(org) == "trial"


def start(org: Organization, when: datetime | None = None) -> tuple[datetime, datetime]:
    """Start the Trial clock (once). Returns (started, ends)."""
    data = _load(org)
    t = data.get("trial") or {}
    started, ends = _when(t.get("started_at")), _when(t.get("ends_at"))
    if started and ends:
        return started, ends
    started = when or timezone.now()
    ends = started + timedelta(days=trial_days())
    data["trial"] = {"started_at": started.isoformat(), "ends_at": ends.isoformat()}
    _save(org, data)
    return started, ends


def window(org: Organization) -> tuple[datetime, datetime] | None:
    """(started, ends), or None while the programme hasn't started."""
    t = _load(org).get("trial") or {}
    started, ends = _when(t.get("started_at")), _when(t.get("ends_at"))
    if started and ends:
        return started, ends
    from .admin_setup import started as programme_started

    if is_trial(org) and programme_started(org):
        return start(org)  # a running programme from before the 7-day Trial: one fresh Trial from now
    return None


def ends_at(org: Organization) -> datetime | None:
    w = window(org) if is_trial(org) else None
    return w[1] if w else None


def expired(org: Organization | None, now: datetime | None = None) -> bool:
    """On Trial, and its days are used up."""
    if not is_trial(org):
        return False
    end = ends_at(org)
    return end is not None and (now or timezone.now()) >= end


def days_left(org: Organization, now: datetime | None = None) -> int | None:
    end = ends_at(org)
    if end is None:
        return None
    seconds = (end - (now or timezone.now())).total_seconds()
    return max(0, int(-(-seconds // 86400)))


def summary(org: Organization) -> dict | None:
    """What the Admin Center, the plans page and the pricing copy show about the Trial (None on a paid plan)."""
    if not is_trial(org):
        return None
    end = ends_at(org)
    return {"days": trial_days(), "started": end is not None, "ends_at": end.isoformat() if end else "",
            "days_left": days_left(org) if end else trial_days(), "expired": expired(org)}


def line(org: Organization) -> str:
    """One short sentence: "7-day Trial: starts when you start the programme", "3 days left", "ended"."""
    s = summary(org)
    if s is None:
        return ""
    if not s["started"]:
        return f"{s['days']}-day Trial, starting the day you start the programme"
    if s["expired"]:
        return "Your Trial has ended"
    left = s["days_left"]
    return f"{left} day{'s' if left != 1 else ''} left of your {s['days']}-day Trial"


# ---------- licence validity ----------

def licence_valid_until(org: Organization | None, now: datetime | None = None) -> datetime | None:
    """When a licence issued now stops being valid: the end of the Trial, or PAID_LICENCE_DAYS on a paid plan.
    None = the Trial is over, so no licence can be issued."""
    now = now or timezone.now()
    if org is None or not is_trial(org):
        return now + timedelta(days=licence_days())
    w = window(org) or start(org, now)
    return w[1] if w[1] > now else None


def extend_licences(org: Organization, now: datetime | None = None) -> int:
    """After an upgrade: every issued licence is valid at least PAID_LICENCE_DAYS from now (never shortened).
    Members who were taken out of the channel keep their suspension; only the date moves."""
    now = now or timezone.now()
    until = now + timedelta(days=licence_days())
    changed = 0
    for member in AIDLUser.objects.filter(organization_id=str(org.pk), licence_issued=True).exclude(
            role=AIDLUser.Role.ADMIN):
        if member.licence_expires_at is None or member.licence_expires_at < until:
            member.licence_expires_at = until
            member.save(update_fields=["licence_expires_at", "updated_at"])
            changed += 1
    return changed


def consume(org: Organization, now: datetime | None = None) -> None:
    """A paid plan starts: the Trial is used up whether or not it was running, so cancelling later returns the
    organization to a finished Trial, not a new one."""
    now = now or timezone.now()
    data = _load(org)
    t = data.get("trial") or {}
    started, ends = _when(t.get("started_at")), _when(t.get("ends_at"))
    if started and ends and ends <= now:
        return
    data["trial"] = {"started_at": (started or now).isoformat(), "ends_at": now.isoformat(), "ended_by": "upgrade"}
    _save(org, data)


def mark_closed(org: Organization) -> None:
    """Record that the end of this Trial needs no announcement (the organization came back from a paid plan)."""
    data = _load(org)
    t = data.get("trial") or {}
    if t and not t.get("closed_at"):
        t["closed_at"] = timezone.now().isoformat()
        data["trial"] = t
        _save(org, data)


def restart(org: Organization) -> None:
    """Our own test workspaces only (billing.set_internal_plan): Trial again, from the next programme start."""
    data = _load(org)
    if data.pop("trial", None) is not None:
        _save(org, data)


def on_plan_change(org: Organization, old_plan: str, new_plan: str) -> None:
    """Called by billing.activate after the plan is saved."""
    if new_plan == "trial":
        return
    consume(org)
    extend_licences(org)


# ---------- the end of the Trial ----------

def close_trial(org: Organization) -> bool:
    """Once, when the Trial is over: cancel the awareness cards still scheduled, tell the admins, redraw the Admin
    Center. Safe to call again. Returns True the time it did something."""
    if not expired(org):
        return False
    data = _load(org)
    t = data.get("trial") or {}
    if t.get("closed_at"):
        return False
    t["closed_at"] = timezone.now().isoformat()
    data["trial"] = t
    _save(org, data)
    from .licensing import learners
    from .package_delivery import pause_package

    cancelled = 0
    for member in learners(org).filter(licence_issued=True):
        try:
            cancelled += pause_package(org, member)
        except Exception:  # noqa: BLE001 — one person's Slack call must not stop the rest
            logger.exception("trial end: cancelling cards for %s", member.pk)
    _tell_admins(org)
    try:
        from .admin_home import refresh

        refresh(org)
    except Exception:  # noqa: BLE001
        logger.exception("trial end: redrawing the Admin Center")
    logger.info("trial of org %s ended: %s scheduled cards cancelled", org.pk, cancelled)
    return True


def _tell_admins(org: Organization) -> None:
    if not org.slack_team_id:
        return  # Teams admins see it on their Admin Center page
    from . import slack_client
    from .slack_onboarding import primary_admin

    admin = primary_admin(org)
    token = slack_client.bot_token(org)
    if admin is None or not token or not admin.microsoft_id.startswith("slack:"):
        return
    text = (f"⏰ *{org.name}'s {trial_days()}-day AIDL Trial has ended.* Awareness cards have stopped and new people "
            "can't earn a licence until you upgrade. Everything your team has done is kept, and upgrading to "
            "Enterprise or Basic extends their licences. Open *Upgrade plan* in the Admin Center.")
    slack_client.send_dm(token, org, admin.microsoft_id.split(":")[-1], text="Your AIDL Trial has ended",
                         blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])


def close_finished_trials() -> int:
    """Daily / hourly job: close every Trial that has ended."""
    closed = 0
    for org in Organization.objects.filter(is_active=True).exclude(plan__in=("basic", "enterprise")):
        try:
            closed += close_trial(org)
        except Exception:  # noqa: BLE001
            logger.exception("closing trial of org %s", org.pk)
    return closed

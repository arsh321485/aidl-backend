"""Step 2: the learner flow inside Moodle, and the small teacher box that starts it.

Same rules as Slack and Teams (api/teams_portal.py `me` / `me_action`); this
file only *calls* api/ — nothing about cards or licences is decided here:

    Welcome → policy cards (I agree) → 🚦 Traffic Light Check → 📖 Highway Code
            → licence (issued when every card is done and a seat is free)
            → avatar (the website's avatar page) → licence image

Before learners can start, an admin (the first teacher) must approve the
Traffic Light Check and the Highway Code and start the programme. Until the
full Moodle Admin Center (step 4) exists, the teacher's home page does that.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from api import admin_setup, aup_store, card_approval, card_image, licence_image, policy_awareness
from api.models import Acknowledgement, AIDLUser, Organization

logger = logging.getLogger(__name__)
ONBOARDING = card_approval.ONBOARDING  # ("traffic_light", "highway_code")


def _first(user: AIDLUser) -> str:
    return (user.first_name or user.full_name or "there").split()[0]


# ---------- learner ----------

def learner_context(user: AIDLUser, org: Organization) -> dict:
    """What moodle/learner.html needs. `blocked` = a full-page message instead."""
    from api.avatar import can_change, start_url
    from api.employee_flow import stage
    from api.plans import cadence
    from api.policy_cards import ICONS, SHORT

    if user.slack_left_at:  # "removed from the programme" (the field is shared by every platform)
        return {"blocked": ("You're no longer in the programme", f"You were removed from {org.name}'s AIDL "
                            "programme, so your licence is suspended. Your AIDL admin can add you back.")}
    if not admin_setup.started(org):
        return {"blocked": ("Nothing to do yet", f"{org.name}'s AIDL admin (your teacher) is still setting things "
                            "up. Open AIDL from your course again a little later.")}

    now = stage(user, org)
    ctx = {"stage": now, "first": _first(user), "avatar_url": start_url(user), "can_change_avatar": can_change(user),
           "welcome": not user.slack_onboarded_at, "cadence": cadence(org)[:1].upper() + cadence(org)[1:]}
    versions = aup_store.required_points(org)
    if ctx["welcome"]:
        ctx["policy_count"] = len(versions)
        return ctx

    ctx["policy"] = [{"n": n, "title": SHORT.get(n, ""), "icon": ICONS.get(n, ""), "version": versions[n],
                      "image": card_image.image_url(f"aup-{n}", org)} for n in aup_store.pending_points(user, org)]
    for item, kind in (("traffic_light", "traffic"), ("highway_code", "highway")):
        if now == item:
            ctx["waiting"] = not card_approval.is_approved(org, item)
            ctx[kind] = card_image.image_url(item, org)
    if now == "licence" and user.licence_issued and user.avatar_data:
        ctx["licence"] = _licence(user, org)
    acked = {i[4:] for i in Acknowledgement.objects.filter(user_id=str(user.pk), item__startswith="paw_")
             .values_list("item", flat=True)}
    ctx["awareness"] = [{**c, "image": card_image.image_url(f"paw-{c['id']}", org)}
                        for c in policy_awareness.approved(org) if user.licence_issued and c["id"] not in acked]
    return ctx


def _licence(user: AIDLUser, org: Organization) -> dict:
    version = licence_image.url_version(user, org)
    verify = licence_image.share_url(user.licence_number)
    text, link = quote(f"I just earned my AI Driving Licence from {org.name}!"), quote(verify, safe="")
    return {
        "link": verify,
        "image": licence_image.image_url(user.licence_number, version=version),
        "download": licence_image.image_url(user.licence_number, download=True, version=version),
        "share": f"https://www.linkedin.com/sharing/share-offsite/?url={link}",
        "share_x": f"https://twitter.com/intent/tweet?text={text}&url={link}",
        "share_facebook": f"https://www.facebook.com/sharer/sharer.php?u={link}",
        "share_whatsapp": f"https://wa.me/?text={text}%20{link}",
    }


def learner_action(user: AIDLUser, org: Organization, post) -> str:
    """One button in the flow. Returns a notice to show ("" = none)."""
    from api.employee_flow import stage
    from api.licensing import acknowledge, issue_licence_if_ready

    if user.role == AIDLUser.Role.ADMIN or user.slack_left_at or not admin_setup.started(org):
        return ""
    action = post.get("action", "")
    if action == "start":
        if not user.slack_onboarded_at:
            user.slack_onboarded_at = timezone.now()
            user.save(update_fields=["slack_onboarded_at", "updated_at"])
        return ""
    if action == "paw":
        policy_awareness.acknowledge(org, user, post.get("id", ""), "", "")
        return ""
    if action == "agree":
        n = int(post["n"]) if post.get("n", "").isdigit() else 0
        current = aup_store.required_points(org).get(n)
        if not current:
            return "That card isn't in use any more."
        if post.get("version") != current:
            return "This card was updated while you were reading. Please read the new version."
        acknowledge(user, aup_store.point_item(n), current)
    elif action in ONBOARDING and stage(user, org) == action and card_approval.is_approved(org, action):
        # Only the card the learner is on: no skipping ahead with an old form.
        acknowledge(user, action)
    else:
        return ""

    if stage(user, org) == "licence" and not user.licence_issued:
        result = issue_licence_if_ready(user, org)
        user.refresh_from_db()
        if result == "no_seats":
            logger.warning("moodle: %s finished every card but org %s has no free seats", user.pk, org.pk)
    return ""


# ---------- teacher (admin) ----------

def teacher_context(org: Organization) -> dict:
    from api.licensing import learners

    people = learners(org)
    return {
        "onboarding": card_approval.onboarding(org),
        "ready": card_approval.onboarding_ready(org),
        "started": admin_setup.started(org),
        "learner_count": people.count(),
        "licensed_count": people.filter(licence_issued=True).count(),
    }


def teacher_action(user: AIDLUser, org: Organization, post) -> str:
    """Approve the two onboarding cards / start the programme. Returns a notice."""
    if user.role != AIDLUser.Role.ADMIN:
        return "Only your school's AIDL admins can do this."
    action = post.get("action", "")
    if action == "approve":
        item = post.get("item", "")
        if item not in ONBOARDING:
            return ""
        card_approval.approve_onboarding(org, item, user)
        return f"{card_approval.ONBOARDING_LABEL[item]} approved."
    if action == "start":
        if not card_approval.onboarding_ready(org):
            return "Approve the Traffic Light Check and the Highway Code first."
        if not admin_setup.started(org):
            # Not admin_setup.send_item(): that posts Slack / Teams messages. Moodle
            # learners see their first card the next time they open AIDL.
            now = timezone.now().isoformat()
            admin_setup.update(org, flow_started=now, aup_sent=now, traffic_light_sent=now, highway_code_sent=now)
        return "The programme has started. Learners see their first card when they open AIDL from the course."
    return ""

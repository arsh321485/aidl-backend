"""The admin's step-by-step setup (Slack Admin Center):

  1. Create the AI policy (AUP) — or ask someone else to
  2. Add admins (optional)
  3. Add your team
  4. Send the learning cards: AUP, Traffic Light Check, Highway Code
  5. Licences & awareness cards

Learning cards reach employees only after the admin sends each one the
first time; anyone who joins later gets the already-sent cards
automatically. Progress lives in Organization.setup_state (JSON).
"""

from __future__ import annotations

import json

from django.utils import timezone

from . import slack_client
from .models import Acknowledgement, AIDLUser, Organization

ITEMS = ("aup", "traffic_light", "highway_code")
ITEM_TAB = {"aup": "aup", "traffic_light": "traffic-light", "highway_code": "highway-code"}
ITEM_LABEL = {"aup": "📄 AI Acceptable Use Policy", "traffic_light": "🚦 Traffic Light Check",
              "highway_code": "📖 Highway Code"}


def state(org: Organization) -> dict:
    try:
        return json.loads(org.setup_state or "{}")
    except ValueError:
        return {}


def update(org: Organization, **values) -> dict:
    data = {**state(org), **values}
    org.setup_state = json.dumps(data)
    org.save(update_fields=["setup_state", "updated_at"])
    return data


def is_sent(org: Organization, item: str) -> bool:
    return bool(state(org).get(f"{item}_sent"))


def has_acknowledged(member: AIDLUser, item: str) -> bool:
    """Accepted the card. For the AUP: all 14 points of the current version."""
    if item == "aup":
        from .aup_store import all_points_accepted
        from .org_service import get_organization_for_user

        return all_points_accepted(member, get_organization_for_user(member))
    return Acknowledgement.objects.filter(user_id=str(member.pk), item=item).exists()


def visible_to(member: AIDLUser, org: Organization, item: str) -> bool:
    """Employees see a learning card once the admin has sent it (or if they
    already accepted it before)."""
    return is_sent(org, item) or has_acknowledged(member, item)


def first_tab(member: AIDLUser, org: Organization) -> str:
    """The dashboard tab a new joiner's message opens on."""
    for item in ITEMS:
        if visible_to(member, org, item):
            return ITEM_TAB[item]
    return "home"


def _slack_learners(org: Organization):
    from .licensing import learners

    return learners(org).filter(microsoft_id__startswith=f"slack:{org.slack_team_id}:")


def send_item(org: Organization, item: str) -> tuple[int, int]:
    """DM every team member the card (their dashboard opened on it) and mark
    it sent, so later joiners get it automatically. Returns (sent, total)."""
    from .slack_blocks import user_dashboard_blocks

    sent_info = {f"{item}_sent": timezone.now().isoformat()}
    if item == "aup":  # remember which version went out, to spot later changes
        from .aup_store import version

        sent_info["aup_sent_version"] = version(org)
    update(org, **sent_info)
    token = slack_client.bot_token(org)
    members = list(_slack_learners(org))
    sent = 0
    for member in members:
        result = slack_client.send_dm(token, org, member.microsoft_id.split(":")[-1],
                                      text=ITEM_LABEL[item], blocks=user_dashboard_blocks(member, org, ITEM_TAB[item]))
        sent += 1 if result.get("ok") or result.get("fallback_ok") else 0
    return sent, len(members)


def remind_item(org: Organization, item: str) -> tuple[int, int]:
    """DM only the team members who haven't accepted the card yet."""
    from .slack_blocks import user_dashboard_blocks

    token = slack_client.bot_token(org)
    pending = [m for m in _slack_learners(org) if not has_acknowledged(m, item)]
    sent = 0
    for member in pending:
        result = slack_client.send_dm(token, org, member.microsoft_id.split(":")[-1],
                                      text=f"Reminder: {ITEM_LABEL[item]}",
                                      blocks=user_dashboard_blocks(member, org, ITEM_TAB[item]))
        sent += 1 if result.get("ok") or result.get("fallback_ok") else 0
    return sent, len(pending)


def aup_change(org: Organization, st: dict | None = None) -> dict:
    """{'changed': True, 'sent_version', 'version'} when the AUP was changed
    (answers or apps) after it was sent to the team."""
    st = state(org) if st is None else st
    if not (st.get("aup_sent") and st.get("aup_sent_version")):
        return {"changed": False}
    from .aup_store import version

    current = version(org)
    if current == st["aup_sent_version"]:
        return {"changed": False}
    return {"changed": True, "sent_version": st["aup_sent_version"], "version": current}


def accepted_count(org: Organization, item: str) -> int:
    from .licensing import ACK_VERSION

    ids = [str(pk) for pk in _slack_learners(org).values_list("pk", flat=True)]
    if item == "aup":  # everyone who accepted all 14 points of the current AUP
        from .aup_store import POINTS, version

        ver = version(org)
        if not ver:
            return 0
        per_user: dict[str, int] = {}
        for uid in Acknowledgement.objects.filter(user_id__in=ids, version=ver,
                                                  item__startswith="aup_").values_list("user_id", flat=True):
            per_user[uid] = per_user.get(uid, 0) + 1
        return sum(1 for n in per_user.values() if n >= POINTS)
    return Acknowledgement.objects.filter(user_id__in=ids, item=item, version=ACK_VERSION).count()


def summary(org: Organization) -> dict:
    """Everything the setup steps show."""
    from .aup_store import draft, has_draft, is_complete

    st = state(org)
    delegate = AIDLUser.objects.filter(pk=st["aup_delegate"]).first() if st.get("aup_delegate") else None
    members = _slack_learners(org).count()
    admins = AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True).count()
    sends = {item: {"sent": bool(st.get(f"{item}_sent")), "accepted": accepted_count(org, item), "changed": False}
             for item in ITEMS}
    sends["aup"].update(aup_change(org, st))
    aup_done = is_complete(org)
    admins_done = bool(st.get("admins_step")) or admins > 1
    team_done = members > 0
    cards_done = all(s["sent"] for s in sends.values())
    return {
        "aup_done": aup_done,
        "aup_draft_section": draft(org)["section"] if not aup_done and has_draft(org) else None,
        "delegate_name": (delegate.full_name or delegate.email) if delegate and not aup_done else "",
        "admins_done": admins_done,
        "team_done": team_done,
        "cards_done": cards_done,
        "members": members,
        "sends": sends,
        "done_count": sum([aup_done, admins_done, team_done, cards_done]),
    }


def reset(org: Organization, *, answers: bool = True, fresh: bool = False) -> dict:
    """Start the setup again (demos): forget the sends, the AUP and everyone's
    AUP acceptance. With fresh=True every team member starts from zero too:
    all acceptances (Highway Code, Traffic Light, AUP points) and licences
    are removed, and scheduled awareness cards are cancelled."""
    from .models import PackageDelivery

    acks = Acknowledgement.objects.filter(organization_id=str(org.pk))
    if not fresh:
        acks = acks.filter(item__startswith="aup")
    removed = acks.delete()[0]
    AIDLUser.objects.filter(organization_id=str(org.pk)).update(aup_signed=False, aup_signed_at=None)
    result = {"acceptances_removed": removed, "answers_cleared": answers}
    if fresh:
        from .package_delivery import pause_package

        licensed = AIDLUser.objects.filter(organization_id=str(org.pk), licence_issued=True).exclude(
            role=AIDLUser.Role.ADMIN)
        for member in licensed:
            pause_package(org, member)
        PackageDelivery.objects.filter(organization_id=str(org.pk)).delete()
        result["licences_removed"] = licensed.update(licence_issued=False, licence_number="", licence_issued_at=None,
                                                     licence_expires_at=None)
    org.setup_state = ""
    fields = ["setup_state", "updated_at"]
    if answers:
        org.policy_answers = ""
        org.aup_data = ""
        fields += ["policy_answers", "aup_data"]
    org.save(update_fields=fields)
    return result

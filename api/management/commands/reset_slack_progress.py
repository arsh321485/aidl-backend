"""Start every Slack organization from nothing. There's no undo and no backup.

    python manage.py reset_slack_progress            # preview: shows the counts, changes nothing
    python manage.py reset_slack_progress --confirm  # does it

Removes messages waiting in Slack's schedule, agreements, licences, avatars, card
approvals, policy answers and cards, uploads, app lists and requests, deliveries
and setup progress. Keeps the people, admins and their permissions, the plan,
the company logo and the Slack connection. Teams organizations aren't touched.
Messages already posted stay in Slack: a bot can't reach into every chat.
"""

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from api import slack_client
from api.models import (Acknowledgement, AIDLUser, AppRequest, AupUpload, CardApproval, CardCustomRequest, CardImage,
                        CardRequest, Organization, PackageDelivery, PolicyVersion, RegisteredApp, TrafficLightVote)

BY_ORG = (Acknowledgement, PackageDelivery, CardApproval, AupUpload, AppRequest, RegisteredApp, TrafficLightVote,
          PolicyVersion, CardRequest, CardCustomRequest)
# The bot is gone from this workspace, so Slack won't post what it had scheduled.
BOT_GONE = {"account_inactive", "token_revoked", "invalid_auth", "not_authed", "missing_token"}


def _call(method: str, token: str, **kwargs) -> dict:
    for _ in range(8):
        result = slack_client.slack_api(method, token, **kwargs)
        if result.get("error") != "ratelimited":
            return result
        time.sleep(15)
    return result


def scheduled_messages(token: str) -> list[dict]:
    """Everything this bot has waiting in Slack. Raises if Slack won't say."""
    found, cursor = [], ""
    while True:
        result = _call("chat.scheduledMessages.list", token, json={"limit": 100, **({"cursor": cursor} if cursor else {})})
        if not result.get("ok"):
            if result.get("error") in BOT_GONE:
                return []
            raise RuntimeError(result.get("error") or "no answer")
        found += result.get("scheduled_messages") or []
        cursor = (result.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return found


def licence_numbers(org: Organization) -> list[str]:
    return [n for n in AIDLUser.objects.filter(organization_id=str(org.pk)).values_list("licence_number", flat=True) if n]


def image_rows(org: Organization):
    query = Q(key__startswith=f"card:{org.pk}:")
    for number in licence_numbers(org):
        query |= Q(key__startswith=f"lic:{number}:")
    return CardImage.objects.filter(query)


def counts(org: Organization) -> dict:
    people = AIDLUser.objects.filter(organization_id=str(org.pk))
    out = {model.__name__: model.objects.filter(organization_id=str(org.pk)).count() for model in BY_ORG}
    out.update(CardImage=image_rows(org).count(), licences=people.filter(licence_issued=True).count(),
               avatars=people.exclude(avatar_data="").count(), people_kept=people.count(),
               admins_kept=people.filter(role=AIDLUser.Role.ADMIN).count())
    return {k: v for k, v in out.items() if v}


def reset_org(org: Organization, token: str, waiting: list[dict]) -> dict:
    """`waiting`: from scheduled_messages. Slack first: if it fails, nothing is deleted."""
    failed = 0
    for message in waiting:
        result = _call("chat.deleteScheduledMessage", token,
                       json={"channel": message.get("channel_id"), "scheduled_message_id": message.get("id")})
        failed += 0 if result.get("ok") or result.get("error") == "invalid_scheduled_message_id" else 1
    image_rows(org).delete()
    for model in BY_ORG:
        model.objects.filter(organization_id=str(org.pk)).delete()
    AIDLUser.objects.filter(organization_id=str(org.pk)).update(
        licence_issued=False, licence_number="", licence_issued_at=None, licence_expires_at=None,
        aup_signed=False, aup_signed_at=None, avatar_url="", avatar_data="", avatar_config="",
        avatar_changed_at=None, slack_onboarded_at=None)
    org.setup_state = org.policy_answers = org.aup_data = ""
    org.save(update_fields=["setup_state", "policy_answers", "aup_data", "updated_at"])
    return {"cancelled": len(waiting) - failed, "failed": failed}


class Command(BaseCommand):
    help = "Remove all progress from every Slack organization (plan, logo and admins stay). No undo."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true", help="really do it; without this it only shows the counts")

    def handle(self, *args, **options):
        confirm = options["confirm"]
        problems = 0
        for org in Organization.objects.exclude(slack_team_id=""):
            token = slack_client.bot_token(org)
            try:
                waiting = scheduled_messages(token) if token else []
            except RuntimeError as exc:  # those messages would still arrive, so leave this one alone
                problems += 1
                self.stderr.write(f"{org.name} ({org.slack_team_id}): SKIPPED, Slack didn't list scheduled messages: {exc}")
                continue
            self.stdout.write(f"{org.name} ({org.slack_team_id}) | {org.plan} | waiting in Slack: {len(waiting)} | {counts(org)}")
            if not confirm:
                continue
            result = reset_org(org, token, waiting)
            problems += 1 if result["failed"] else 0
            self.stdout.write(f"  reset: {result}")
            try:
                from api.slack_blocks import publish_admin_center
                from api.slack_onboarding import primary_admin

                admin = primary_admin(org)
                if admin is not None and token:
                    publish_admin_center(org, admin)
            except Exception as exc:  # noqa: BLE001 — the reset itself is done
                self.stderr.write(f"  Admin Center not refreshed: {type(exc).__name__}")
        self.stdout.write("Done." if confirm else "Preview only: nothing changed. Add --confirm to do it.")
        if problems:
            self.stderr.write(f"{problems} organization(s) need another look: run it again.")

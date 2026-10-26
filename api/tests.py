"""Admin Center backend tests — permission chips, policy versioning, the
Cards catalogue (quota/request/send/schedule), Add Application, and the
invite-email HTML-link regression."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

# TransactionTestCase, not TestCase: django-mongodb-backend's per-test atomic()
# wrapping (what plain TestCase relies on for rollback) reliably hangs against
# this project's Atlas cluster instead of erroring — confirmed by isolating a
# single test class, where the plain-TestCase run never returned but the
# TransactionTestCase run finished in ~12s/test. TransactionTestCase instead
# flushes collections after each test, which is slower per-test but actually
# completes.
from django.test import SimpleTestCase, TransactionTestCase as TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .auth_jwt import create_access_token
from .cards_catalog_data import MONTHLY_CARD_QUOTA
from .cards_service import CardsError, dispatch_due_scheduled_cards, quota_for_org, request_card, schedule_card
from .models import AIDLUser, CardRequest, Organization, PolicyVersion, RegisteredApp
from .teams_invites import _send_invite_email


def make_org(**kwargs) -> Organization:
    defaults = dict(
        name=f"Org {uuid.uuid4().hex[:8]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        admin_seat_limit=3,
    )
    defaults.update(kwargs)
    return Organization.objects.create(**defaults)


def _shown_views(calls) -> list:
    """Popups as the admin sees them: popup buttons first open a "Loading…"
    popup (views.open) and then fill it in (views.update)."""
    return [b["view"] for m, b in calls if m in ("views.open", "views.push", "views.update")
            and b["view"].get("callback_id") != "aidl_loading"]


AUP_BASE = {"0.1": "Acme Trading W.L.L.", "0.3": "0.3.a", "0.4": "0.4.b", "0.5": "Bahrain", "0.6": ["0.6.b"],
            "0.8": "it@acme.example", "0.9": "2026-11-01", "5.4": "5.4.c", "7.2": "ChatGPT Enterprise",
            "8.5": "the Communications team"}
AUP_FILLS = {"6.2.b": "SharePoint", "9.2.a": "security@acme.example"}


def _answer_policy(org: Organization, **changes) -> dict:
    """Publish a complete question-bank AUP (recommended answers)."""
    from . import aup_builder, aup_store

    answers = aup_builder.recommended_answers()
    answers.update(AUP_BASE)
    answers.update(changes)
    aup_store.save_draft(org, answers, dict(AUP_FILLS), 14)
    return aup_store.publish(org)


def _approve_all(org: Organization) -> None:
    """An admin approved every card (onboarding + the plan's package)."""
    from . import card_approval

    for item in card_approval.ONBOARDING:
        card_approval.approve_onboarding(org, item, None)
    card_approval.approve_waiting(org, None)


def _accept_aup(member: AIDLUser, org: Organization) -> None:
    """The employee accepts all 14 points (publishes an AUP if needed)."""
    from . import aup_store
    from .licensing import acknowledge

    org.refresh_from_db()
    if not aup_store.is_complete(org):
        _answer_policy(org)
        org.refresh_from_db()
    for n in range(1, aup_store.POINTS + 1):
        acknowledge(member, aup_store.point_item(n), aup_store.version(org))


def make_user(org: Organization, *, role=AIDLUser.Role.ADMIN, **kwargs) -> AIDLUser:
    defaults = dict(
        microsoft_id=uuid.uuid4().hex,
        email=f"{uuid.uuid4().hex[:8]}@northwind.example",
        full_name="Test Admin",
        role=role,
        organization_id=str(org.pk),
        organization_name=org.name,
        is_active=True,
    )
    defaults.update(kwargs)
    return AIDLUser.objects.create(**defaults)


class AddAdminPermissionsTests(TestCase):
    def setUp(self):
        self.org = make_org()
        self.caller = make_user(self.org, role=AIDLUser.Role.ADMIN)
        self.target = make_user(self.org, role=AIDLUser.Role.LEARNER)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + create_access_token(self.caller))

    def test_promote_sets_chosen_permissions(self):
        resp = self.client.post(
            "/api/teams/admin/invite/",
            {"email": self.target.email, "permissions": {"approve_apps": False, "access_cards": True, "create_card": False}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, AIDLUser.Role.ADMIN)
        self.assertFalse(self.target.perm_approve_apps)
        self.assertTrue(self.target.perm_access_cards)
        self.assertFalse(self.target.perm_create_card)
        self.assertEqual(resp.data["admin"]["permissions"], {"approve_apps": False, "access_cards": True, "create_card": False})

    def test_promote_defaults_permissions_when_omitted(self):
        resp = self.client.post("/api/teams/admin/invite/", {"email": self.target.email}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.target.refresh_from_db()
        # Defaults from the model (True) are untouched when the caller sends nothing.
        self.assertTrue(self.target.perm_approve_apps)
        self.assertTrue(self.target.perm_access_cards)
        self.assertTrue(self.target.perm_create_card)

    def test_non_admin_cannot_promote(self):
        learner = make_user(self.org, role=AIDLUser.Role.LEARNER)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + create_access_token(learner))
        resp = client.post("/api/teams/admin/invite/", {"email": self.target.email}, format="json")
        self.assertEqual(resp.status_code, 403)


class PolicyUploadTests(TestCase):
    def setUp(self):
        self.org = make_org()
        self.admin = make_user(self.org, role=AIDLUser.Role.ADMIN)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + create_access_token(self.admin))

    def _pdf_upload(self, name="aup.pdf", content=b"%PDF-1.4 fake pdf body"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, content, content_type="application/pdf")

    def test_upload_becomes_live_and_roundtrips(self):
        resp = self.client.post(
            "/api/teams/admin/policy/upload/",
            {"file": self._pdf_upload(), "version": "v3.2", "effective_date": "2026-10-01"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        version_id = resp.data["policy_version_id"]

        version = PolicyVersion.objects.get(pk=version_id)
        self.assertTrue(version.is_live)
        self.assertEqual(version.version, "v3.2")

        file_resp = self.client.get(f"/api/teams/admin/policy/file/{version_id}/")
        self.assertEqual(file_resp.status_code, 200)
        self.assertEqual(file_resp["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(file_resp.streaming_content) if file_resp.streaming else file_resp.content, b"%PDF-1.4 fake pdf body")

    def test_second_upload_replaces_live_flag(self):
        first = self.client.post(
            "/api/teams/admin/policy/upload/", {"file": self._pdf_upload("a.pdf")}, format="multipart"
        ).data
        second = self.client.post(
            "/api/teams/admin/policy/upload/", {"file": self._pdf_upload("b.pdf")}, format="multipart"
        ).data
        self.assertFalse(PolicyVersion.objects.get(pk=first["policy_version_id"]).is_live)
        self.assertTrue(PolicyVersion.objects.get(pk=second["policy_version_id"]).is_live)

    def test_non_pdf_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        resp = self.client.post("/api/teams/admin/policy/upload/", {"file": bad}, format="multipart")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "pdf_required")

    def test_policy_tab_reflects_uploaded_version(self):
        self.client.post(
            "/api/teams/admin/policy/upload/",
            {"file": self._pdf_upload(), "version": "v9", "effective_date": "2026-11-01"},
            format="multipart",
        )
        resp = self.client.get(f"/api/teams/admin/policy/?email={self.admin.email}")
        self.assertEqual(resp.status_code, 200)
        placeholder = resp.data["placeholder"]
        self.assertEqual(placeholder["policy_version"], "v9")
        self.assertIn("/api/teams/admin/policy/file/", placeholder["policy_url"])


class CardsCatalogueTests(TestCase):
    def setUp(self):
        self.org = make_org()
        self.admin = make_user(self.org, role=AIDLUser.Role.ADMIN)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + create_access_token(self.admin))

    def test_request_card_then_reflected_in_catalog(self):
        resp = self.client.post("/api/teams/admin/cards/c1/request/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "requested")

        tab = self.client.get(f"/api/teams/admin/cards/?email={self.admin.email}")
        catalog = tab.data["placeholder"]["catalog"]
        row = next(c for c in catalog if c["id"] == "c1")
        self.assertEqual(row["status"], "requested")
        self.assertEqual(tab.data["placeholder"]["quota_used"], 1)

    def test_unknown_card_id_rejected(self):
        resp = self.client.post("/api/teams/admin/cards/does-not-exist/request/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "unknown_card")

    def test_quota_blocks_after_limit(self):
        for i in range(MONTHLY_CARD_QUOTA):
            CardRequest.objects.create(organization_id=str(self.org.pk), card_id="c0", requested_by_email=self.admin.email)
        self.assertEqual(quota_for_org(self.org)["used"], MONTHLY_CARD_QUOTA)
        with self.assertRaises(CardsError) as ctx:
            request_card(self.org, card_id="c1", by_email=self.admin.email)
        self.assertEqual(ctx.exception.code, "quota_exceeded")

        resp = self.client.post("/api/teams/admin/cards/c1/request/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "quota_exceeded")

    def test_access_cards_permission_required(self):
        self.admin.perm_access_cards = False
        self.admin.save(update_fields=["perm_access_cards"])
        resp = self.client.post("/api/teams/admin/cards/c1/request/", {}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data["error"], "permission_denied")

    @patch("api.cards_service.get_access_token_for_user", return_value="fake-token")
    @patch("api.teams_messaging.send_channel_adaptive_card", return_value={"ok": True, "message_id": "m1"})
    def test_send_now_success(self, mock_send, mock_token):
        self.admin.teams_team_id = "team-1"
        self.org.teams_welcome_channel_id = "chan-1"
        self.admin.save(update_fields=["teams_team_id"])
        self.org.save(update_fields=["teams_welcome_channel_id"])

        resp = self.client.post("/api/teams/admin/cards/c2/send/", {"when": "now"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "sent")
        mock_send.assert_called_once()
        self.assertTrue(CardRequest.objects.filter(organization_id=str(self.org.pk), card_id="c2", status="sent").exists())

    def test_send_now_without_channel_configured_fails_cleanly(self):
        resp = self.client.post("/api/teams/admin/cards/c2/send/", {"when": "now"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "channel_not_configured")

    def test_schedule_requires_future_time(self):
        past = (timezone.now() - timedelta(hours=1)).isoformat()
        resp = self.client.post("/api/teams/admin/cards/c3/send/", {"when": "later", "at": past}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "invalid_schedule")

    def test_schedule_future_time_creates_scheduled_row(self):
        future = (timezone.now() + timedelta(hours=2)).isoformat()
        resp = self.client.post("/api/teams/admin/cards/c3/send/", {"when": "later", "at": future}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["status"], "scheduled")

    def test_request_new_card(self):
        resp = self.client.post(
            "/api/teams/admin/cards/request-new/",
            {"title": "Vendor Risk Checklist", "description": "For third-party AI vendors"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["title"], "Vendor Risk Checklist")

    def test_request_new_card_requires_title(self):
        resp = self.client.post("/api/teams/admin/cards/request-new/", {"title": "  "}, format="json")
        self.assertEqual(resp.status_code, 400)


class DispatchScheduledCardsTests(TestCase):
    def setUp(self):
        self.org = make_org(teams_welcome_channel_id="chan-9")
        self.admin = make_user(self.org, role=AIDLUser.Role.ADMIN, teams_team_id="team-9")

    @patch("api.cards_service.get_access_token_for_user", return_value="fake-token")
    @patch("api.teams_messaging.send_channel_adaptive_card", return_value={"ok": True, "message_id": "m1"})
    def test_due_card_gets_sent(self, mock_send, mock_token):
        row = schedule_card(
            self.org,
            card_id="c4",
            by_email=self.admin.email,
            scheduled_at=timezone.now() + timedelta(minutes=1),
        )
        # Not due yet.
        result = dispatch_due_scheduled_cards(now=timezone.now())
        self.assertEqual(result["sent"], 0)
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")

        # Due now.
        result = dispatch_due_scheduled_cards(now=timezone.now() + timedelta(minutes=2))
        self.assertEqual(result["sent"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        mock_send.assert_called_once()

    def test_due_card_without_refresh_token_stays_scheduled_with_error(self):
        row = schedule_card(
            self.org,
            card_id="c5",
            by_email=self.admin.email,
            scheduled_at=timezone.now() + timedelta(minutes=1),
        )
        result = dispatch_due_scheduled_cards(now=timezone.now() + timedelta(minutes=2))
        self.assertEqual(result["failed"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")
        self.assertTrue(row.error)


class AppAddTests(TestCase):
    def setUp(self):
        self.org = make_org()
        self.admin = make_user(self.org, role=AIDLUser.Role.ADMIN)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + create_access_token(self.admin))

    def test_add_ai_app(self):
        resp = self.client.post(
            "/api/teams/admin/ai-apps/add/",
            {"name": "Internal RAG Bot", "category": "Assistant", "data_allowed": "internal", "status": "approved"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        app = RegisteredApp.objects.get(name="Internal RAG Bot")
        self.assertEqual(app.app_type, RegisteredApp.AppType.AI)
        self.assertEqual(app.category, "Assistant")
        self.assertEqual(app.data_allowed, "internal")
        self.assertEqual(app.status, "approved")

    def test_add_it_app(self):
        resp = self.client.post(
            "/api/teams/admin/it-apps/add/",
            {"name": "New SSO Tool"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        app = RegisteredApp.objects.get(name="New SSO Tool")
        self.assertEqual(app.app_type, RegisteredApp.AppType.IT)
        self.assertEqual(app.status, RegisteredApp.Status.PENDING)

    def test_add_app_requires_name(self):
        resp = self.client.post("/api/teams/admin/ai-apps/add/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "name_required")

    def test_invalid_data_allowed_ignored_not_rejected(self):
        resp = self.client.post(
            "/api/teams/admin/ai-apps/add/",
            {"name": "Weird App", "data_allowed": "not-a-real-choice"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        app = RegisteredApp.objects.get(name="Weird App")
        self.assertEqual(app.data_allowed, "")


class InviteEmailLinkTests(TestCase):
    """Regression test: the invite email must carry a real <a href> link, not
    plain text, or Outlook Safe Links mangles the click (see teams_invites.py
    / microsoft_auth.py send_mail_via_graph)."""

    def setUp(self):
        self.org = make_org()
        self.admin = make_user(self.org, role=AIDLUser.Role.ADMIN)

    @patch("api.teams_invites.send_mail_via_graph")
    def test_invite_email_sends_html_with_anchor_link(self, mock_send):
        mock_send.return_value = {"ok": True}
        from .models import Invitation

        invitation = Invitation.objects.create(
            token=uuid.uuid4().hex,
            email="new.hire@northwind.example",
            full_name="New Hire",
            organization_id=str(self.org.pk),
            organization_name=self.org.name,
            invited_by_email=self.admin.email,
            expires_at=timezone.now() + timedelta(days=14),
        )
        _send_invite_email(invitation, access_token="fake-token")

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        self.assertNotIn("body_text", kwargs)
        self.assertIn("body_html", kwargs)
        self.assertIn("<a href=", kwargs["body_html"])
        self.assertIn("/api/auth/teams/login-redirect/", kwargs["body_html"])


class BotAdaptiveCardActionsTests(TestCase):
    """The in-Teams native path: Action.Execute invoke payloads hitting the
    bot messaging endpoint directly (no Website Tab, no JWT) — same shape
    Teams sends when a card's button is clicked, per the Adaptive Cards
    Universal Action Model (button's static `data` + every Input's current
    value, merged into one flat object)."""

    def setUp(self):
        self.org = make_org()
        self.admin = make_user(self.org, role=AIDLUser.Role.ADMIN)
        self.client = APIClient()

    def _invoke(self, value: dict, *, as_user=None):
        actor = as_user or self.admin
        activity = {
            "type": "invoke",
            "name": "adaptiveCard/action",
            "from": {"aadObjectId": actor.microsoft_id, "name": actor.full_name},
            "value": value,
        }
        return self.client.post("/api/teams/bot/messages/", activity, format="json")

    def test_nav_action_returns_card_no_write(self):
        resp = self._invoke({"action": "nav", "tab": "cards"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["type"], "application/vnd.microsoft.card.adaptive")
        self.assertEqual(resp.data["value"]["type"], "AdaptiveCard")

    def test_promote_admin_via_bot_sets_permissions(self):
        target = make_user(self.org, role=AIDLUser.Role.LEARNER)
        resp = self._invoke(
            {
                "action": "promote_admin",
                "tab": "add-admin",
                "promoteEmail": target.email,
                "permApproveApps": "false",
                "permAccessCards": "true",
                "permCreateCard": "false",
            }
        )
        self.assertEqual(resp.status_code, 200)
        target.refresh_from_db()
        self.assertEqual(target.role, AIDLUser.Role.ADMIN)
        self.assertFalse(target.perm_approve_apps)
        self.assertTrue(target.perm_access_cards)
        self.assertFalse(target.perm_create_card)
        # No error banner injected into the refreshed card.
        body_texts = [b.get("items", [{}])[0].get("text", "") for b in resp.data["value"]["body"] if b.get("type") == "Container"]
        self.assertFalse(any(t.startswith("⚠") for t in body_texts))

    def test_promote_admin_via_bot_unknown_target_surfaces_error(self):
        resp = self._invoke({"action": "promote_admin", "tab": "add-admin", "promoteEmail": "ghost@nowhere.example"})
        self.assertEqual(resp.status_code, 200)
        first_block = resp.data["value"]["body"][0]
        self.assertEqual(first_block["style"], "attention")
        self.assertIn("⚠", first_block["items"][0]["text"])

    def test_request_card_via_bot(self):
        resp = self._invoke({"action": "request_card", "tab": "cards", "card_id": "c3"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            CardRequest.objects.filter(organization_id=str(self.org.pk), card_id="c3", status="requested").exists()
        )

    def test_request_card_via_bot_unknown_card_surfaces_error(self):
        resp = self._invoke({"action": "request_card", "tab": "cards", "card_id": "nope"})
        self.assertEqual(resp.status_code, 200)
        first_block = resp.data["value"]["body"][0]
        self.assertEqual(first_block["style"], "attention")

    def test_request_card_via_bot_requires_permission(self):
        self.admin.perm_access_cards = False
        self.admin.save(update_fields=["perm_access_cards"])
        resp = self._invoke({"action": "request_card", "tab": "cards", "card_id": "c3"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(CardRequest.objects.filter(organization_id=str(self.org.pk), card_id="c3").exists())
        first_block = resp.data["value"]["body"][0]
        self.assertIn("permission", first_block["items"][0]["text"].lower())

    @patch("api.cards_service.get_access_token_for_user", return_value="fake-token")
    @patch("api.teams_messaging.send_channel_adaptive_card", return_value={"ok": True, "message_id": "m1"})
    def test_send_card_now_via_bot(self, mock_send, mock_token):
        self.admin.teams_team_id = "team-1"
        self.org.teams_welcome_channel_id = "chan-1"
        self.admin.save(update_fields=["teams_team_id"])
        self.org.save(update_fields=["teams_welcome_channel_id"])

        resp = self._invoke({"action": "send_card_now", "tab": "cards", "card_id": "c4"})
        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once()
        self.assertTrue(
            CardRequest.objects.filter(organization_id=str(self.org.pk), card_id="c4", status="sent").exists()
        )

    def test_schedule_card_via_bot(self):
        future = timezone.now() + timedelta(days=1)
        resp = self._invoke(
            {
                "action": "schedule_card",
                "tab": "cards",
                "scheduleCardId": "c5",
                "scheduleDate": future.strftime("%Y-%m-%d"),
                "scheduleTime": "14:30",
            }
        )
        self.assertEqual(resp.status_code, 200)
        row = CardRequest.objects.get(organization_id=str(self.org.pk), card_id="c5")
        self.assertEqual(row.status, "scheduled")
        self.assertIsNotNone(row.scheduled_at)

    def test_request_new_card_via_bot(self):
        resp = self._invoke(
            {
                "action": "request_new_card",
                "tab": "cards",
                "newCardTitle": "Shadow AI Checklist",
                "newCardDesc": "For unsanctioned tool usage",
            }
        )
        self.assertEqual(resp.status_code, 200)
        from .models import CardCustomRequest

        self.assertTrue(CardCustomRequest.objects.filter(organization_id=str(self.org.pk), title="Shadow AI Checklist").exists())

    def test_add_app_via_bot(self):
        resp = self._invoke(
            {
                "action": "add_app",
                "tab": "ai-apps",
                "app_type": "ai",
                "appName": "Bot-added App",
                "appCategory": "Assistant",
                "appDataAllowed": "internal",
                "appStatus": "approved",
            }
        )
        self.assertEqual(resp.status_code, 200)
        app = RegisteredApp.objects.get(name="Bot-added App")
        self.assertEqual(app.app_type, RegisteredApp.AppType.AI)
        self.assertEqual(app.data_allowed, "internal")

    @patch("api.teams_invites.send_mail_via_graph", return_value={"ok": True})
    @patch("api.teams_invites.add_team_member", return_value={"ok": True})
    @patch("api.teams_invites.resolve_user_by_email", return_value={"id": "aad-123"})
    @patch("api.teams_invites.get_access_token_for_user", return_value="fake-token")
    def test_invite_user_via_bot(self, mock_token, mock_resolve, mock_add, mock_mail):
        resp = self._invoke(
            {"action": "invite_user", "tab": "add-user", "inviteName": "New Person", "inviteEmail": "new.person@northwind.example"}
        )
        self.assertEqual(resp.status_code, 200)
        from .models import Invitation

        self.assertTrue(Invitation.objects.filter(email="new.person@northwind.example").exists())

    def test_unresolvable_actor_cannot_write(self):
        activity = {
            "type": "invoke",
            "name": "adaptiveCard/action",
            "from": {"aadObjectId": "not-a-real-aad-id", "name": "Stranger"},
            "value": {"action": "add_app", "tab": "ai-apps", "app_type": "ai", "appName": "Should Not Save"},
        }
        resp = self.client.post("/api/teams/bot/messages/", activity, format="json")
        self.assertEqual(resp.status_code, 200)
        first_block = resp.data["value"]["body"][0]
        self.assertEqual(first_block["style"], "attention")
        self.assertFalse(RegisteredApp.objects.filter(name="Should Not Save").exists())


@override_settings(AUTH_EMAIL_OTP=False, AUTH_CAPTCHA=False)  # direct path; OTP/captcha tested below
class WebsiteSignupTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.payload = {
            "enroll_as": "individual",
            "first_name": "Alex",
            "last_name": "Morgan",
            "email": f"alex.{uuid.uuid4().hex[:8]}@company.com",
            "password": "CreateA-StrongPass9",
            "confirm_password": "CreateA-StrongPass9",
            "mobile_number": "+15550000000",
            "country": "United States",
            "state": "California",
            "city": "San Francisco",
            "license_class": "class_l",
        }

    def test_individual_signup_returns_tokens(self):
        resp = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIn("access_token", resp.data)
        self.assertIn("refresh_token", resp.data)
        self.assertEqual(resp.data["token_type"], "Bearer")
        self.assertNotIn("password", resp.data)
        self.assertNotIn("password_hash", resp.data.get("user") or {})
        user = AIDLUser.objects.get(email=self.payload["email"])
        self.assertEqual(user.provider, "website")
        self.assertEqual(user.role, AIDLUser.Role.LEARNER)
        self.assertEqual(user.license_class, AIDLUser.LicenseClass.CLASS_L)
        self.assertTrue(user.check_password(self.payload["password"]))
        self.assertTrue(user.microsoft_id.startswith("local:"))

    def test_organization_signup_creates_org_and_admin(self):
        self.payload["enroll_as"] = "organization"
        self.payload["organization_name"] = f"Northwind {uuid.uuid4().hex[:6]}"
        resp = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        user = AIDLUser.objects.get(email=self.payload["email"])
        self.assertEqual(user.role, AIDLUser.Role.ADMIN)
        self.assertTrue(user.organization_id)
        self.assertEqual(user.organization_name, self.payload["organization_name"])
        self.assertTrue(Organization.objects.filter(pk=user.organization_id).exists())

    def test_duplicate_email_rejected(self):
        first = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        again = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertIn("email", again.data)

    def test_password_complexity_required(self):
        self.payload["password"] = "onlyletters"
        self.payload["confirm_password"] = "onlyletters"
        resp = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(resp.status_code, 400)
        messages = " ".join(resp.data.get("password") or [])
        self.assertIn("uppercase", messages.lower())
        self.assertIn("number", messages.lower())
        self.assertIn("special", messages.lower())

    def test_password_mismatch_rejected(self):
        self.payload["confirm_password"] = "Different-Pass99"
        resp = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("confirm_password", resp.data)

    def test_missing_required_fields_rejected(self):
        resp = self.client.post("/api/auth/signup/", {"email": "only@x.com"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("first_name", resp.data)
        self.assertIn("country", resp.data)   # password is optional (emailed code instead)

    def test_organization_requires_name(self):
        self.payload["enroll_as"] = "organization"
        self.payload["organization_name"] = ""
        resp = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("organization_name", resp.data)

    def test_login_and_me_after_signup(self):
        created = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        login = self.client.post(
            "/api/auth/signin/",
            {
                "enroll_as": "individual",
                "email": self.payload["email"],
                "password": self.payload["password"],
            },
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.content)
        self.assertEqual(login.data["message"], "Signed in successfully.")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + login.data["access_token"])
        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["email"], self.payload["email"])
        self.assertEqual(me.data["first_name"], "Alex")

    def test_wrong_password_login_rejected(self):
        created = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        resp = self.client.post(
            "/api/auth/signin/",
            {
                "enroll_as": "individual",
                "email": self.payload["email"],
                "password": "Wrong-Password99",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_signin_wrong_enroll_as_rejected(self):
        created = self.client.post("/api/auth/signup/", self.payload, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        resp = self.client.post(
            "/api/auth/signin/",
            {
                "enroll_as": "organization",
                "email": self.payload["email"],
                "password": self.payload["password"],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("enroll_as", resp.data)


class LocationApiTests(SimpleTestCase):
    """Signup-form dropdowns — served from the bundled dataset, no DB."""

    def setUp(self):
        self.client = APIClient()

    def test_countries_list(self):
        resp = self.client.get("/api/locations/countries/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertGreater(resp.data["count"], 200)
        us = next(c for c in resp.data["results"] if c["code"] == "US")
        self.assertEqual(us["name"], "United States")
        self.assertEqual(us["phone_code"], "+1")

    def test_states_by_code_or_name(self):
        by_code = self.client.get("/api/locations/states/", {"country": "us"})
        by_name = self.client.get("/api/locations/states/", {"country": "United States"})
        self.assertEqual(by_code.status_code, 200, by_code.content)
        self.assertEqual(by_code.data["results"], by_name.data["results"])
        self.assertIn({"code": "CA", "name": "California"}, by_code.data["results"])

    def test_cities_for_state(self):
        resp = self.client.get("/api/locations/cities/", {"country": "US", "state": "California"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["state"]["code"], "CA")
        self.assertIn({"name": "San Francisco"}, resp.data["results"])

    def test_missing_params_rejected(self):
        self.assertEqual(self.client.get("/api/locations/states/").status_code, 400)
        resp = self.client.get("/api/locations/cities/", {"country": "US"})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_country_or_state_404(self):
        resp = self.client.get("/api/locations/states/", {"country": "Atlantis"})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/locations/cities/", {"country": "US", "state": "Nowhere"})
        self.assertEqual(resp.status_code, 404)


class ApiDocsTests(SimpleTestCase):
    """Swagger UI / ReDoc / OpenAPI schema are served and cover the key APIs."""

    def test_docs_pages_render(self):
        for url in ("/api/docs/", "/api/redoc/"):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)

    def test_schema_documents_frontend_apis(self):
        resp = self.client.get("/api/schema/?format=json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        paths = schema["paths"]
        for path in (
            "/api/auth/signup/",
            "/api/auth/signin/",
            "/api/auth/me/",
            "/api/locations/countries/",
            "/api/locations/states/",
            "/api/locations/cities/",
        ):
            self.assertIn(path, paths)
        self.assertEqual(paths["/api/auth/me/"]["get"]["security"], [{"BearerAuth": []}])
        self.assertIn("SignupRequest", schema["components"]["schemas"])


class SlackCardsTests(TestCase):
    """Slack Admin cards + User cards pages (Slack guide sections 8 and 10)."""

    def setUp(self):
        self.org = make_org(name="Secureitlab")
        self.primary = make_user(self.org, full_name="Priya Raman")
        self.learner = make_user(self.org, role=AIDLUser.Role.LEARNER, full_name="Jordan Ellis")
        self.client = APIClient()

    def _get(self, path, user=None):
        token = f"?token={create_access_token(user)}" if user else ""
        return self.client.get(f"/api/slack/cards/{path}/{token}")

    def test_demo_preview_without_token(self):
        resp = self._get("admin")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Northwind Logistics")
        self.assertNotContains(resp, 'data-tab="policy"')

    def test_bad_token_is_rejected(self):
        resp = self.client.get("/api/slack/cards/admin/?token=nope")
        self.assertEqual(resp.status_code, 401)

    def test_learner_cannot_open_admin_cards(self):
        learner = make_user(self.org, role=AIDLUser.Role.LEARNER)
        self.assertEqual(self._get("admin", learner).status_code, 403)

    def test_primary_admin_sees_every_tab_with_real_data(self):
        resp = self._get("admin", self.primary)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "for Secureitlab")
        self.assertContains(resp, "Welcome to your Admin Center, Priya")
        for tab in ("home", "add-admin", "cards", "ai-apps", "it-apps"):
            self.assertContains(resp, f'data-tab="{tab}"')
        self.assertNotContains(resp, 'data-tab="add-user"')   # users join the channel instead

    def test_invited_admin_sees_only_permitted_tabs(self):
        invited = make_user(
            self.org, perm_approve_apps=False, perm_access_cards=True, perm_create_card=False
        )
        resp = self._get("admin", invited)
        self.assertContains(resp, 'data-tab="home"')
        self.assertContains(resp, 'data-tab="cards"')
        for tab in ("add-admin", "ai-apps", "it-apps"):
            self.assertNotContains(resp, f'data-tab="{tab}"')

    def test_user_cards_welcome_and_selected_lights(self):
        learner = make_user(self.org, role=AIDLUser.Role.LEARNER, full_name="Jordan Ellis")
        resp = self.client.get(
            f"/api/slack/cards/user/?token={create_access_token(learner)}&lights=green,amber"
        )
        self.assertContains(resp, "Welcome to AIDL, Jordan!")
        self.assertNotContains(resp, "Acceptable Use")
        self.assertContains(resp, "tl-section green")
        self.assertNotContains(resp, "tl-section red")

    def test_coverage_page_lists_members_with_download(self):
        self.assertEqual(self.client.get("/api/slack/cards/admin/coverage/").status_code, 401)
        resp = self.client.get(f"/api/slack/cards/admin/coverage/?token={create_access_token(self.primary)}")
        self.assertContains(resp, "Coverage report")
        self.assertContains(resp, self.learner.email)          # team member listed
        self.assertNotContains(resp, self.primary.email)       # admins aren't part of the team
        self.assertContains(resp, "coverage.csv?token=")

    def test_private_links_use_https_behind_a_proxy(self):
        from django.test import RequestFactory

        from .slack_interactions import _private_link

        req = RequestFactory().post("/", HTTP_HOST="testserver", HTTP_X_FORWARDED_PROTO="https")
        self.assertTrue(_private_link(req, self.primary, "home").startswith("https://testserver/api/slack/cards/admin/coverage/?token="))

    def test_coverage_csv_requires_admin(self):
        self.assertEqual(self.client.get("/api/slack/cards/admin/coverage.csv").status_code, 401)
        resp = self.client.get(
            f"/api/slack/cards/admin/coverage.csv?token={create_access_token(self.primary)}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.learner.email, resp.content.decode())
        self.assertNotIn(self.primary.email, resp.content.decode())


class SlackOrganizationBootstrapTests(TestCase):
    def test_workspace_creates_then_reuses_organization(self):
        from .auth_views import _ensure_slack_organization

        profile = {"team_id": "T123", "team_name": "Acme Slack"}
        first = AIDLUser.objects.create(
            microsoft_id="slack:T123:U1", email="a@acme.example", full_name="A",
            enroll_as=AIDLUser.EnrollAs.ORGANIZATION, provider="slack",
        )
        _ensure_slack_organization(first, profile)
        first.refresh_from_db()
        org = Organization.objects.get(slack_team_id="T123")
        self.assertEqual(first.organization_id, str(org.pk))
        self.assertEqual(first.role, AIDLUser.Role.ADMIN)

        second = AIDLUser.objects.create(
            microsoft_id="slack:T123:U2", email="b@acme.example", full_name="B",
            enroll_as=AIDLUser.EnrollAs.ORGANIZATION, provider="slack",
        )
        _ensure_slack_organization(second, {**profile, "team_name": "Renamed"})
        second.refresh_from_db()
        self.assertEqual(second.organization_id, str(org.pk))
        self.assertEqual(Organization.objects.filter(slack_team_id="T123").count(), 1)


class SlackWorkspaceSetupTests(TestCase):
    """Organization login → "Add to Slack" → #aidl + Admin Center card
    (Slack guide sections 6 and 7). Slack's Web API is mocked."""

    INSTALL = {
        "ok": True, "access_token": "xoxb-test", "bot_user_id": "UBOT",
        "team": {"id": "T9", "name": "Acme"}, "authed_user": {"id": "U1"},
    }

    def _fake_api(self, calls):
        def fake(method, token, *, json=None, params=None):
            calls.append((method, json or params))
            if method == "users.info":
                return {"ok": True, "user": {"profile": {"email": "owner@acme.example", "real_name": "Ana Owner", "first_name": "Ana"}}}
            if method == "conversations.create":
                return {"ok": True, "channel": {"id": "C42"}}
            if method == "conversations.info":
                return {"ok": True, "channel": {"id": "C42", "is_archived": False}}
            if method == "chat.postMessage":
                return {"ok": True, "ts": "111.222"}
            return {"ok": True}
        return fake

    def _login(self):
        from .microsoft_auth import create_oauth_state

        state = create_oauth_state("organization")
        return self.client.get(f"/api/auth/slack/callback/?code=abc&state={state}")

    def test_org_login_asks_for_bot_install(self):
        with self.settings(SLACK_CLIENT_ID="cid", SLACK_CLIENT_SECRET="sec"):
            resp = self.client.get("/api/auth/slack/login/?enroll_as=organization")
        self.assertTrue(resp.json()["auth_url"].startswith("https://slack.com/oauth/v2/authorize"))
        self.assertIn("channels%3Amanage", resp.json()["auth_url"])

    def test_org_login_creates_channel_and_posts_admin_card(self):
        calls = []
        with self.settings(SLACK_CLIENT_ID="cid", SLACK_CLIENT_SECRET="sec"), \
                patch("api.slack_client.exchange_install_code", return_value=self.INSTALL), \
                patch("api.slack_client.slack_api", side_effect=self._fake_api(calls)):
            resp = self._login()

        self.assertEqual(resp.status_code, 302)
        self.assertIn("slack_url=https%3A%2F%2Fapp.slack.com%2Fclient%2FT9%2FC42", resp["Location"])
        self.assertIn("landed_on=channel", resp["Location"])
        methods = [m for m, _ in calls]
        self.assertIn("conversations.create", methods)
        created = next(body for m, body in calls if m == "conversations.create")
        self.assertTrue(created["is_private"])                     # #aidl is a private channel
        self.assertIn(("conversations.invite", {"channel": "C42", "users": "U1"}), calls)

        post = next(body for m, body in calls if m == "chat.postMessage")
        text = json.dumps(post["blocks"], ensure_ascii=False)
        self.assertIn("🚦 AIDL Admin Center", text)
        self.assertIn("aidl_policy_open", text)                    # step 1: Create AUP
        self.assertFalse(any(b.get("block_id") == "aidl_tabs" for b in post["blocks"]))

        org = Organization.objects.get(slack_team_id="T9")
        self.assertEqual(org.slack_channel_id, "C42")
        self.assertNotIn("xoxb-test", org.slack_bot_token)  # stored encrypted
        from .slack_client import bot_token
        self.assertEqual(bot_token(org), "xoxb-test")

    def test_taken_private_name_falls_back_to_next_name(self):
        calls = []
        base = self._fake_api(calls)

        def fake(method, token, *, json=None, params=None):
            if method == "conversations.create" and json["name"] == "aidl":
                calls.append((method, json))
                return {"ok": False, "error": "name_taken"}
            if method == "conversations.list":
                return {"ok": True, "channels": []}  # the taken #aidl is private
            return base(method, token, json=json, params=params)

        with self.settings(SLACK_CLIENT_ID="cid", SLACK_CLIENT_SECRET="sec", SLACK_CHANNEL_NAME="aidl"),                 patch("api.slack_client.exchange_install_code", return_value=self.INSTALL),                 patch("api.slack_client.slack_api", side_effect=fake):
            resp = self._login()
        self.assertIn("landed_on=channel", resp["Location"])
        names = [body["name"] for m, body in calls if m == "conversations.create"]
        self.assertEqual(names, ["aidl", "aidl-app"])

    def test_second_login_reuses_channel_and_updates_card(self):
        calls = []
        with self.settings(SLACK_CLIENT_ID="cid", SLACK_CLIENT_SECRET="sec"), \
                patch("api.slack_client.exchange_install_code", return_value=self.INSTALL), \
                patch("api.slack_client.slack_api", side_effect=self._fake_api(calls)):
            self._login()
            calls.clear()
            self._login()
        methods = [m for m, _ in calls]
        self.assertNotIn("conversations.create", methods)
        self.assertIn("chat.update", methods)
        self.assertEqual(Organization.objects.filter(slack_team_id="T9").count(), 1)


class SlackInteractionsTests(TestCase):
    SECRET = "shh"

    def setUp(self):
        self.org = make_org(slack_team_id="T9")
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Ana Owner")

    def _post(self, action, user_id="U1", secret=None):
        import hashlib, hmac, time
        from urllib.parse import urlencode

        payload = {
            "type": "block_actions", "team": {"id": "T9"}, "user": {"id": user_id},
            "response_url": "https://hooks.slack.test/r", "container": {"is_ephemeral": False},
            "actions": [action],
        }
        body = urlencode({"payload": json.dumps(payload)})
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new((secret or self.SECRET).encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
        with self.settings(SLACK_SIGNING_SECRET=self.SECRET, SLACK_REPLY_SYNC=True), patch("api.slack_interactions.requests.post") as post:
            resp = self.client.post(
                "/api/slack/interactions/", body, content_type="application/x-www-form-urlencoded",
                HTTP_X_SLACK_REQUEST_TIMESTAMP=ts, HTTP_X_SLACK_SIGNATURE=sig,
            )
        return resp, post

    def test_bad_signature_rejected(self):
        resp, post = self._post({"action_id": "aidl_tab_cards", "value": "cards"}, secret="wrong")
        self.assertEqual(resp.status_code, 401)
        post.assert_not_called()

    def test_tab_click_replies_privately_with_that_card(self):
        resp, post = self._post({"action_id": "aidl_tab_cards", "value": "cards"})
        self.assertEqual(resp.status_code, 200)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_type"], "ephemeral")
        text = json.dumps(body["blocks"])
        self.assertIn("Send Cards to your team", text)
        self.assertIn("aidl_open_cards", text)  # opens a Slack modal, not a web page

    def test_tab_outside_permissions_is_refused(self):
        make_user(self.org, microsoft_id="slack:T9:U2", perm_approve_apps=False, perm_access_cards=True, perm_create_card=False)
        _resp, post = self._post({"action_id": "aidl_tab_ai-apps", "value": "ai-apps"}, user_id="U2")
        self.assertIn("permissions don't include", post.call_args.kwargs["json"]["text"])

    def test_non_admin_is_refused(self):
        make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U3")
        _resp, post = self._post({"action_id": "aidl_tab_home", "value": "home"}, user_id="U3")
        self.assertIn("only available to AIDL admins", post.call_args.kwargs["json"]["text"])


ALL_ANSWERS = {
    "ai_policy": "No, not yet",
    "approved_tools": "Only company-approved tools",
    "confidential_data": "Never",
    "human_review": "Always",
    "disclosure": "Yes, always",
    "regulation": "DPDP Act (India)",
    "incident_reporting": "Through HR",
    "training_frequency": "Every quarter",
}


class PolicyAnswersTests(TestCase):
    """Slack guide 4.7 (saving answers) and 7.3 (answers → card data)."""

    def setUp(self):
        self.org = make_org()
        self.admin = make_user(self.org)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + create_access_token(self.admin))

    def test_save_and_read_answers(self):
        resp = self.client.post("/api/org/policy-answers/", {"answers": ALL_ANSWERS}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["policy_completed"])
        self.assertEqual(self.client.get("/api/org/policy-answers/").data["answers"], ALL_ANSWERS)
        me = self.client.get("/api/auth/me/")
        self.assertTrue(me.data["policy_completed"])

    def test_incomplete_answers_rejected(self):
        partial = dict(ALL_ANSWERS, regulation="Mars law")
        resp = self.client.post("/api/org/policy-answers/", {"answers": partial}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("regulation", resp.data)

    def test_learner_cannot_change_answers(self):
        learner = make_user(self.org, role=AIDLUser.Role.LEARNER)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + create_access_token(learner))
        self.assertEqual(client.post("/api/org/policy-answers/", {"answers": ALL_ANSWERS}, format="json").status_code, 403)

    def test_answers_travel_through_slack_login_state(self):
        with self.settings(SLACK_CLIENT_ID="cid", SLACK_CLIENT_SECRET="sec"):
            resp = self.client.get("/api/auth/slack/login/", {"enroll_as": "organization", "policy": json.dumps(ALL_ANSWERS)})
        self.assertEqual(resp.status_code, 200)
        from .models import OAuthState
        self.assertEqual(json.loads(OAuthState.objects.get(state=resp.data["state"]).payload)["policy_answers"], ALL_ANSWERS)

    def test_answers_change_the_cards(self):
        from .org_policy import save_answers
        from .slack_cards import build_admin_cards_context, build_user_cards_context

        save_answers(self.org, ALL_ANSWERS)
        d = build_admin_cards_context(self.admin)
        cards = {c["id"]: c for c in d["cards"]}
        self.assertEqual(d["aup_display"], "—")                               # no written policy
        self.assertEqual(cards["c0"]["state"], "unavailable")
        self.assertEqual([c["id"] for c in d["cards"][:2]], ["c7", "c8"])      # recommended first
        self.assertIn("the DPDP Act", cards["c5"]["desc"])
        self.assertIn("HR", cards["c9"]["desc"])
        self.assertEqual(d["ai_apps"][0]["status"], "Prohibited")              # consumer chatbots first
        self.assertIn("consumer", d["ai_apps"][0]["name"].lower())

        learner = make_user(self.org, role=AIDLUser.Role.LEARNER)
        u = build_user_cards_context(learner)
        driver = next(r for r in u["highway_code"] if r["shape"] == "yield")
        self.assertTrue(driver["text"].endswith("Say when AI helped."))
        red = next(l for l in u["lights"] if l["id"] == "red")
        self.assertIn("All confidential and customer data", red["items"])
        self.assertEqual(u["training_note"], "Refresher reminder every 3 months")

    def test_other_answers(self):
        from .org_policy import save_answers
        from .slack_cards import build_admin_cards_context

        save_answers(self.org, dict(ALL_ANSWERS, ai_policy="Yes, but still a draft",
                                    approved_tools="Any public AI tool",
                                    confidential_data="Only in approved enterprise tools",
                                    human_review="No, it is optional"))
        d = build_admin_cards_context(self.admin)
        cards = {c["id"]: c for c in d["cards"]}
        self.assertIn("Draft", cards["c0"]["kicker"])
        self.assertNotIn("unreviewed", cards["c3"]["desc"])
        self.assertEqual([a["name"] for a in d["ai_apps"][:3]], ["ChatGPT", "Claude", "Gemini"])
        self.assertTrue(all(a["data_allowed"] in ("Public only", "Internal", "Internal + Confidential")
                            for a in d["ai_apps"] if a["status"] == "Approved"))


class SlackMultiWorkspaceTests(TestCase):
    """Any company can install AIDL — each Slack workspace is its own org."""

    def _user(self, uid, team):
        return AIDLUser.objects.create(
            microsoft_id=f"slack:{team}:{uid}", email=f"{uid}@{team}.example", full_name=uid,
            enroll_as=AIDLUser.EnrollAs.ORGANIZATION, provider="slack",
        )

    def test_same_workspace_name_gets_separate_orgs(self):
        from .auth_views import _ensure_slack_organization

        a = _ensure_slack_organization(self._user("U1", "TA"), {"team_id": "TA", "team_name": "Acme"})
        b = _ensure_slack_organization(self._user("U2", "TB"), {"team_id": "TB", "team_name": "Acme"})
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual((a.slack_team_id, b.slack_team_id), ("TA", "TB"))

    def test_second_workspace_never_takes_over_the_first(self):
        from .auth_views import _ensure_slack_organization

        user = self._user("U1", "TA")
        first = _ensure_slack_organization(user, {"team_id": "TA", "team_name": "Acme"})
        second = _ensure_slack_organization(user, {"team_id": "TB", "team_name": "Acme Labs"})
        first.refresh_from_db()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.slack_team_id, "TA")

    def test_existing_website_org_gets_its_workspace_linked(self):
        from .auth_views import _ensure_slack_organization

        org = make_org(name="Globex")
        user = make_user(org, microsoft_id="slack:TG:U9")
        linked = _ensure_slack_organization(user, {"team_id": "TG", "team_name": "Globex HQ"})
        self.assertEqual(linked.pk, org.pk)
        self.assertEqual(Organization.objects.get(pk=org.pk).slack_team_id, "TG")


class SlackModalTests(TestCase):
    """Admin card forms as Slack modals (guide 8.2–8.6, 9). Slack API mocked."""

    SECRET = "shh"

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_token=encrypt_token("xoxb-t"))
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []

    def _fake(self, method, token, *, json=None, params=None):
        self.calls.append((method, json or params))
        if method in ("users.lookupByEmail", "users.info"):
            email = (params or {}).get("email", "rahul@acme.example")
            if email == "stranger@nowhere.example":
                return {"ok": False, "error": "users_not_found"}
            return {"ok": True, "user": {"id": "U7", "profile": {"email": email, "real_name": "Rahul K"}}}
        return {"ok": True, "ts": "1.2"}

    def _post(self, payload):
        import hashlib
        import hmac
        import time
        from urllib.parse import urlencode

        payload = {"team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"}, "trigger_id": "trig", **payload}
        body = urlencode({"payload": json.dumps(payload)})
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(self.SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
        with self.settings(SLACK_SIGNING_SECRET=self.SECRET, SLACK_REPLY_SYNC=True), \
                patch("api.slack_client.slack_api", side_effect=self._fake), \
                patch("requests.post") as http:
            http.return_value.status_code = 200
            self.http = http  # every outgoing HTTP POST (response_url replies, file upload)
            return self.client.post("/api/slack/interactions/", body, content_type="application/x-www-form-urlencoded",
                                    HTTP_X_SLACK_REQUEST_TIMESTAMP=ts, HTTP_X_SLACK_SIGNATURE=sig)

    def _submit(self, callback, values, meta=None):
        view = {"callback_id": callback, "private_metadata": json.dumps(meta or {"channel_id": "C42"}),
                "blocks": [{"type": "input", "block_id": next(iter(values), "name")}],
                "state": {"values": {k: {"v": v} for k, v in values.items()}}}
        return self._post({"type": "view_submission", "view": view})

    def test_send_admin_invite_opens_a_modal(self):
        self._post({"type": "block_actions", "channel": {"id": "C42"},
                    "actions": [{"action_id": "aidl_open_add-admin", "value": "add-admin"}]})
        opened = [body for m, body in self.calls if m == "views.open"]
        self.assertEqual(opened[0]["trigger_id"], "trig")              # Loading… popup at once
        self.assertEqual(_shown_views(self.calls)[0]["callback_id"], "aidl_add_admin")

    def test_add_admin_saves_permissions_and_onboards(self):
        resp = self._submit("aidl_add_admin", {
            "email": {"value": "rahul@acme.example"},
            "perms": {"selected_options": [{"value": "access_cards"}]},
        })
        self.assertEqual(resp.json(), {"response_action": "clear"})
        rahul = AIDLUser.objects.get(microsoft_id="slack:T9:U7")
        self.assertEqual(rahul.role, AIDLUser.Role.ADMIN)
        self.assertEqual((rahul.perm_approve_apps, rahul.perm_access_cards, rahul.perm_create_card), (False, True, False))
        methods = [m for m, _ in self.calls]
        self.assertIn("conversations.invite", methods)
        self.assertIn("chat.postEphemeral", methods)
        dm = next(b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "U7")
        self.assertIn("added you as an AIDL admin", dm["text"])

    def test_add_admin_conditions(self):
        resp = self._submit("aidl_add_admin", {"email": {"value": ""}})
        self.assertIn("email", resp.json()["errors"])
        resp = self._submit("aidl_add_admin", {"email": {"value": "stranger@nowhere.example"}})
        self.assertIn("isn't in your Slack workspace", resp.json()["errors"]["email"])
        self.org.admin_seat_limit = 1
        self.org.save()
        resp = self._submit("aidl_add_admin", {"email": {"value": "rahul@acme.example"}})
        self.assertIn("no seats left", resp.json()["errors"]["email"])

    def test_card_request_then_send_with_lights(self):
        resp = self._submit("aidl_card_request", {}, meta={"channel_id": "C42", "card_id": "c1"})
        self.assertEqual(resp.json()["view"]["callback_id"], "aidl_card_send")
        meta = {"channel_id": "C42", "card_id": "c1"}
        resp = self._submit("aidl_card_send", {"lights": {"selected_options": []}, "when": {"selected_option": {"value": "now"}}}, meta=meta)
        self.assertIn("Select at least one light", resp.json()["errors"]["lights"])
        resp = self._submit("aidl_card_send", {"lights": {"selected_options": [{"value": "green"}, {"value": "red"}]},
                                               "when": {"selected_option": {"value": "now"}}}, meta=meta)
        self.assertEqual(resp.json(), {"response_action": "clear"})
        post = next(b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "C42")
        text = json.dumps(post["blocks"])
        self.assertIn("GREEN", text)
        self.assertNotIn("AMBER", text)
        self.assertEqual(CardRequest.objects.get(organization_id=str(self.org.pk), card_id="c1").status, CardRequest.Status.SENT)

    def test_schedule_needs_a_time(self):
        meta = {"channel_id": "C42", "card_id": "c8"}
        self._submit("aidl_card_request", {}, meta=meta)
        resp = self._submit("aidl_card_send", {"when": {"selected_option": {"value": "later"}}}, meta=meta)
        self.assertIn("at", resp.json()["errors"])

    def test_add_app_saves_to_registry(self):
        resp = self._submit("aidl_add_app", {
            "name": {"value": "Perplexity"},
            "category": {"selected_option": {"value": "Research & Search"}},
            "data": {"selected_option": {"value": "Internal"}},
            "status": {"selected_option": {"value": "Prohibited"}},
        }, meta={"channel_id": "C42", "kind": "ai"})
        self.assertEqual(resp.json(), {"response_action": "clear"})
        app = RegisteredApp.objects.get(organization_id=str(self.org.pk), name="Perplexity")
        self.assertEqual((app.app_type, app.status, app.data_allowed), ("ai", "rejected", "internal"))

    def test_export_opens_popup_and_dms_the_csv(self):
        def fake(method, token, *, json=None, params=None):
            self.calls.append((method, json or params))
            if method == "conversations.open":
                return {"ok": True, "channel": {"id": "D1"}}
            if method == "files.getUploadURLExternal":
                return {"ok": True, "upload_url": "https://files.slack.test/up", "file_id": "F1"}
            if method == "views.open":
                return {"ok": True, "view": {"id": "V1"}}
            if method == "files.info":
                return {"ok": True, "file": {"url_private_download": "https://files.slack.com/files-pri/T9-F1/download/aidl-coverage.csv"}}
            return {"ok": True}

        self._fake = fake
        learner = make_user(self.org, role=AIDLUser.Role.LEARNER, full_name="Jordan Ellis")
        self._post({"type": "block_actions", "channel": {"id": "C42"}, "response_url": "https://hooks.slack.test/r",
                    "actions": [{"action_id": "aidl_open_home", "value": "home"}]})
        self.assertEqual(next(b for m, b in self.calls if m == "views.open")["view"]["callback_id"], "aidl_loading")
        opened = {"view": _shown_views(self.calls)[0]}
        self.assertEqual(opened["view"]["callback_id"], "aidl_coverage")
        self.assertIn(learner.email, json.dumps(opened["view"]["blocks"]))
        self.assertNotIn(self.admin.email, json.dumps(opened["view"]["blocks"]))
        upload = next(c for c in self.http.call_args_list if c.args and c.args[0] == "https://files.slack.test/up")
        self.assertIn(b"name,email,role", upload.kwargs["data"])
        done = next(b for m, b in self.calls if m == "files.completeUploadExternal")
        self.assertEqual(done["channel_id"], "D1")
        self.assertEqual(done["files"][0]["id"], "F1")
        self.assertIn("/api/slack/cards/admin/coverage.csv?token=", json.dumps(opened["view"]["blocks"]))
        # only the Loading… popup being filled in — the DM copy worked, no note added
        self.assertEqual(len([b for m, b in self.calls if m == "views.update"]), 1)

    def test_export_without_files_scope_explains_the_fix(self):
        def fake(method, token, *, json=None, params=None):
            self.calls.append((method, json or params))
            if method == "conversations.open":
                return {"ok": True, "channel": {"id": "D1"}}
            if method == "files.getUploadURLExternal":
                return {"ok": False, "error": "missing_scope"}
            if method == "views.open":
                return {"ok": True, "view": {"id": "V1"}}
            return {"ok": True}

        self._fake = fake
        import hashlib, hmac, time
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"}, "trigger_id": "t",
                   "channel": {"id": "C42"}, "response_url": "https://hooks.slack.test/r",
                   "actions": [{"action_id": "aidl_open_home", "value": "home"}]}
        body = urlencode({"payload": json.dumps(payload)})
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(self.SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
        with self.settings(SLACK_SIGNING_SECRET=self.SECRET, SLACK_REPLY_SYNC=True), \
                patch("api.slack_client.slack_api", side_effect=fake), \
                patch("api.slack_interactions.requests.post") as reply:
            self.client.post("/api/slack/interactions/", body, content_type="application/x-www-form-urlencoded",
                             HTTP_X_SLACK_REQUEST_TIMESTAMP=ts, HTTP_X_SLACK_SIGNATURE=sig)
        updated = [b for m, b in self.calls if m == "views.update"][-1]
        self.assertIn("files:write", json.dumps(updated["view"]["blocks"]))

    def test_traffic_light_card_has_rating_row(self):
        meta = {"channel_id": "C42", "card_id": "c1"}
        self._submit("aidl_card_request", {}, meta=meta)
        self._submit("aidl_card_send", {"lights": {"selected_options": [{"value": "green"}]},
                                        "when": {"selected_option": {"value": "now"}}}, meta=meta)
        post = next(b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "C42")
        text = json.dumps(post["blocks"])
        self.assertIn("Was this card useful?", text)
        self.assertIn("aidl_rate_like", text)

    def test_full_highway_code_opens_for_any_member(self):
        self._post({"type": "block_actions", "user": {"id": "U99", "team_id": "T9"},  # not an AIDL admin
                    "actions": [{"action_id": "aidl_hc_full", "value": "full"}]})
        opened = next(b for m, b in self.calls if m == "views.open")
        text = json.dumps(opened["view"])
        self.assertIn("Reference only", text)
        self.assertIn("Mind What You Share", text)

    def test_rating_counts_once_per_person(self):
        from .models import TrafficLightRating

        message = {"ts": "111.1", "text": "Traffic Light Check",
                   "blocks": [{"type": "actions", "block_id": "aidl_rate", "elements": []}]}
        vote = {"type": "block_actions", "user": {"id": "U5", "team_id": "T9"}, "channel": {"id": "C42"},
                "message": message, "response_url": "https://hooks.slack.test/r",
                "actions": [{"action_id": "aidl_rate_like", "value": "like"}]}
        self._post(vote)
        rating = TrafficLightRating.objects.get()
        self.assertEqual(rating.likes, 129)
        update = next(b for m, b in self.calls if m == "chat.update")
        self.assertIn("👍 129", json.dumps(update["blocks"], ensure_ascii=False))
        self.assertIn("Thanks for the feedback", self.http.call_args.kwargs["json"]["text"])

        self._post(vote)  # same person again
        self.assertEqual(TrafficLightRating.objects.get().likes, 129)
        self.assertIn("already rated", self.http.call_args.kwargs["json"]["text"])

    def test_user_tab_switches_the_dashboard_message(self):
        from .admin_setup import update

        update(self.org, traffic_light_sent="2026-10-09T10:00:00")
        make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U5", full_name="Jordan Ellis",
                  licence_issued=True, licence_number="AIDL-L-0455-2210")
        self._post({"type": "block_actions", "user": {"id": "U5", "team_id": "T9"},
                    "container": {"channel_id": "D5", "message_ts": "222.2"},
                    "actions": [{"action_id": "aidl_utab_traffic-light", "value": "traffic-light"}]})
        update = next(b for m, b in self.calls if m == "chat.update")
        self.assertEqual((update["channel"], update["ts"]), ("D5", "222.2"))
        text = json.dumps(update["blocks"], ensure_ascii=False)
        self.assertIn("Traffic Light Check", text)
        self.assertIn("RED · STOP", text)            # lights come from the policy, not the admin
        self.assertIn("aidl_utab_home", text)

    def test_invited_admin_without_permission_is_refused(self):
        make_user(self.org, microsoft_id="slack:T9:U2", perm_approve_apps=False, perm_access_cards=False, perm_create_card=False)
        view = {"callback_id": "aidl_add_app", "private_metadata": json.dumps({"kind": "ai"}),
                "blocks": [{"type": "input", "block_id": "name"}], "state": {"values": {"name": {"v": {"value": "X"}}}}}
        resp = self._post({"type": "view_submission", "user": {"id": "U2", "team_id": "T9"}, "view": view})
        self.assertIn("permissions", resp.json()["errors"]["name"])


class SlackDmFallbackTests(TestCase):
    """If the Slack app can't DM (e.g. its Messages tab is off), the user still
    sees their cards privately in the channel and the admin is told why."""

    def test_blocked_dm_falls_back_to_channel_and_warns_admin(self):
        from .slack_client import encrypt_token
        from .slack_onboarding import onboard_member

        org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_token=encrypt_token("xoxb-t"))
        admin = make_user(org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        calls = []

        def fake(method, token, *, json=None, params=None):
            calls.append((method, json))
            if method == "users.info":
                return {"ok": True, "user": {"id": "U7", "profile": {"email": "anshul@acme.example", "real_name": "Anshul Chutani"}}}
            if method == "chat.postMessage":
                return {"ok": False, "error": "messages_tab_disabled"}
            return {"ok": True}

        with patch("api.slack_client.slack_api", side_effect=fake):
            self.assertEqual(onboard_member(org, "U7"), "onboarded")

        shown = [b["text"] for m, b in calls if m == "chat.postEphemeral" and b["user"] == "U7"]
        self.assertEqual(shown, ["Welcome to AIDL, Anshul!"])
        warning = next(b["text"] for m, b in calls if m == "chat.postEphemeral" and b["user"] == "U1")
        self.assertIn("Messages tab", warning)


class SlackHomeVisualTests(TestCase):
    """Admin Home card in the approved layout (aidl_admin_home.json)."""

    def setUp(self):
        self.org = make_org(name="Secureitlab")
        self.admin = make_user(self.org, full_name="Priya Raman")
        make_user(self.org, role=AIDLUser.Role.LEARNER, full_name="Jordan Ellis")  # hasn't signed the AUP

    def _blocks(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        return admin_card_blocks(build_admin_cards_context(self.admin), "home")

    def test_home_layout_matches_design(self):
        """Step-by-step Admin Center: no tab bar; finished steps collapse."""
        _answer_policy(self.org)
        blocks = self._blocks()
        self.assertEqual(blocks[0]["text"]["text"], "🚦 AIDL Admin Center")
        self.assertFalse(any(b.get("block_id") == "aidl_tabs" for b in blocks))
        text = json.dumps(blocks, ensure_ascii=False)
        for label in ("Signed in as *Priya*", "Welcome, Priya.", "1 of 5 setup steps done",
                      "✅  *1 · AI policy (AUP)*", "aidl_aup_view", "aidl_tab_ai-apps", "aidl_tab_it-apps",
                      "*2 · Add admins (optional)*", "aidl_open_add-admin", "aidl_setup_skip",
                      "3 · Add your team*  —  _after step 2_"):
            self.assertIn(label, text)

    def test_send_reminders_dms_unsigned_members(self):
        from .slack_client import encrypt_token
        from .slack_interactions import _send_aup_reminders

        self.org.slack_team_id = "T9"
        self.org.slack_channel_id = "C42"
        self.org.slack_bot_token = encrypt_token("xoxb-t")
        self.org.save()
        make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U7", aup_signed=False)
        make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U8", aup_signed=True)
        calls = []
        with self.settings(SLACK_REPLY_SYNC=True), \
                patch("api.slack_client.slack_api", side_effect=lambda m, t, **k: calls.append((m, k.get("json"))) or {"ok": True}), \
                patch("api.slack_interactions.requests.post") as reply:
            reply.return_value.status_code = 200
            reply.return_value.text = "ok"
            _send_aup_reminders(self.org, self.admin, "https://hooks.slack.test/r")
        dms = [j["channel"] for m, j in calls if m == "chat.postMessage"]
        self.assertEqual(dms, ["U7"])
        self.assertIn("reminder sent to 1", reply.call_args.kwargs["json"]["text"])


class SlackOnboardingTests(TestCase):
    """Joining the AIDL channel onboards the user; accepting the Highway Code
    and the Traffic Light Check issues the Learner licence automatically."""

    SECRET = "shh"

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"), seats_purchased=5)
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []

    def _fake(self, method, token, *, json=None, params=None):
        self.calls.append((method, json or params))
        if method == "users.info":
            uid = (params or {}).get("user")
            if uid == "UB0T2":
                return {"ok": True, "user": {"id": uid, "is_bot": True, "profile": {}}}
            return {"ok": True, "user": {"id": uid, "profile": {"email": f"{uid.lower()}@acme.example", "real_name": "Jordan Ellis"}}}
        return {"ok": True, "ts": "1.1"}

    def _signed(self, path, body, content_type, extra=None):
        import hashlib
        import hmac
        import time

        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(self.SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
        with self.settings(SLACK_SIGNING_SECRET=self.SECRET, SLACK_REPLY_SYNC=True), \
                patch("api.slack_client.slack_api", side_effect=self._fake), patch("requests.post") as http:
            http.return_value.status_code = 200
            http.return_value.text = "ok"
            return self.client.post(path, body, content_type=content_type,
                                    HTTP_X_SLACK_REQUEST_TIMESTAMP=ts, HTTP_X_SLACK_SIGNATURE=sig, **(extra or {}))

    def _event(self, user, channel="C42", extra=None):
        body = json.dumps({"type": "event_callback", "team_id": "T9",
                           "event": {"type": "member_joined_channel", "user": user, "channel": channel}})
        return self._signed("/api/slack/events/", body, "application/json", extra)

    def _click(self, user, item):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"},
                   "container": {"channel_id": "D7", "message_ts": "9.9"},
                   "actions": [{"action_id": f"aidl_ack_{item}", "value": item}]}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def test_url_verification(self):
        resp = self._signed("/api/slack/events/", json.dumps({"type": "url_verification", "challenge": "abc"}),
                            "application/json")
        self.assertEqual(resp.json(), {"challenge": "abc"})

    def test_bad_signature_rejected(self):
        resp = self.client.post("/api/slack/events/", "{}", content_type="application/json",
                                HTTP_X_SLACK_REQUEST_TIMESTAMP="1", HTTP_X_SLACK_SIGNATURE="v0=bad")
        self.assertEqual(resp.status_code, 401)

    def test_joining_the_channel_onboards_the_user(self):
        _answer_policy(self.org)
        self._event("U7")
        user = AIDLUser.objects.get(microsoft_id="slack:T9:U7")
        self.assertEqual((user.role, user.organization_id), (AIDLUser.Role.LEARNER, str(self.org.pk)))
        self.assertFalse(user.licence_issued)                 # earned later, not on join
        self.assertIsNotNone(user.slack_onboarded_at)
        dms = [b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "U7"]
        self.assertEqual([d["text"] for d in dms], ["Welcome to AIDL, Jordan!"])   # programme not started yet

    def test_joiner_gets_cards_the_admin_already_sent(self):
        from .admin_setup import update

        _answer_policy(self.org)
        update(self.org, flow_started="2026-10-09T10:00:00")      # the admin started the programme
        self._event("U7")
        dms = [b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "U7"]
        self.assertEqual(len(dms), 1)
        self.assertIn("aidl_flow_start", json.dumps(dms[0]))       # the design's Welcome card

    def test_rejoin_bots_other_channels_and_retries_are_ignored(self):
        self._event("U7")
        self.calls.clear()
        self._event("U7")                                     # rejoin
        self._event("UB0T2")                                  # a bot
        self._event("U8", channel="C99")                      # another channel
        self._event("U9", extra={"HTTP_X_SLACK_RETRY_NUM": "1"})  # Slack retry
        self.assertFalse([b for m, b in self.calls if m == "chat.postMessage"])
        self.assertFalse(AIDLUser.objects.filter(microsoft_id__in=["slack:T9:U8", "slack:T9:U9"]).exists())

    def test_acknowledging_both_issues_the_learner_licence(self):
        self._event("U7")
        self._click("U7", "highway_code")
        user = AIDLUser.objects.get(microsoft_id="slack:T9:U7")
        self.assertFalse(user.licence_issued)                 # one of two done
        self._click("U7", "highway_code")                     # double click — no effect
        self._click("U7", "traffic_light")
        user.refresh_from_db()
        self.assertFalse(user.licence_issued)                 # AUP points still missing
        _accept_aup(user, self.org)
        self._click("U7", "traffic_light")                    # any acceptance re-checks the licence
        user.refresh_from_db()
        self.assertTrue(user.licence_issued)
        self.assertTrue(user.licence_number.startswith("AIDL-L-"))
        update = [b for m, b in self.calls if m == "chat.update"][-1]
        self.assertIn("Your Learner", json.dumps(update["blocks"], ensure_ascii=False))
        self.assertTrue(any(m == "chat.postMessage" and "Learner's Permit is ready" in b["text"] for m, b in self.calls))

    def test_no_free_seat_keeps_the_licence_waiting_and_tells_the_admin(self):
        # e.g. the plan was reduced after people joined
        self.org.plan = "basic"
        self.org.save()
        self._event("U7")
        self.org.seats_purchased = 0
        self.org.save()
        _accept_aup(AIDLUser.objects.get(microsoft_id="slack:T9:U7"), self.org)
        self._click("U7", "highway_code")
        self._click("U7", "traffic_light")
        self.assertFalse(AIDLUser.objects.get(microsoft_id="slack:T9:U7").licence_issued)
        self.assertTrue(any(m == "chat.postMessage" and b["channel"] == "U1" and "no free license seats" in b["text"]
                            for m, b in self.calls))

    def test_admin_center_has_no_add_user(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        text = json.dumps(admin_card_blocks(build_admin_cards_context(self.admin), "home"))
        self.assertNotIn("add-user", text)
        self.assertNotIn("aidl_tabs", text)


class AdminProgressViewTests(TestCase):
    """Admin progress view: Home summary, Team progress popup, coverage columns."""

    def setUp(self):
        from .models import Acknowledgement

        self.org = make_org(name="Secureitlab", seats_purchased=10)
        self.admin = make_user(self.org, full_name="Priya Raman")
        self.done = make_user(self.org, role=AIDLUser.Role.LEARNER, full_name="Anshul", licence_issued=True,
                              licence_number="AIDL-L-0001-0001")
        self.legacy = make_user(self.org, role=AIDLUser.Role.LEARNER, full_name="Yash", licence_issued=True,
                                licence_number="AIDL-L-0002-0002")
        self.half = make_user(self.org, role=AIDLUser.Role.LEARNER, full_name="Jordan")
        for item in ("highway_code", "traffic_light"):
            Acknowledgement.objects.create(organization_id=str(self.org.pk), user_id=str(self.done.pk), item=item, version="v1")
        Acknowledgement.objects.create(organization_id=str(self.org.pk), user_id=str(self.half.pk), item="highway_code", version="v1")
        _accept_aup(self.done, self.org)

    def test_statuses_and_counts(self):
        from .licensing import team_progress

        t = team_progress(self.org)
        status = {r["name"]: r["status"] for r in t["rows"]}
        self.assertEqual(status, {"Anshul": "licensed", "Yash": "legacy", "Jordan": "in_progress"})  # no admins
        self.assertEqual((t["members"], t["highway_code"], t["traffic_light"], t["licensed"], t["legacy"]),
                         (3, 2, 1, 2, 1))

    def test_home_card_shows_team_progress(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        from .licensing import team_progress

        t = team_progress(self.org)                       # admin not counted
        self.assertEqual((t["members"], t["highway_code"], t["traffic_light"], t["licensed"]), (3, 2, 1, 2))

    def test_team_progress_popup_lists_every_member(self):
        from .slack_modals import team_progress_view

        text = json.dumps(team_progress_view(self.org), ensure_ascii=False)
        for label in ("Anshul", "Yash", "Jordan", "LICENSED (BEFORE ACKNOWLEDGEMENT RULE)*  (1)",
                      "IN PROGRESS*  (1)", "AIDL-L-0001-0001", "old *Add User* flow", "*2/3* · 67%"):
            self.assertIn(label, text)

    def test_coverage_csv_has_journey_columns(self):
        from .slack_cards import coverage_csv, coverage_data

        csv_text = coverage_csv(coverage_data(self.org)["rows"])
        header = csv_text.splitlines()[0]
        self.assertIn("status,highway_code_accepted,traffic_light_accepted", header)
        self.assertIn("Licensed (before acknowledgement rule)", csv_text)


class GeneratedAupTests(TestCase):
    """AUP generated from the organization's policy answers + app registry."""

    ANSWERS = {
        "ai_policy": "Yes, published and enforced",
        "approved_tools": "Only company-approved tools",
        "confidential_data": "Never",
        "human_review": "Always",
        "disclosure": "Yes, always",
        "regulation": "DPDP Act (India)",
        "incident_reporting": "Through HR",
        "training_frequency": "Every year",
    }

    def setUp(self):
        from .org_policy import save_answers

        self.org = make_org(name="Secureitlab", slack_team_id="T9", slack_channel_id="C42")
        save_answers(self.org, self.ANSWERS)
        org_id = str(self.org.pk)
        RegisteredApp.objects.create(organization_id=org_id, name="Claude Enterprise", app_type="ai", status="approved",
                                     category="Assistant / Chatbot", data_allowed="internal_confidential")
        RegisteredApp.objects.create(organization_id=org_id, name="Otter.ai", app_type="ai", status="rejected")
        RegisteredApp.objects.create(organization_id=org_id, name="Jira", app_type="it", status="approved", data_allowed="internal")
        RegisteredApp.objects.create(organization_id=org_id, name="Personal drives", app_type="it", status="rejected")
        RegisteredApp.objects.create(organization_id=org_id, name="Pending tool", app_type="ai", status="pending")
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.learner = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U7", full_name="Jordan Ellis")

    def test_generated_from_answers_and_apps(self):
        from .aup import generate_aup

        aup = generate_aup(self.org)
        self.assertEqual([a["name"] for a in aup["allowed_ai"]], ["Claude Enterprise"])
        self.assertEqual([a["name"] for a in aup["allowed_it"]], ["Jira"])
        prohibited = [a["name"] for a in aup["prohibited"]]
        self.assertIn("Otter.ai", prohibited)
        self.assertIn("Personal drives", prohibited)
        self.assertNotIn("Pending tool", prohibited + [a["name"] for a in aup["allowed_ai"]])
        self.assertEqual(aup["red_list"][0], "Any confidential or customer data")   # confidential_data = Never
        rules = " ".join(aup["rules"])
        for phrase in ("Use only the apps listed", "Never put confidential", "must review every AI output",
                       "Always say when content was created with AI", "the DPDP Act", "report it to HR"):
            self.assertIn(phrase, rules)

    def test_version_changes_when_the_app_list_changes(self):
        from .aup import generate_aup

        before = generate_aup(self.org)["version"]
        self.assertEqual(before, generate_aup(self.org)["version"])           # stable
        RegisteredApp.objects.create(organization_id=str(self.org.pk), name="Notion AI", app_type="ai", status="approved")
        self.assertNotEqual(before, generate_aup(self.org)["version"])

    def test_card_layout_and_accept(self):
        from .aup import generate_aup
        from .licensing import acknowledge
        from .slack_blocks import aup_blocks

        aup = generate_aup(self.org)
        text = json.dumps(aup_blocks(aup, self.learner), ensure_ascii=False)
        for label in ("Your AI Acceptable Use Policy", "Prepared for *Jordan Ellis*", aup["version"],
                      "AI APPS YOU CAN USE", "IT APPS YOU CAN USE", "DON'T USE", "NEVER PUT INTO ANY AI TOOL",
                      "YOUR RULES", "Claude Enterprise", "Internal + Confidential data", "aidl_ack_aup"):
            self.assertIn(label, text)
        acknowledge(self.learner, "aup", aup["version"])
        self.learner.refresh_from_db()
        self.assertTrue(self.learner.aup_signed)
        text = json.dumps(aup_blocks(aup, self.learner), ensure_ascii=False)
        self.assertNotIn("aidl_ack_aup", text)
        self.assertIn("You accepted this policy", text)



class PlanLimitTests(TestCase):
    """Trial / Basic plans: the user limit is enforced when people join the
    AIDL channel, licences earned keep counting after removal, and the
    plan's awareness package is delivered after the licence."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"), plan="trial", seats_purchased=50)
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []
        _approve_all(self.org)

    def _fake(self, method, token, *, json=None, params=None):
        self.calls.append((method, json or params))
        if method == "users.info":
            uid = (params or {}).get("user")
            return {"ok": True, "user": {"id": uid, "tz_offset": 19800,
                                         "profile": {"email": f"{uid.lower()}@acme.example", "real_name": f"Person {uid}"}}}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D7"}}
        if method == "chat.scheduleMessage":
            return {"ok": True, "scheduled_message_id": f"Q{len(self.calls)}"}
        return {"ok": True, "ts": "1.1"}

    def _event(self, user, kind="member_joined_channel"):
        body = json.dumps({"type": "event_callback", "team_id": "T9",
                           "event": {"type": kind, "user": user, "channel": "C42"}})
        return self._signed("/api/slack/events/", body, "application/json")

    def _click(self, user, item):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"},
                   "container": {"channel_id": "D7", "message_ts": "9.9"},
                   "actions": [{"action_id": f"aidl_ack_{item}", "value": item}]}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _member(self, uid):
        return AIDLUser.objects.filter(microsoft_id=f"slack:T9:{uid}").first()

    def _fill_trial(self):
        for uid in ("U2", "U3", "U4", "U5", "U6"):
            self._event(uid)

    def _licence(self, uid):
        _accept_aup(self._member(uid), self.org)
        self._click(uid, "highway_code")
        self._click(uid, "traffic_light")
        return self._member(uid)

    def test_trial_refuses_sixth_user(self):
        self._fill_trial()
        self.calls.clear()
        self._event("U7")
        self.assertIsNone(self._member("U7"))
        self.assertIn(("conversations.kick", {"channel": "C42", "user": "U7"}), self.calls)
        admin_dm = [j for m, j in self.calls if m == "chat.postMessage" and j["channel"] == "U1"]
        self.assertIn("plan is full", admin_dm[0]["text"] + json.dumps(admin_dm[0]["blocks"]))

    def test_admins_do_not_use_seats(self):
        make_user(self.org, microsoft_id="slack:T9:U8", full_name="Second Admin")
        self._fill_trial()
        from .plans import usage
        self.assertEqual(usage(self.org)["used"], 5)

    def test_removing_unlicensed_member_frees_the_seat(self):
        self._fill_trial()
        self._event("U2", "member_left_channel")
        self.assertIsNotNone(self._member("U2").slack_left_at)
        self._event("U7")
        self.assertIsNotNone(self._member("U7"))

    def test_licensed_member_keeps_seat_after_removal(self):
        self._fill_trial()
        self._licence("U2")
        self._event("U2", "member_left_channel")
        self._event("U7")
        self.assertIsNone(self._member("U7"))  # no seat recycling
        # Added back: same licence, no new seat needed.
        number = self._member("U2").licence_number
        self.calls.clear()
        self._event("U2")
        u2 = self._member("U2")
        self.assertIsNone(u2.slack_left_at)
        self.assertEqual(u2.licence_number, number)
        self.assertNotIn("conversations.kick", [m for m, _ in self.calls])

    def test_removed_member_cannot_earn_licence(self):
        from .licensing import issue_learner_if_ready

        self._event("U2")
        self._event("U2", "member_left_channel")
        self._click("U2", "highway_code")
        self._click("U2", "traffic_light")
        self.assertFalse(self._member("U2").licence_issued)
        self.assertEqual(issue_learner_if_ready(self._member("U2"), self.org), "left")

    def test_licence_starts_default_package(self):
        from .models import PackageDelivery

        self._event("U2")
        self.calls.clear()
        u2 = self._licence("U2")
        rows = PackageDelivery.objects.filter(user_id=str(u2.pk))
        self.assertEqual(rows.count(), 12)
        self.assertEqual(rows.filter(status="sent").get().card_no, "006")  # card 1 right away
        scheduled = [j for m, j in self.calls if m == "chat.scheduleMessage"]
        self.assertEqual(len(scheduled), 4)   # Trial: 1 a month; Slack takes 120 days ahead
        self.assertEqual(scheduled[0]["text"], "AIDL card: Traffic Light Check")  # sheet's posting order

    def test_leaving_cancels_scheduled_cards_and_rejoin_resumes(self):
        from .models import PackageDelivery

        self._event("U2")
        u2 = self._licence("U2")
        self.calls.clear()
        self._event("U2", "member_left_channel")
        self.assertEqual(len([m for m, _ in self.calls if m == "chat.deleteScheduledMessage"]), 4)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk), status="cancelled").count(), 11)
        self.calls.clear()
        self._event("U2")
        self.assertEqual(len([m for m, _ in self.calls if m == "chat.scheduleMessage"]), 4)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk), status="sent").count(), 1)

    def test_upgrade_to_basic_schedules_remaining_cards(self):
        from .models import PackageDelivery
        from .package_delivery import sync_organization

        self._event("U2")
        u2 = self._licence("U2")
        self.org.plan = "basic"
        self.org.save()
        _approve_all(self.org)                                    # the Basic cards were approved
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            sync_organization(self.org)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk)).count(), 104)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk), status="scheduled").count(), 103)

    def test_post_times_two_a_week_at_ten_local(self):
        import datetime as dt

        from .package_delivery import post_times

        start = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)  # Tuesday
        times = post_times(start, 4, 2, tz_offset=19800)  # IST
        local = [t + dt.timedelta(seconds=19800) for t in times]
        self.assertEqual([t.strftime("%a %H:%M") for t in local], ["Thu 10:00", "Mon 10:00", "Thu 10:00", "Mon 10:00"])

    def test_admin_home_shows_plan(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        from .admin_setup import update

        self._fill_trial()
        _answer_policy(self.org)
        update(self.org, admins_step="skipped")
        text = json.dumps(admin_card_blocks(build_admin_cards_context(self.admin), "home"), ensure_ascii=False)
        self.assertIn("5 of 5 users on your Trial plan", text)   # step 3
        self.assertIn("plan is full", text)


class RemovePeopleTests(TestCase):
    """The first admin removes admins; any admin removes team members."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake
    _event = PlanLimitTests._event

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"), plan="trial")
        self.owner = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.co = make_user(self.org, microsoft_id="slack:T9:U2", full_name="Arsh Co")
        self.calls = []

    def _press(self, user, action_id, value):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"},
                   "view": {"id": "V1", "private_metadata": "{}"},
                   "actions": [{"action_id": action_id, "value": value}]}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _popup_text(self):
        return json.dumps([j for m, j in self.calls if m == "views.update"][-1]["view"], ensure_ascii=False)

    def test_add_admin_popup_lists_admins_with_remove_for_owner_only(self):
        from .slack_cards import build_admin_cards_context
        from .slack_modals import add_admin_view

        owner_view = json.dumps(add_admin_view(build_admin_cards_context(self.owner), {}))
        self.assertEqual(owner_view.count("aidl_admin_remove"), 1)  # co-admin only, not the owner
        self.assertIn(str(self.co.pk), owner_view)

    def test_owner_removes_co_admin_who_becomes_member(self):
        self._press("U1", "aidl_admin_remove", str(self.co.pk))
        self.co.refresh_from_db()
        self.assertEqual(self.co.role, AIDLUser.Role.LEARNER)
        self.assertIn("no longer an admin", self._popup_text())
        self.assertIsNotNone(self.co.slack_onboarded_at)  # got the learner Welcome + dashboard

    def test_removed_admin_kicked_when_plan_full(self):
        for uid in ("U3", "U4", "U5", "U6", "U7"):
            self._event(uid)
        self._press("U1", "aidl_admin_remove", str(self.co.pk))
        self.co.refresh_from_db()
        self.assertEqual(self.co.role, AIDLUser.Role.LEARNER)
        self.assertIsNotNone(self.co.slack_left_at)
        self.assertIn(("conversations.kick", {"channel": "C42", "user": "U2"}), self.calls)

    def test_co_admin_cannot_remove_admins(self):
        self._press("U2", "aidl_admin_remove", str(self.owner.pk))
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.role, AIDLUser.Role.ADMIN)
        self.assertIn("Only the master admin", self._popup_text())

    def test_admin_removes_member(self):
        self._event("U3")
        member = AIDLUser.objects.get(microsoft_id="slack:T9:U3")
        self.calls.clear()
        self._press("U2", "aidl_member_remove", str(member.pk))  # co-admin may remove members
        member.refresh_from_db()
        self.assertIsNotNone(member.slack_left_at)
        self.assertIn(("conversations.kick", {"channel": "C42", "user": "U3"}), self.calls)
        self.assertIn("seat is free again", self._popup_text())

    def test_progress_popup_has_remove_buttons(self):
        from .slack_modals import team_progress_view

        self._event("U3")
        self.assertIn("aidl_member_remove", json.dumps(team_progress_view(self.org)))


class OrganizationOwnerTests(TestCase):
    """The first admin is the recorded owner, not just the oldest account."""

    def test_owner_wins_over_older_admin(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context
        from .slack_onboarding import primary_admin

        org = make_org(slack_team_id="T9")
        older = make_user(org, microsoft_id="slack:T9:U2", full_name="Older Admin")
        owner = make_user(org, microsoft_id="slack:T9:U1", full_name="Owner")
        self.assertEqual(primary_admin(org).pk, older.pk)          # no owner yet → oldest
        org.owner_user_id = str(owner.pk)
        org.save()
        self.assertEqual(primary_admin(org).pk, owner.pk)
        # Only the owner can change the plan.
        self.assertIn("aidl_upgrade", json.dumps(admin_card_blocks(build_admin_cards_context(owner), "home")))
        self.assertNotIn("aidl_upgrade", json.dumps(admin_card_blocks(build_admin_cards_context(older), "home")))

    def test_set_owner_command(self):
        from django.core.management import call_command

        org = make_org(slack_team_id="T9")
        make_user(org, microsoft_id="slack:T9:U2", email="older@x.example")
        owner = make_user(org, microsoft_id="slack:T9:U1", email="boss@x.example")
        call_command("set_plan", "--team", "T9", "--owner", "boss@x.example", stdout=__import__("io").StringIO())
        org.refresh_from_db()
        self.assertEqual(org.owner_user_id, str(owner.pk))


class SetupStepsTests(TestCase):
    """Admin Center step-by-step setup (admin_setup.py)."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake
    _event = PlanLimitTests._event

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"), plan="trial")
        self.owner = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []

    def _press(self, action_id, value="x", view=None):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "trigger_id": "TR", "channel": {"id": "C42"}, "response_url": "https://hooks.example/r",
                   "actions": [{"action_id": action_id, "value": value}]}
        if view:
            payload["view"] = view
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _submit(self, callback, values):
        from urllib.parse import urlencode

        payload = {"type": "view_submission", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "view": {"callback_id": callback, "private_metadata": "{}", "state": {"values": values}}}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _home(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        return json.dumps(admin_card_blocks(build_admin_cards_context(self.owner), "home"), ensure_ascii=False)

    def test_steps_unlock_in_order(self):
        from .admin_setup import update

        self.assertIn("2 · Add admins (optional)*  —  _after step 1_", self._home())
        _answer_policy(self.org)
        self.assertIn("aidl_setup_skip", self._home())
        self._press("aidl_setup_skip", "admins")
        self.assertIn("*3 · Add your team*", self._home())
        self._event("U5")                                         # someone joins
        home = self._home()
        self.assertIn("✅  *3 · Team*  ·  1 member", home)
        self.assertIn("*4 · Card approval*\\nNothing reaches your team", home)
        self.assertEqual(home.count("aidl_send_item"), 0)         # can't start before the cards are approved
        _approve_all(self.org)
        home = self._home()
        self.assertIn("✅  *4 · Card approval*  ·  onboarding cards approved", home)
        self.assertEqual(home.count("aidl_send_item"), 1)         # Start the programme
        update(self.org, flow_started="2026-10-09")
        self.assertIn("*6 · Licences & awareness cards*", self._home())

    def test_start_programme_sends_welcome_and_remind(self):
        from .admin_setup import started

        _answer_policy(self.org)
        self._event("U5")
        self.calls.clear()
        self._press("aidl_send_item", "flow")                     # not approved yet → nothing sent
        self.assertFalse(any(m == "chat.postMessage" and b["channel"] == "U5" for m, b in self.calls))
        _approve_all(self.org)
        self._press("aidl_send_item", "flow")
        dm = next(b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "U5")
        self.assertIn("aidl_flow_start", json.dumps(dm))
        self.org.refresh_from_db()
        self.assertTrue(started(self.org))
        self.calls.clear()
        self._press("aidl_remind_item", "flow")                   # U5 hasn't finished → current card again
        self.assertTrue(any(m == "chat.postMessage" and b["channel"] == "U5" for m, b in self.calls))

    PEOPLE = [("U6", "Bharti Mali"), ("U8", "Yana"), ("U9", "Ritika")]

    def _people_patches(self, people=None):
        """The workspace members who can be added (not in the channel)."""
        people = self.PEOPLE if people is None else people
        return (patch("api.slack_modals.workspace_members", return_value=people),
                patch("api.slack_modals.in_background", side_effect=lambda fn, *a: fn(*a)))

    def _picked(self, *uids, block="people_0"):
        return {block: {"v": {"type": "multi_static_select",
                              "selected_options": [{"value": u} for u in uids]}}}

    def test_people_dropdown_has_select_all_on_top_and_no_checkbox(self):
        from .slack_cards import build_admin_cards_context
        from .slack_modals import SELECT_ALL, add_people_view

        view = add_people_view({}, build_admin_cards_context(self.owner), self.PEOPLE)
        block = next(b for b in view["blocks"] if b.get("block_id", "").startswith("people"))
        picker = block["element"]
        self.assertEqual(picker["type"], "multi_static_select")                  # searchable multi-select
        self.assertTrue(block["dispatch_action"])
        self.assertEqual(picker["options"][0]["value"], SELECT_ALL)              # "Select all people" on top
        self.assertEqual(picker["options"][0]["text"]["text"], "✅ Select all people (3)")
        self.assertEqual([o["value"] for o in picker["options"][1:]], ["U6", "U8", "U9"])
        self.assertNotIn("checkboxes", json.dumps(view))                         # no tick box below the input
        self.assertNotIn("isn't in the AIDL channel yet", json.dumps(view))
        self.assertTrue(block["optional"])

    def test_select_all_selects_everyone_as_removable_chips(self):
        from urllib.parse import urlencode

        view = {"id": "V9", "hash": "h1", "callback_id": "aidl_add_people",
                "private_metadata": json.dumps({"channel_id": "C42", "people_version": 0})}
        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "view": view, "actions": [{"action_id": "v", "block_id": "people_0",
                                              "selected_options": [{"value": "U8"}, {"value": "__all__"}]}]}
        with self._people_patches()[0]:
            self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                         "application/x-www-form-urlencoded")
        update = next(b for m, b in self.calls if m == "views.update")
        self.assertEqual(update["view_id"], "V9")
        block = next(b for b in update["view"]["blocks"] if b.get("block_id", "").startswith("people"))
        self.assertEqual(block["block_id"], "people_1")                          # new id → Slack shows the selection
        chosen = [o["value"] for o in block["element"]["initial_options"]]
        self.assertEqual(chosen, ["U6", "U8", "U9"])                             # everyone, once each, as chips
        self.assertNotIn("__all__", chosen)

    def test_deselected_people_are_not_added(self):
        p1, p2 = self._people_patches()
        with p1, p2:                                                             # U9 removed after "Select all"
            self.assertEqual(self._submit("aidl_add_people", self._picked("U6", "U8", block="people_1")).json(),
                             {"response_action": "clear"})
        invited = [b["users"] for m, b in self.calls if m == "conversations.invite"]
        self.assertEqual(invited, ["U6", "U8"])

    def test_select_all_applies_to_everyone_not_just_search_results(self):
        from .slack_modals import people_options

        with self._people_patches()[0]:
            options = people_options(self.org, "bha")                            # searching "bha"
        self.assertEqual(options[0]["value"], "__all__")                         # still "Select all people (3)"
        self.assertIn("(3)", options[0]["text"]["text"])
        self.assertEqual([o["value"] for o in options[1:]], ["U6"])              # the search result
        p1, p2 = self._people_patches()
        with p1, p2:                                                             # "Select all" chosen from a search
            self._submit("aidl_add_people", self._picked("__all__"))
        self.assertEqual([b["users"] for m, b in self.calls if m == "conversations.invite"], ["U6", "U8", "U9"])

    def test_excludes_channel_members_and_duplicates(self):
        from .slack_modals import workspace_members

        def fake(method, token, *, json=None, params=None):
            if method == "conversations.members":
                return {"ok": True, "members": ["U1", "U5"]}
            if method == "users.list":
                return {"ok": True, "members": [
                    {"id": "U1"}, {"id": "U5"}, {"id": "U6", "profile": {"real_name": "Bharti Mali"}},
                    {"id": "U7", "deleted": True}, {"id": "UB", "is_bot": True}, {"id": "USLACKBOT"},
                    {"id": "UG", "is_restricted": True}, {"id": "U8", "real_name": "Yana"}]}
            return {"ok": True}

        with patch("api.slack_client.slack_api", side_effect=fake):
            self.assertEqual(workspace_members(self.org), [("U6", "Bharti Mali"), ("U8", "Yana")])
        p1, p2 = self._people_patches([("U6", "Bharti Mali")])
        with p1, p2:  # U6 twice, U5 already in the channel
            self._submit("aidl_add_people", self._picked("U6", "U6", "U5"))
        self.assertEqual([b["users"] for m, b in self.calls if m == "conversations.invite"], ["U6"])

    def test_seat_limit_and_nothing_picked(self):
        people = [(f"U{n}", f"Person {n}") for n in range(10, 20)]
        p1, p2 = self._people_patches(people)
        with p1, p2:
            resp = self._submit("aidl_add_people", self._picked("__all__", block="people_2")).json()
            self.assertEqual(resp["response_action"], "errors")
            self.assertIn("seats left", resp["errors"]["people_2"])              # error on the People field
            self.assertIn("Remove some people", resp["errors"]["people_2"])
            resp = self._submit("aidl_add_people", self._picked(block="people_2")).json()
            self.assertIn("Select all people", resp["errors"]["people_2"])
        self.assertFalse(any(m == "conversations.invite" for m, _b in self.calls))

    def test_old_home_button_points_to_live_message(self):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "response_url": "https://hooks.example/r", "container": {"is_ephemeral": True},
                   "actions": [{"action_id": "aidl_tab_home", "value": "home"}]}
        with patch("api.slack_interactions._post_reply") as post:
            self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                         "application/x-www-form-urlencoded")
        body = post.call_args[0][1]
        self.assertTrue(body["replace_original"])                # the stale private copy is replaced
        self.assertNotIn("blocks", body)
        self.assertIn("AIDL Admin Center", body["text"])

    def test_reset_command(self):
        from django.core.management import call_command

        from .admin_setup import is_sent, update
        from .models import Acknowledgement
        from .org_policy import policy_completed

        _answer_policy(self.org)
        update(self.org, aup_sent="2026-10-09")
        learner = make_user(self.org, role=AIDLUser.Role.LEARNER, aup_signed=True)
        Acknowledgement.objects.create(organization_id=str(self.org.pk), user_id=str(learner.pk), item="aup", version="AUP-1")
        Acknowledgement.objects.create(organization_id=str(self.org.pk), user_id=str(learner.pk), item="highway_code", version="v1")
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            call_command("reset_setup", "--team", "T9", stdout=__import__("io").StringIO())
        self.org.refresh_from_db()
        learner.refresh_from_db()
        self.assertFalse(policy_completed(self.org))
        self.assertFalse(is_sent(self.org, "aup"))
        self.assertFalse(learner.aup_signed)
        self.assertEqual(list(Acknowledgement.objects.filter(user_id=str(learner.pk)).values_list("item", flat=True)),
                         ["highway_code"])


class PopupTimingTests(TestCase):
    """Popup buttons open a Loading… popup before any slow work, then fill it."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"))
        self.owner = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []

    def test_create_aup_opens_before_building_admin_center(self):
        from urllib.parse import urlencode

        order = []
        real = __import__("api.slack_cards", fromlist=["x"]).build_admin_cards_context

        def slow_context(user):
            order.append("context")
            return real(user)

        def fake(method, token, *, json=None, params=None):
            if method in ("views.open", "views.update"):
                order.append(f"{method}:{json['view'].get('callback_id')}")
                return {"ok": True, "view": {"id": "V1"}}
            return self._fake(method, token, json=json, params=params)

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "trigger_id": "TR", "channel": {"id": "C42"},
                   "actions": [{"action_id": "aidl_policy_open", "value": "policy"}]}
        with patch("api.slack_interactions.build_admin_cards_context", side_effect=slow_context):
            self._fake_saved = self._fake
            with self.settings(SLACK_SIGNING_SECRET=self.SECRET, SLACK_REPLY_SYNC=True), \
                    patch("api.slack_client.slack_api", side_effect=fake), patch("requests.post"):
                import hashlib, hmac, time
                body = urlencode({"payload": json.dumps(payload)})
                ts = str(int(time.time()))
                sig = "v0=" + hmac.new(self.SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
                self.client.post("/api/slack/interactions/", body, content_type="application/x-www-form-urlencoded",
                                 HTTP_X_SLACK_REQUEST_TIMESTAMP=ts, HTTP_X_SLACK_SIGNATURE=sig)
        self.assertEqual(order[0], "views.open:aidl_loading")     # before the slow Admin Center data
        self.assertIn("views.update:aidl_aup_section", order)


class AupBuilderTests(SimpleTestCase):
    """AUP generator (aup_builder.py) built from the manager's question bank."""

    BASE = {"0.1": "Acme Trading W.L.L.", "0.3": "0.3.a", "0.4": "0.4.b", "0.5": "Bahrain", "0.6": ["0.6.b"],
            "0.8": "it@acme.example", "0.9": "2026-11-01", "5.4": "5.4.c", "7.2": "ChatGPT Enterprise",
            "8.5": "the Communications team"}
    FILLS = {"6.2.b": "SharePoint", "9.2.a": "security@acme.example"}

    def _answers(self, **changes):
        from . import aup_builder

        answers = aup_builder.recommended_answers()
        answers.update(self.BASE)
        answers.update({k.replace("_", "."): v for k, v in changes.items()})
        return answers

    def test_bank_loaded(self):
        from . import aup_builder

        bank = aup_builder.bank()
        self.assertEqual(len(bank["sections"]), 15)
        self.assertEqual(len(bank["questions"]), 68)

    def test_recommended_answers_generate_all_14_points(self):
        from . import aup_builder

        aup = aup_builder.generate(self._answers(), self.FILLS)
        self.assertEqual([p["no"] for p in aup["points"]], list(range(1, 15)))
        self.assertTrue(all(p["clauses"] for p in aup["points"]))
        text = aup_builder.as_text(aup)
        self.assertIn('This policy applies to Acme Trading W.L.L. ("the Company").', text)
        self.assertIn("The Company actively monitors the use of its systems.", text)          # reviewed wording
        self.assertIn("Report incidents via: email: security@acme.example", text)             # specify fill
        self.assertIn("This policy is effective from 1 November 2026.", text)                 # date, point 14
        self.assertTrue(aup["version"].startswith("AUP-"))

    def test_yes_no_answer(self):
        from . import aup_builder

        yes = aup_builder.as_text(aup_builder.generate(self._answers(), self.FILLS))
        no = aup_builder.as_text(aup_builder.generate(self._answers(**{"9_4": "9.4.b"}), self.FILLS))
        self.assertIn("Reports made in good faith will never lead to punishment.", yes)
        self.assertNotIn("good faith", no)                                                     # "No" adds nothing

    def test_show_if_hides_follow_ups(self):
        from . import aup_builder

        answers = self._answers(**{"2_1": "2.1.c"})                                           # No monitoring
        hidden = {q["id"] for q in aup_builder.bank()["questions"]} - {q["id"] for q in aup_builder.visible_questions(answers)}
        self.assertTrue({"2.2", "2.3", "2.4"} <= hidden)
        point2 = aup_builder.generate(answers, self.FILLS)["points"][1]
        self.assertEqual([c["text"] for c in point2["clauses"]], ["The Company does not monitor the use of its systems."])

    def test_validation_lists_missing_answers_and_fills(self):
        from . import aup_builder

        errors = aup_builder.validate(aup_builder.recommended_answers())
        for qid in ("0.1", "0.8", "0.9", "6.2", "9.2"):
            self.assertIn(qid, errors)
        with self.assertRaises(aup_builder.AupAnswersError):
            aup_builder.generate({}, {})

    def test_other_option_uses_typed_text(self):
        from . import aup_builder

        answers = self._answers(**{"4_1": ["4.1.a", "4.1.x"]})
        text = aup_builder.as_text(aup_builder.generate(answers, {**self.FILLS, "4.1.x": "Using AI to write exam answers"}))
        self.assertIn("Using AI to write exam answers", text)

    def test_preview_endpoint(self):
        resp = self.client.post("/api/aup/preview/", {"use_recommended": True, "answers": self.BASE, "fills": self.FILLS},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.json()["aup"]["points"]), 14)
        bad = self.client.post("/api/aup/preview/", {"answers": {}}, content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("0.1", bad.json()["errors"])
        self.assertEqual(self.client.get("/api/aup/questions/").json()["sections"][0]["title"], "Organization Profile")



class AupQuestionnaireSlackTests(TestCase):
    """Create AUP in Slack from the question bank: 15 screens, then one AUP
    point per card for employees; the licence needs all 14 points."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake
    _event = PlanLimitTests._event

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(name="Acme", slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"))
        self.owner = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []

    def _post(self, payload):
        from urllib.parse import urlencode

        payload = {"team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"}, **payload}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _screen(self, section=0):
        from . import aup_slack, aup_store

        d = aup_store.draft(self.org)
        return aup_slack.section_view(self.org, section, d["answers"], d.get("fills") or {}, {})

    def _values(self, view):
        """What Slack sends back for a screen: the pre-selected / pre-filled values."""
        values = {}
        for b in view["blocks"]:
            if b["type"] != "input":
                continue
            el = b["element"]
            if "initial_option" in el:
                v = {"selected_option": {"value": el["initial_option"]["value"]}}
            elif "initial_options" in el:
                v = {"selected_options": [{"value": o["value"]} for o in el["initial_options"]]}
            elif "initial_date" in el:
                v = {"selected_date": el["initial_date"]}
            elif el["type"] in ("checkboxes", "multi_static_select"):
                v = {"selected_options": []}
            else:
                v = {"value": el.get("initial_value", "")}
            values[b["block_id"]] = {"v": v}
        return values

    def _submit(self, view, values):
        return self._post({"type": "view_submission", "view": {**view, "state": {"values": values}}}).json()

    def test_first_screen_has_recommended_answers_and_profile(self):
        view = self._screen(0)
        self.assertEqual(view["callback_id"], "aidl_aup_section")
        self.assertEqual(view["submit"]["text"], "Next →")
        text = json.dumps(view, ensure_ascii=False)
        self.assertIn("Organization Profile", text)
        self.assertIn('"initial_value": "Acme"', text)              # organization name pre-filled
        policy = next(b for b in self._screen(2)["blocks"] if b.get("block_id") == "q|2.1")
        self.assertEqual(policy["element"]["initial_option"]["value"], "2.1.a")   # recommended pre-selected
        self.assertTrue(policy["dispatch_action"])                  # changing it shows / hides follow-ups

    def test_show_if_hides_follow_ups_on_redraw(self):
        from . import aup_store

        view = self._screen(2)
        self.assertIn("q|2.2", [b.get("block_id") for b in view["blocks"]])
        values = self._values(view)
        values["q|2.1"] = {"v": {"selected_option": {"value": "2.1.c"}}}     # No monitoring
        self._post({"type": "block_actions", "view": {**view, "id": "V1", "hash": "h", "state": {"values": values}},
                    "actions": [{"action_id": "v", "block_id": "q|2.1"}]})
        update = next(b for m, b in self.calls if m == "views.update")
        self.assertNotIn("q|2.2", [b.get("block_id") for b in update["view"]["blocks"]])
        self.org.refresh_from_db()
        self.assertEqual(aup_store.draft(self.org)["answers"]["2.1"], "2.1.c")

    def test_next_validates_then_moves_on(self):
        view = self._screen(0)
        values = self._values(view)
        resp = self._submit(view, values)                           # email / date / laws still empty
        self.assertEqual(resp["response_action"], "errors")
        self.assertIn("q|0.8", resp["errors"])
        values.update({"q|0.3": {"v": {"selected_option": {"value": "0.3.a"}}},
                       "q|0.4": {"v": {"selected_option": {"value": "0.4.b"}}},
                       "q|0.5": {"v": {"value": "Bahrain"}}, "q|0.6": {"v": {"selected_options": [{"value": "0.6.b"}]}},
                       "q|0.8": {"v": {"value": "it@acme.example"}}, "q|0.9": {"v": {"selected_date": "2026-11-01"}}})
        resp = self._submit(view, values)
        self.assertEqual(resp["response_action"], "update")
        self.assertIn("Scope & Applicability", json.dumps(resp["view"]))
        self.assertEqual(json.loads(resp["view"]["private_metadata"])["section"], 1)

    def test_last_screen_generates_aup_with_send_and_view(self):
        from . import aup_builder, aup_store

        answers = aup_builder.recommended_answers()
        answers.update(AUP_BASE)
        aup_store.save_draft(self.org, answers, dict(AUP_FILLS), 14)
        view = self._screen(14)
        self.assertEqual(view["submit"]["text"], "Generate AUP")
        resp = self._submit(view, self._values(view))
        self.assertEqual(resp["response_action"], "update")
        text = json.dumps(resp["view"], ensure_ascii=False)
        for label in ("Your AUP is ready", "aidl_crv_approve_onb", "aidl_aup_view", "1 · Scope & Applicability",
                      "14 · Acknowledgment & Review"):
            self.assertIn(label, text)
        self.org.refresh_from_db()
        self.assertTrue(aup_store.is_complete(self.org))

    def test_admin_center_create_then_view(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        home = json.dumps(admin_card_blocks(build_admin_cards_context(self.owner), "home"), ensure_ascii=False)
        self.assertIn("📝 Create AUP", home)
        _answer_policy(self.org)
        home = json.dumps(admin_card_blocks(build_admin_cards_context(self.owner), "home"), ensure_ascii=False)
        self.assertIn("View AUP", home)
        self.assertIn("Change AUP", home)
        self.assertNotIn("📝 Create AUP", home)

    def test_continue_where_left_off(self):
        from . import aup_store
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        d = aup_store.draft(self.org)
        aup_store.save_draft(self.org, d["answers"], {}, 4)
        home = json.dumps(admin_card_blocks(build_admin_cards_context(self.owner), "home"), ensure_ascii=False)
        self.assertIn("Continue AUP (step 5 of 15)", home)

    def test_employee_accepts_one_point_at_a_time_then_licence(self):
        from . import aup_store
        from .admin_setup import update
        from .slack_blocks import user_dashboard_blocks

        _answer_policy(self.org)
        update(self.org, aup_sent="2026-10-13")
        self._event("U5")
        member = AIDLUser.objects.get(microsoft_id="slack:T9:U5")
        self.assertIn("point 1 of 14", json.dumps(user_dashboard_blocks(member, self.org, "aup")))
        for item in ("highway_code", "traffic_light"):
            self._post({"type": "block_actions", "user": {"id": "U5", "team_id": "T9"},
                        "container": {"channel_id": "D5", "message_ts": "1.1"},
                        "actions": [{"action_id": f"aidl_ack_{item}", "value": item}]})
        member.refresh_from_db()
        self.assertFalse(member.licence_issued)                     # AUP not accepted yet
        self._post({"type": "block_actions", "user": {"id": "U5", "team_id": "T9"},
                    "container": {"channel_id": "D5", "message_ts": "1.1"},
                    "actions": [{"action_id": "aidl_ack_aup", "value": "aup_3"}]})   # out of order → ignored
        self.assertEqual(aup_store.points_accepted(member, self.org), set())
        for n in range(1, 15):
            self._post({"type": "block_actions", "user": {"id": "U5", "team_id": "T9"},
                        "container": {"channel_id": "D5", "message_ts": "1.1"},
                        "actions": [{"action_id": "aidl_ack_aup", "value": f"aup_{n}"}]})
            if n == 1:
                last = [b for m, b in self.calls if m == "chat.update"][-1]
                self.assertIn("point 2 of 14", json.dumps(last["blocks"]))   # next point appears at once
        member.refresh_from_db()
        self.assertTrue(member.licence_issued)
        self.assertTrue(member.aup_signed)

    def test_changed_aup_shows_send_new_version(self):
        from .admin_setup import aup_change, send_item
        from .aup_slack import report_view
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        _answer_policy(self.org)
        make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U5", full_name="Jordan")
        from .admin_setup import update

        update(self.org, admins_step="skipped")
        _approve_all(self.org)
        with patch("api.slack_client.slack_api", return_value={"ok": True}):
            send_item(self.org, "aup")
        self.assertFalse(aup_change(self.org)["changed"])
        _answer_policy(self.org, **{"2.1": "2.1.c"})               # Change AUP
        self.org.refresh_from_db()
        self.assertTrue(aup_change(self.org)["changed"])

        def home():
            return json.dumps(admin_card_blocks(build_admin_cards_context(self.owner), "home"), ensure_ascii=False)

        self.assertIn("approve the new version in step 4", home())    # the new version needs approval first
        self.assertNotIn("Send new version", home())
        self.assertIn("aidl_crv_approve_onb", json.dumps(report_view(self.org)))
        from . import card_approval

        card_approval.approve_onboarding(self.org, "aup", self.owner)
        self.assertIn("Send new version", home())
        self.assertIn("Your updated AUP is approved", json.dumps(report_view(self.org)))

    def test_fresh_reset_clears_acceptances_and_licences(self):
        from django.core.management import call_command

        from . import aup_store
        from .models import Acknowledgement

        _answer_policy(self.org)
        member = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U5", licence_issued=True,
                           licence_number="AIDL-L-1111-2222")
        _accept_aup(member, self.org)
        Acknowledgement.objects.create(organization_id=str(self.org.pk), user_id=str(member.pk), item="highway_code", version="v1")
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            call_command("reset_setup", "--team", "T9", "--fresh", stdout=__import__("io").StringIO())
        member.refresh_from_db()
        self.org.refresh_from_db()
        self.assertFalse(member.licence_issued)
        self.assertEqual(member.licence_number, "")
        self.assertFalse(Acknowledgement.objects.filter(user_id=str(member.pk)).exists())
        self.assertFalse(aup_store.is_complete(self.org))



@override_settings(AUTH_EMAIL_OTP=False, AUTH_CAPTCHA=False)  # direct path; OTP/captcha tested below
class SignupWithoutMobileTests(TestCase):
    def test_signup_without_mobile_and_underscore_password(self):
        resp = self.client.post("/api/auth/signup/", {
            "enroll_as": "individual", "first_name": "Shivraj", "last_name": "Choudhary",
            "email": "shivraj.test@example.com", "password": "Bc99HntU5wKAWgf_", "confirm_password": "Bc99HntU5wKAWgf_",
            "country": "Bahrain", "state": "Southern", "city": "Isa Town"}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["user"]["license_class"], "class_l")


@override_settings(AUTH_EMAIL_OTP=True, AUTH_CAPTCHA=True,
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OtpCaptchaTests(TestCase):
    """AI-C-004: sign-up needs the emailed code before anything is shown.
    AI-C-008: sign-in needs the picture code and then the emailed code."""

    PASSWORD = "Bc99HntU5wKAWgf_"

    def _signup(self, email="otp.user@example.com"):
        return self.client.post("/api/auth/signup/", {
            "enroll_as": "individual", "first_name": "Otp", "last_name": "User", "email": email,
            "password": self.PASSWORD, "confirm_password": self.PASSWORD,
            "country": "India", "state": "Delhi", "city": "Delhi"}, content_type="application/json")

    def _code(self):
        import re
        from django.core import mail

        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def test_signup_needs_code(self):
        resp = self._signup()
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.json()["otp_required"])
        self.assertNotIn("access_token", resp.json())
        user = AIDLUser.objects.get(email="otp.user@example.com")
        self.assertFalse(user.is_active)
        bad = self.client.post("/api/auth/signup/verify/", {"otp_token": resp.json()["otp_token"], "code": "000000"},
                               content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post("/api/auth/signup/verify/", {"otp_token": resp.json()["otp_token"], "code": self._code()},
                              content_type="application/json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertIn("access_token", ok.json())
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_unconfirmed_signup_does_not_block_email(self):
        self._signup()
        self.assertEqual(self._signup().status_code, 202)
        self.assertEqual(AIDLUser.objects.filter(email="otp.user@example.com").count(), 1)

    def test_signin_needs_captcha_then_code(self):
        resp = self._signup()
        self.client.post("/api/auth/signup/verify/", {"otp_token": resp.json()["otp_token"], "code": self._code()},
                         content_type="application/json")
        body = {"enroll_as": "individual", "email": "otp.user@example.com", "password": self.PASSWORD}
        no_captcha = self.client.post("/api/auth/signin/", body, content_type="application/json")
        self.assertIn("captcha", no_captcha.json())
        with patch("api.auth_verification.secrets.choice", return_value="A"):
            token = self.client.get("/api/auth/captcha/").json()["captcha_token"]
        wrong = self.client.post("/api/auth/signin/", {**body, "captcha_token": token, "captcha_answer": "BBBBB"},
                                 content_type="application/json")
        self.assertIn("captcha", wrong.json())
        with patch("api.auth_verification.secrets.choice", return_value="A"):
            token = self.client.get("/api/auth/captcha/").json()["captcha_token"]
        step1 = self.client.post("/api/auth/signin/", {**body, "captcha_token": token, "captcha_answer": "aaaaa"},
                                 content_type="application/json")
        self.assertEqual(step1.status_code, 200, step1.content)
        self.assertNotIn("access_token", step1.json())
        step2 = self.client.post("/api/auth/signin/verify/", {"otp_token": step1.json()["otp_token"], "code": self._code()},
                                 content_type="application/json")
        self.assertEqual(step2.status_code, 200, step2.content)
        self.assertIn("access_token", step2.json())

    def test_email_failure_is_a_clear_error(self):
        with patch("api.auth_verification.send_mail", side_effect=OSError("smtp down")):
            resp = self._signup()
        self.assertEqual(resp.status_code, 503)
        self.assertIn("couldn't send the code", resp.json()["detail"])

    def test_register_without_password_issues_learner_licence(self):
        resp = self.client.post("/api/auth/signup/", {
            "enroll_as": "individual", "first_name": "No", "last_name": "Password", "email": "nopw@example.com",
            "country": "India", "state": "Delhi", "city": "Delhi"}, content_type="application/json")
        self.assertEqual(resp.status_code, 202, resp.content)
        ok = self.client.post("/api/auth/signup/verify/", {"otp_token": resp.json()["otp_token"], "code": self._code()},
                              content_type="application/json")
        user = ok.json()["user"]
        self.assertTrue(user["licence_issued"])
        self.assertTrue(user["licence_number"].startswith("AIDL-L-"))
        from datetime import datetime
        issued = datetime.fromisoformat(user["licence_issued_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(user["licence_expires_at"].replace("Z", "+00:00"))
        self.assertEqual((expires - issued).days, 365)
        self.assertEqual(AIDLUser.objects.get(email="nopw@example.com").password_hash, "")

    def test_signin_without_password_uses_code(self):
        resp = self.client.post("/api/auth/signup/", {
            "enroll_as": "individual", "first_name": "No", "last_name": "Password", "email": "nopw@example.com",
            "country": "India", "state": "Delhi", "city": "Delhi"}, content_type="application/json")
        self.client.post("/api/auth/signup/verify/", {"otp_token": resp.json()["otp_token"], "code": self._code()},
                         content_type="application/json")
        with patch("api.auth_verification.secrets.choice", return_value="A"):
            token = self.client.get("/api/auth/captcha/").json()["captcha_token"]
        step1 = self.client.post("/api/auth/signin/", {"enroll_as": "individual", "email": "nopw@example.com",
                                                       "captcha_token": token, "captcha_answer": "AAAAA"},
                                 content_type="application/json")
        self.assertTrue(step1.json().get("otp_required"), step1.content)
        step2 = self.client.post("/api/auth/signin/verify/", {"otp_token": step1.json()["otp_token"], "code": self._code()},
                                 content_type="application/json")
        self.assertIn("access_token", step2.json())

    def test_code_locks_after_five_wrong_tries(self):
        token = self._signup().json()["otp_token"]
        for _ in range(5):
            self.client.post("/api/auth/signup/verify/", {"otp_token": token, "code": "111111"}, content_type="application/json")
        last = self.client.post("/api/auth/signup/verify/", {"otp_token": token, "code": self._code()},
                                content_type="application/json")
        self.assertIn("Too many", last.json()["code"][0])


@override_settings(EMAIL_BACKEND="api.sendgrid_backend.SendGridBackend", SENDGRID_API_KEY="SG.test",
                   DEFAULT_FROM_EMAIL="AIDL <info@grcmentor.ai>")
class SendGridBackendTests(TestCase):
    def test_code_email_goes_through_sendgrid_api(self):
        from django.core.mail import send_mail

        with patch("api.sendgrid_backend.requests.post") as post:
            post.return_value.status_code = 202
            send_mail("Your AIDL code: 123456", "code 123456", None, ["someone@example.com"],
                      html_message="<b>123456</b>")
        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "https://api.sendgrid.com/v3/mail/send")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer SG.test")
        self.assertEqual(kwargs["json"]["from"], {"email": "info@grcmentor.ai", "name": "AIDL"})
        self.assertEqual([c["type"] for c in kwargs["json"]["content"]], ["text/plain", "text/html"])

    def test_sendgrid_error_raises(self):
        from django.core.mail import send_mail

        with patch("api.sendgrid_backend.requests.post") as post:
            post.return_value.status_code = 403
            post.return_value.text = "forbidden"
            with self.assertRaises(RuntimeError):
                send_mail("s", "b", None, ["someone@example.com"])


class SetPlanCommandTests(TestCase):
    def test_basic_plan_with_more_users_and_admins(self):
        from io import StringIO

        from django.core.management import call_command

        from .plans import usage

        org = make_org(slack_team_id="T9", plan="trial")
        call_command("set_plan", "--team", "T9", "--plan", "basic", "--seats", "500", "--admins", "50", stdout=StringIO())
        org.refresh_from_db()
        self.assertEqual((org.plan, org.seats_purchased, org.admin_seat_limit), ("basic", 500, 50))
        self.assertEqual(usage(org)["limit"], 500)


class PlanUpgradeTests(TestCase):
    """Trial → Basic → Enterprise: topic-picked cards, schedules and the
    website upgrade (billing.py)."""

    def setUp(self):
        self.org = make_org(slack_team_id="T9", plan="trial")
        self.owner = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_access_token(self.owner)}")

    def test_cards_per_plan(self):
        from .plans import cards_for_plan, package_cards

        self.assertEqual(len(package_cards(self.org)), 12)                       # Trial: Default
        basic = cards_for_plan(self.org, "basic", {"basic": ["C13", "C14"]})
        self.assertEqual(len(basic), 104)                                         # 2 a week × 52 weeks
        self.assertEqual({c["code"] for c in basic[12:]}, {"C13", "C14"})        # the rest from picked topics
        small = cards_for_plan(self.org, "basic", {"basic": ["C11"]})             # C11 has only 7 cards
        self.assertEqual(len(small), 104)                                         # topped up from all topics
        self.assertEqual(len({c["no"] for c in small}), 104)                      # no duplicates
        ent = cards_for_plan(self.org, "enterprise", {"enterprise": ["C12"]})    # picks are ignored
        self.assertEqual(len(ent), 208)                                           # 4 a week × 52 weeks
        self.assertEqual(len({c["code"] for c in ent[12:]}), 15)                  # every topic
        self.assertEqual(len({c["no"] for c in ent}), 208)
        no_picks = cards_for_plan(self.org, "basic")                              # manager's Basic 24 first
        self.assertEqual(len(no_picks), 104)
        from .plans import _packages

        manager = {c["no"] for c in _packages()["default"] + _packages()["basic"]}
        self.assertEqual({c["no"] for c in no_picks[:len(manager)]}, manager)

    def test_schedules(self):
        import datetime as dt

        from .package_delivery import post_times
        from .plans import schedule_of

        start = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)        # a Wednesday
        monthly = post_times(start, 3, schedule_of(self.org))
        self.assertEqual([t.strftime("%a %d %b") for t in monthly], ["Mon 02 Nov", "Mon 07 Dec", "Mon 04 Jan"])
        self.org.plan = "enterprise"
        self.org.billing = json.dumps({"schedule": {"days": [1, 2, 3, 4], "hour": 9}})
        weekly = post_times(start, 4, schedule_of(self.org))
        self.assertEqual([t.strftime("%a %H") for t in weekly], ["Thu 09", "Fri 09", "Tue 09", "Wed 09"])

    def test_plans_page_and_preview(self):
        data = self.client.get("/api/billing/plans/").json()
        self.assertEqual(data["current"], "trial")
        self.assertTrue(data["can_upgrade"])
        self.assertEqual([p["key"] for p in data["plans"]], ["trial", "basic", "enterprise"])
        self.assertEqual(len(data["categories"]), 15)
        self.assertEqual([p["cards"] for p in data["plans"]], [12, 104, 208])
        prev = self.client.post("/api/billing/preview/", {"plan": "basic", "topics": ["C10"]}, format="json").json()
        self.assertEqual((len(prev["cards"]), prev["total"]), (12, 92))
        self.assertNotIn("message", prev["cards"][0])                             # card text stays on the server

    @override_settings(BILLING_TEST_MODE=True, BILLING_PROVIDER="")
    def test_test_mode_upgrade_activates(self):
        from .plans import package_cards

        resp = self.client.post("/api/billing/checkout/", {"plan": "basic", "topics": ["C10", "C13"], "seats": 50},
                                format="json")
        self.assertEqual(resp.json()["status"], "activated", resp.content)
        self.org.refresh_from_db()
        self.assertEqual((self.org.plan, self.org.seats_purchased), ("basic", 50))
        self.assertEqual({c["code"] for c in package_cards(self.org)[12:]}, {"C10", "C13"})
        bad = self.client.post("/api/billing/checkout/", {"plan": "enterprise",
                                                          "schedule": {"days": [0, 1], "hour": 10}}, format="json")
        self.assertIn("schedule", bad.json())
        self.assertNotIn("topics", bad.json())                                    # Enterprise covers all topics
        ok = self.client.post("/api/billing/checkout/", {"plan": "enterprise",
                                                         "schedule": {"days": [0, 1, 3, 4], "hour": 10}}, format="json")
        self.assertEqual(ok.json()["status"], "activated", ok.content)
        self.org.refresh_from_db()
        self.assertEqual(len(json.loads(self.org.billing)["topics"]["enterprise"]), 15)

    @override_settings(BILLING_TEST_MODE=False, BILLING_PROVIDER="")
    def test_without_payment_provider_request_is_pending(self):
        resp = self.client.post("/api/billing/checkout/", {"plan": "basic", "topics": ["C10"]}, format="json")
        self.assertEqual(resp.json()["status"], "pending")
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, "trial")
        self.assertEqual(json.loads(self.org.billing)["pending"]["plan"], "basic")

    def test_only_owner_can_check_out(self):
        other = make_user(self.org, microsoft_id="slack:T9:U2")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_access_token(other)}")
        self.assertEqual(client.post("/api/billing/checkout/", {"plan": "basic", "topics": ["C10"]},
                                     format="json").status_code, 403)

    def test_admin_center_upgrade_button(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        home = json.dumps(admin_card_blocks(build_admin_cards_context(self.owner), "home"), ensure_ascii=False)
        self.assertIn("aidl_upgrade", home)
        self.assertIn("Trial plan", home)
        self.assertIn("1 card a month", home)


@override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_test", BILLING_PROVIDER="stripe",
                   STRIPE_PRICES={"basic": "price_basic", "enterprise": "price_ent"}, BILLING_TEST_MODE=False,
                   BILLING_UNLIMITED_ADMIN_EMAILS={"free@aidl.example"})
class StripeBillingTests(TestCase):
    def setUp(self):
        self.org = make_org(slack_team_id="T9", plan="trial")
        self.owner = make_user(self.org, microsoft_id="slack:T9:U1", email="owner@acme.example")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_access_token(self.owner)}")

    def _webhook(self, event):
        import hashlib, hmac, time

        body = json.dumps(event).encode()
        ts = str(int(time.time()))
        sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return self.client.post("/api/billing/stripe/webhook/", body, content_type="application/json",
                                HTTP_STRIPE_SIGNATURE=f"t={ts},v1={sig}")

    def test_checkout_creates_stripe_session(self):
        with patch("api.stripe_billing.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.content = b"{}"
            post.return_value.json.return_value = {"url": "https://checkout.stripe.com/c/pay/cs_test_1"}
            resp = self.client.post("/api/billing/checkout/", {"plan": "basic", "topics": ["C10"], "seats": 20}, format="json")
        self.assertEqual(resp.json(), {"status": "redirect", "url": "https://checkout.stripe.com/c/pay/cs_test_1"})
        data = post.call_args.kwargs["data"]
        self.assertEqual((data["mode"], data["line_items[0][price]"], data["line_items[0][quantity]"]),
                         ("subscription", "price_basic", "20"))
        self.assertEqual((data["metadata[app]"], data["metadata[plan]"], data["metadata[topics]"]), ("aidl", "basic", "C10"))
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, "trial")                       # nothing changes until Stripe confirms

    def test_webhook_activates_paid_upgrade_once(self):
        event = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
            "payment_status": "paid", "customer": "cus_1", "subscription": "sub_1",
            "metadata": {"app": "aidl", "org_id": str(self.org.pk), "plan": "enterprise", "topics": "C12,C13",
                         "days": "0,1,2,3", "hour": "9", "seats": "40", "by": "owner@acme.example"}}}}
        self.assertEqual(self._webhook(event).status_code, 200)
        self.org.refresh_from_db()
        self.assertEqual((self.org.plan, self.org.seats_purchased), ("enterprise", 40))
        from .plans import schedule_of

        self.assertEqual(schedule_of(self.org), {"days": (0, 1, 2, 3), "hour": 9})
        self._webhook(event)                                            # Stripe retry → no second activation
        self.assertEqual(len(json.loads(self.org.billing)["history"]), 1)
        cancel = {"id": "evt_2", "type": "customer.subscription.deleted",
                  "data": {"object": {"id": "sub_1", "metadata": {"app": "aidl"}}}}
        self._webhook(cancel)
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, "trial")

    def test_webhook_rejects_bad_signature_and_other_products(self):
        bad = self.client.post("/api/billing/stripe/webhook/", b"{}", content_type="application/json",
                               HTTP_STRIPE_SIGNATURE="t=1,v1=nope")
        self.assertEqual(bad.status_code, 400)
        other = {"id": "evt_3", "type": "checkout.session.completed",
                 "data": {"object": {"payment_status": "paid", "client_reference_id": "vaptfix-123", "metadata": {}}}}
        self.assertEqual(self._webhook(other).status_code, 200)       # VAPTFIX checkout on the shared account
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, "trial")

    def test_unlimited_admin_upgrades_without_payment(self):
        self.owner.email = "free@aidl.example"
        self.owner.save()
        with patch("api.stripe_billing.requests.post") as post:
            resp = self.client.post("/api/billing/checkout/", {"plan": "basic", "topics": ["C10"]}, format="json")
        self.assertEqual(resp.json()["status"], "activated")
        post.assert_not_called()
        self.org.refresh_from_db()
        self.assertEqual(self.org.plan, "basic")



class EmployeeFlowTests(TestCase):
    """The design's card flow: Welcome → AUP (14 points) → Traffic Light →
    Highway Code → licence card with the licence image."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake
    _event = PlanLimitTests._event

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(name="SpinifexIT", slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"))
        self.owner = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []
        _answer_policy(self.org)
        _approve_all(self.org)
        from .admin_setup import update

        update(self.org, flow_started="2026-10-14")
        self._event("U5")
        self.member = AIDLUser.objects.get(microsoft_id="slack:T9:U5")

    def _press(self, action_id, value="x"):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": "U5", "team_id": "T9"},
                   "container": {"channel_id": "D5", "message_ts": "1.1"},
                   "actions": [{"action_id": action_id, "value": value}]}
        self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                     "application/x-www-form-urlencoded")

    def _posted(self):
        return [b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "U5"]

    def test_full_flow_to_licence(self):
        self.calls.clear()
        self._press("aidl_flow_start", "start")
        card = self._posted()[-1]
        aup = json.dumps(card)                                                   # the AUP first, as image cards
        self.assertIn("Card 1 of 3", aup)
        self.assertIn("/api/cards/aup-1/", aup)
        self.assertEqual(card["attachments"][0]["color"], "#14140F")               # design's coloured bar
        for n in range(1, 15):
            self._press("aidl_flow_aup", f"aup_{n}")
        update = [b for m, b in self.calls if m == "chat.update"][-1]
        self.assertIn("AGREED", json.dumps(update))
        tl = json.dumps(self._posted()[-1])                                      # then the Traffic Light
        self.assertIn("Card 2 of 3", tl)
        self.assertIn("Traffic Light Check", tl)
        self._press("aidl_flow_tl", "next")
        self.assertIn("Card 3 of 3", json.dumps(self._posted()[-1]))             # then the Highway Code
        self._press("aidl_flow_hc", "next")
        self.member.refresh_from_db()
        self.assertTrue(self.member.licence_issued)
        self.assertEqual((self.member.licence_expires_at - self.member.licence_issued_at).days, 365)
        ask = json.dumps(self._posted()[-1])                                      # no avatar yet → pick one
        self.assertIn("/api/avatar/start/?u=", ask)
        self.assertIn("aidl_flow_avatar", ask)

        import base64
        import io
        from urllib.parse import parse_qs, urlparse

        from PIL import Image

        from .avatar import start_url

        start = urlparse(start_url(self.member))
        resp = self.client.get(f"{start.path}?{start.query}")
        token = parse_qs(urlparse(resp["Location"]).query)["token"][0]           # → website avatar builder
        self.assertEqual(self.client.get(f"/api/avatar/?token={token}").json()["has_avatar"], False)
        buf = io.BytesIO()
        Image.new("RGB", (200, 220), "#BFE3F0").save(buf, "PNG")
        png = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            saved = self.client.post("/api/avatar/", json.dumps({"token": token, "png": png, "config": {"gender": "girl"}}),
                                     content_type="application/json")
        self.assertTrue(saved.json().get("ok"), saved.content)
        licence = self._posted()[-1]                                              # licence card, with the avatar
        image = next(b for b in licence["attachments"][0]["blocks"] if b["type"] == "image")
        self.assertIn(f"/api/licenses/{self.member.licence_number}.png?s=", image["image_url"])
        self.assertIn("&v=", image["image_url"])
        self.assertIn("valid till", json.dumps(licence))
        self.assertEqual(self.client.post("/api/avatar/", json.dumps({"token": "bad", "png": png}), content_type="application/json").status_code, 401)

    def test_avatar_is_always_created_never_the_slack_photo(self):
        from . import employee_flow, licence_image

        self.member.licence_issued, self.member.licence_number = True, "AIDL-L-1111-2222"
        self.member.avatar_url = "https://img.example/slack.png"
        self.member.save()
        from .licensing import acknowledge

        _accept_aup(self.member, self.org)                                       # finished every card
        acknowledge(self.member, "traffic_light")
        acknowledge(self.member, "highway_code")
        card = json.dumps(employee_flow.licence(self.member, self.org))
        self.assertIn("/api/avatar/start/?u=", card)                             # asked to create an avatar
        self.assertNotIn("Slack photo", card)
        self.assertIn("Create my avatar", json.dumps(employee_flow.welcome(self.member, self.org)))
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            employee_flow.advance(self.org, self.member, "aidl_flow_avatar", "slack", "D5", "1.1")  # old button
        self.member.refresh_from_db()
        self.assertFalse(self.member.avatar_data)                                # nothing taken from Slack
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            employee_flow.remind_everyone(self.org)                              # licensed, no avatar → asked
        self.assertIn("/api/avatar/start/?u=", json.dumps(self._posted()[-1]))
        gone = licence_image.image_url("AIDL-L-0000-0000").split("onrender.com", 1)[-1]
        resp = self.client.get(gone)                                              # revoked / reset licence
        self.assertEqual((resp.status_code, resp.content[1:4]), (200, b"PNG"))

    def test_points_must_be_in_order(self):
        from . import aup_store

        self._press("aidl_flow_aup", "aup_5")
        self.assertEqual(aup_store.points_accepted(self.member, self.org), set())

    def test_licence_image_endpoint(self):
        import base64

        from .licence_image import image_url, save_logo
        from PIL import Image
        import io

        buf = io.BytesIO()
        Image.new("RGB", (40, 40), "#336699").save(buf, "PNG")
        self.org.logo_data = save_logo(buf.getvalue())
        self.org.save()
        self.member.licence_issued = True
        self.member.licence_number = "AIDL-L-1234-5678"
        self.member.save()
        path = image_url("AIDL-L-1234-5678").split("onrender.com", 1)[-1]
        resp = self.client.get(path)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp.content[1:4], b"PNG")
        self.assertEqual(self.client.get("/api/licenses/AIDL-L-1234-5678.png?s=wrong").status_code, 404)
        dl = self.client.get(path + "&download=1")
        self.assertIn("attachment", dl["Content-Disposition"])

    def test_card_images_with_logo_on_paid_plans(self):
        import io

        from PIL import Image

        from . import card_image, employee_flow
        from .licence_image import brand_logo, save_logo

        buf = io.BytesIO()
        Image.new("RGB", (40, 40), "#336699").save(buf, "PNG")
        self.org.logo_data = save_logo(buf.getvalue())
        self.org.plan = "trial"
        self.org.save()
        self.assertIsNone(brand_logo(self.org))                                   # Trial: no company logo
        trial_url = card_image.image_url("highway_code", self.org)
        self.assertIn("-aidl", trial_url)                                         # no company logo in the link
        self.org.plan = "basic"
        self.org.save()
        self.assertIsNotNone(brand_logo(self.org))
        for kind in card_image.KINDS:
            path = card_image.image_url(kind, self.org).split("onrender.com", 1)[-1]
            resp = self.client.get(path)
            self.assertEqual((resp.status_code, resp.content[1:4]), (200, b"PNG"), kind)
        self.assertNotEqual(card_image.image_url("highway_code", self.org), trial_url)  # Slack refetches
        bad = f"/api/cards/highway_code/{self.org.pk}.png?s=wrong"
        self.assertEqual(self.client.get(bad).status_code, 404)
        for build, kind in ((employee_flow.traffic, "traffic_light"), (employee_flow.highway, "highway_code")):
            blocks = build(self.member, self.org)["attachments"][0]["blocks"]
            image = next(b for b in blocks if b["type"] == "image")
            self.assertIn(f"/api/cards/{kind}/", image["image_url"])
            self.assertTrue(image["alt_text"])                                    # screen readers get the text

    def test_logo_upload_saved(self):
        import io

        from PIL import Image
        from urllib.parse import urlencode

        buf = io.BytesIO()
        Image.new("RGB", (800, 600), "#AA3300").save(buf, "PNG")
        view = {"callback_id": "aidl_logo", "private_metadata": "{}", "state": {"values": {"logo": {"v": {
            "type": "file_input", "files": [{"id": "F1", "url_private_download": "https://files.slack.com/F1/logo.png"}]}}}}}
        payload = {"type": "view_submission", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"}, "view": view}
        with patch("requests.get") as get:
            get.return_value.content = buf.getvalue()
            get.return_value.raise_for_status = lambda: None
            self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                         "application/x-www-form-urlencoded")
        self.org.refresh_from_db()
        self.assertTrue(self.org.logo_data)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer xoxb-t")

    def test_admin_center_logo_button(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        def home():
            return json.dumps(admin_card_blocks(build_admin_cards_context(self.owner), "home"), ensure_ascii=False)

        self.assertNotIn("aidl_logo_open", home())                                # Trial: no company logo
        self.assertIn("available on the Basic and Enterprise plans", home())
        self.org.plan = "basic"
        self.org.save()
        self.assertIn("aidl_logo_open", home())


class AdminPermissionTests(TestCase):
    """Master admin (can't be removed / changed) + delegated permissions:
    Manage admins and Manage plan & billing."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"), admin_seat_limit=10)
        self.master = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Master")
        self.manager = make_user(self.org, microsoft_id="slack:T9:U2", full_name="Mo Manager",
                                 perm_manage_admins=True, perm_billing=False)
        self.other = make_user(self.org, microsoft_id="slack:T9:U3", full_name="Ola Other", perm_manage_admins=False)
        self.calls = []

    def _post(self, user, payload):
        from urllib.parse import urlencode

        payload = {"team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"}, **payload}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def test_admin_list_marks_master_and_offers_edit(self):
        from .slack_cards import build_admin_cards_context
        from .slack_modals import add_admin_view

        view = json.dumps(add_admin_view(build_admin_cards_context(self.manager), {}), ensure_ascii=False)
        self.assertIn("👑 Master admin", view)
        self.assertNotIn(f'"value": "{self.master.pk}"', view)              # no Edit / Remove for the master
        self.assertIn("aidl_admin_edit", view)
        self.assertNotIn('"value": "billing"', view)                       # manager can't grant billing

    def test_manager_edits_permissions_within_own_rights(self):
        view = {"callback_id": "aidl_admin_perms", "private_metadata": json.dumps({"user_id": str(self.other.pk)}),
                "state": {"values": {"perms": {"v": {"type": "checkboxes", "selected_options": [
                    {"value": "manage_admins"}, {"value": "billing"}]}}}}}
        self._post("U2", {"type": "view_submission", "view": view})
        self.other.refresh_from_db()
        self.assertTrue(self.other.perm_manage_admins)
        self.assertFalse(self.other.perm_billing)                          # manager doesn't have billing
        self.assertTrue(any(m == "chat.postMessage" and b["channel"] == "U1" for m, b in self.calls))  # master told

    def test_master_cannot_be_edited_or_removed(self):
        from .admin_people import remove_admin

        view = {"callback_id": "aidl_admin_perms", "private_metadata": json.dumps({"user_id": str(self.master.pk)}),
                "state": {"values": {}}}
        resp = self._post("U2", {"type": "view_submission", "view": view}).json()
        self.assertEqual(resp["response_action"], "errors")
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            ok, msg = remove_admin(self.org, self.manager, str(self.master.pk))
        self.assertFalse(ok)
        self.assertIn("master admin can't be removed", msg)

    def test_manager_removes_admin_but_plain_admin_cannot(self):
        from .admin_people import remove_admin

        with patch("api.slack_client.slack_api", side_effect=self._fake):
            self.assertFalse(remove_admin(self.org, self.other, str(self.manager.pk))[0])
            self.assertTrue(remove_admin(self.org, self.manager, str(self.other.pk))[0])

    def test_billing_permission_controls_upgrade(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_access_token(self.manager)}")
        self.assertEqual(client.post("/api/billing/checkout/", {"plan": "basic", "topics": ["C10"]},
                                     format="json").status_code, 403)
        self.manager.perm_billing = True
        self.manager.save()
        with self.settings(BILLING_TEST_MODE=True, BILLING_PROVIDER=""):
            resp = client.post("/api/billing/checkout/", {"plan": "basic", "topics": ["C10"]}, format="json")
        self.assertEqual(resp.json()["status"], "activated")


class CardApprovalTests(TestCase):
    """Admins approve every card before the team gets it (card_approval.py);
    waiting cards are postponed, not dropped."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake
    _event = PlanLimitTests._event

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(name="SpinifexIT", slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"), plan="trial")
        self.master = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Master")
        self.calls = []
        _answer_policy(self.org)

    def _post(self, user, payload):
        from urllib.parse import urlencode

        payload = {"team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"}, "trigger_id": "TR", **payload}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _review(self, action_id, value, view=None, overflow=False):
        action = {"action_id": action_id}
        action.update({"selected_option": {"value": value}} if overflow else {"value": value})
        payload = {"type": "block_actions", "actions": [action]}
        if view is not None:
            payload["view"] = view
        return self._post("U1", payload)

    def test_onboarding_must_be_approved_before_start(self):
        from . import card_approval
        from .models import CardApproval

        self.assertFalse(card_approval.onboarding_ready(self.org))
        for item in card_approval.ONBOARDING:
            self._review("aidl_crv_approve_onb", item)
        self.assertTrue(card_approval.onboarding_ready(self.org))
        row = CardApproval.objects.get(organization_id=str(self.org.pk), card_key="traffic_light")
        self.assertEqual(row.decided_by_name, "Priya Master")                    # audit trail
        _answer_policy(self.org, **{"2.1": "2.1.c"})                              # a new AUP version
        self.assertFalse(card_approval.is_approved(self.org, "aup"))             # needs approving again

    def test_review_popup_months_approve_skip_swap(self):
        from . import card_approval
        from .card_review_slack import review_view

        self.org.plan = "basic"
        self.org.save()
        view = review_view(self.org, 1)
        text = json.dumps(view, ensure_ascii=False)
        for label in ("Onboarding cards", "Month 1 of 12", "Month 2 →", "aidl_crv_card", "Extra reference cards"):
            self.assertIn(label, text)
        live = {"id": "V1", "callback_id": "aidl_cards_review", "private_metadata": json.dumps({"month": 1})}
        self._review("aidl_crv_approve_month", "1", view=live)
        self.assertEqual(card_approval.counts(self.org)["approved"], card_approval.per_month(self.org))
        self.assertTrue(any(m == "views.update" and b.get("view_id") == "V1" for m, b in self.calls))
        second = card_approval.month_cards(self.org, 2)
        self._review("aidl_crv_card", f"skip|{second[0]['no']}", view=live, overflow=True)
        self._review("aidl_crv_card", f"swap|{second[1]['no']}", view=live, overflow=True)
        seq = {c["position"]: c for c in card_approval.sequence(self.org)}
        self.assertEqual(seq[second[0]["position"]]["approval"], "skipped")
        swapped = seq[second[1]["position"]]
        self.assertEqual(swapped["swapped_from"], second[1]["no"])               # replaced in the same slot
        self.assertEqual(swapped["category"], second[1]["category"])             # same topic
        self.assertEqual(swapped["approval"], "waiting")                         # the new card needs approving

    def test_waiting_cards_are_postponed_not_sent(self):
        from . import card_approval
        from .models import PackageDelivery
        from .package_delivery import sync_package

        member = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U5", full_name="Jordan",
                           licence_issued=True, licence_number="AIDL-L-1-2")
        seq = card_approval.sequence(self.org)
        card_approval.approve(self.org, [seq[0]["no"], seq[2]["no"]], self.master)   # card 2 not approved
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            sync_package(self.org, member)
        nos = list(PackageDelivery.objects.filter(user_id=str(member.pk)).order_by("post_at")
                   .values_list("card_no", flat=True))
        self.assertEqual(nos, [seq[0]["no"], seq[2]["no"]])                      # card 2 waits, card 3 takes its slot
        last = PackageDelivery.objects.get(user_id=str(member.pk), card_no=seq[2]["no"]).post_at
        card_approval.approve(self.org, [seq[1]["no"]], self.master)              # approved later →
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            sync_package(self.org, member)
        late = PackageDelivery.objects.get(user_id=str(member.pk), card_no=seq[1]["no"])
        self.assertGreater(late.post_at, last)                                   # … follows the last scheduled card
        sent = [b for m, b in self.calls if m in ("chat.postMessage", "chat.scheduleMessage")]
        self.assertIn("/api/cards/pkg-", json.dumps(sent))                       # awareness cards are images
        self.assertIn("aidl_app_request_open", json.dumps(sent))                 # … with Request an app

    def test_employee_waits_for_unapproved_card_then_gets_it(self):
        from . import employee_flow
        from .admin_setup import update

        update(self.org, flow_started="2026-10-14")
        self._event("U5")
        member = AIDLUser.objects.get(microsoft_id="slack:T9:U5")
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            employee_flow.advance(self.org, member, "aidl_flow_start", "start", "D5", "1.1")
        self.assertIn("waiting for approval", json.dumps(self.calls[-1]))
        self.calls.clear()
        self._review("aidl_crv_approve_onb", "aup")
        posted = [b for m, b in self.calls if m == "chat.postMessage" and b.get("channel") == "U5"]
        self.assertIn("/api/cards/aup-1/", json.dumps(posted))                   # sent once approved

    def test_card_images_render(self):
        from . import card_image
        from .plans import package_cards

        for kind in ("aup-1", "aup-14", f"pkg-{package_cards(self.org)[0]['no']}"):
            path = card_image.image_url(kind, self.org).split("onrender.com", 1)[-1]
            resp = self.client.get(path)
            self.assertEqual((resp.status_code, resp.content[1:4]), (200, b"PNG"), kind)
        self.assertEqual(self.client.get(f"/api/cards/pkg-zzz/{self.org.pk}.png?s=x").status_code, 404)
        from . import image_cache
        from .models import CardImage

        self.assertTrue(CardImage.objects.filter(key__contains=":aup-1:").exists())  # kept in the database
        image_cache._MEMORY.clear()
        path = card_image.image_url("aup-1", self.org).split("onrender.com", 1)[-1]
        with patch("api.card_image.render", side_effect=AssertionError("drawn again")):
            resp = self.client.get(path)                                         # served without drawing
        self.assertEqual((resp.status_code, resp.content[1:4]), (200, b"PNG"))
        self.assertIn("immutable", resp["Cache-Control"])

    def test_needs_permission_and_reminds_approvers(self):
        from datetime import timedelta

        from django.utils import timezone

        from . import card_approval

        helper = make_user(self.org, microsoft_id="slack:T9:U2", full_name="Mo Helper", perm_approve_cards=False)
        self._post("U2", {"type": "block_actions", "actions": [{"action_id": "aidl_crv_approve_all", "value": "1"}],
                          "response_url": "https://hooks.example/r"})
        self.assertEqual(card_approval.counts(self.org)["approved"], 0)          # no permission → nothing changes
        helper.perm_approve_cards = True
        helper.save()
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            told = card_approval.remind(self.org)
        self.assertEqual(told, 2)                                                # master + helper
        dms = [b for m, b in self.calls if m == "chat.postMessage" and b.get("channel") in ("U1", "U2")]
        self.assertIn("aidl_crv_approve_list", json.dumps(dms))
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            self.assertEqual(card_approval.remind(self.org), 0)                  # not again the same week
            self.assertEqual(card_approval.remind(self.org, now=timezone.now() + timedelta(days=8)), 2)


class AppRequestTests(TestCase):
    """Employees ask for an app; admins with Approve Apps decide in Slack."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"))
        self.master = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Master")
        self.emp = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U5", full_name="Jordan")
        self.emp2 = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U6", full_name="Sam")
        self.calls = []

    def _post(self, user, payload):
        from urllib.parse import urlencode

        payload = {"team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"}, "trigger_id": "TR", **payload}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _request(self, user, name="Perplexity", data="amber"):
        values = {"name": {"v": {"value": name}}, "website": {"v": {"value": "https://perplexity.ai"}},
                  "purpose": {"v": {"value": "Research summaries"}},
                  "data": {"v": {"selected_option": {"value": data}}}}
        return self._post(user, {"type": "view_submission", "view": {
            "callback_id": "aidl_app_request", "private_metadata": "{}", "state": {"values": values}}})

    def test_request_approve_flow(self):
        from .models import AppRequest, RegisteredApp

        self._post("U5", {"type": "block_actions", "actions": [{"action_id": "aidl_app_request_open", "value": "x"}]})
        self.assertTrue(any(m == "views.open" for m, _b in self.calls))           # employees can open the popup
        self.assertEqual(self._request("U5").json(), {"response_action": "clear"})
        req = AppRequest.objects.get(organization_id=str(self.org.pk))
        to_admin = [b for m, b in self.calls if m == "chat.postMessage" and b.get("channel") == "U1"]
        self.assertIn("aidl_appreq_approve", json.dumps(to_admin))               # the approver got the request
        self._request("U6", name="perplexity")                                   # same app again
        self.assertEqual(AppRequest.objects.count(), 1)
        self.assertEqual(len(json.loads(AppRequest.objects.get().requester_ids)), 2)
        self.calls.clear()
        self._post("U1", {"type": "block_actions", "actions": [{"action_id": "aidl_appreq_approve",
                                                                "value": str(req.pk)}]})
        app = RegisteredApp.objects.get(organization_id=str(self.org.pk), name="Perplexity")
        self.assertEqual((app.status, app.data_allowed), ("approved", "internal"))
        told = {b.get("channel") for m, b in self.calls if m == "chat.postMessage"}
        self.assertTrue({"U5", "U6"} <= told)                                    # both requesters told
        self.calls.clear()
        self._request("U5", name="PERPLEXITY")                                   # already approved
        self.assertEqual(AppRequest.objects.count(), 1)
        self.assertIn("already approved", json.dumps(self.calls))

    def test_reject_with_reason(self):
        from .models import AppRequest

        self._request("U5", name="SketchyAI", data="red")
        req = AppRequest.objects.get()
        self._post("U1", {"type": "view_submission", "view": {
            "callback_id": "aidl_app_reject", "private_metadata": json.dumps({"request": str(req.pk)}),
            "blocks": [{"type": "input", "block_id": "text"}],
            "state": {"values": {"text": {"v": {"value": "Stores prompts for training"}}}}}})
        req.refresh_from_db()
        self.assertEqual((req.status, req.reason), ("rejected", "Stores prompts for training"))
        self.assertIn("Stores prompts for training",
                      json.dumps([b for m, b in self.calls if m == "chat.postMessage" and b.get("channel") == "U5"]))

    def test_only_app_approvers_decide(self):
        from .models import AppRequest

        make_user(self.org, microsoft_id="slack:T9:U2", full_name="No Apps", perm_approve_apps=False)
        self._request("U5")
        req = AppRequest.objects.get()
        self._post("U2", {"type": "block_actions", "response_url": "https://hooks.example/r",
                          "actions": [{"action_id": "aidl_appreq_approve", "value": str(req.pk)}]})
        req.refresh_from_db()
        self.assertEqual(req.status, "pending")


class CardAckTests(TestCase):
    """✅ Got it · 📚 My cards · reminders · acknowledgement numbers for admins
    (design E:\\slack-cards.html)."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import PackageDelivery
        from .plans import package_cards
        from .slack_client import encrypt_token

        self.org = make_org(name="Secureitlab", slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"), plan="basic", seats_purchased=50)
        self.master = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Master")
        self.member = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U5",
                                full_name="Jordan Ellis", licence_issued=True, licence_number="AIDL-L-1-2")
        _answer_policy(self.org)
        _approve_all(self.org)
        self.calls = []
        now = timezone.now()
        self.cards = package_cards(self.org)[:12]
        self.rows = []
        for i, c in enumerate(self.cards):  # 12 cards sent, every 3 days, oldest first
            self.rows.append(PackageDelivery.objects.create(
                organization_id=str(self.org.pk), user_id=str(self.member.pk), package="basic", card_no=c["no"],
                post_order=i + 1, post_at=now - timedelta(days=3 * (12 - i)), status="sent", slack_channel_id="D7",
                message_ts=f"{100 + i}.1"))

    def _post(self, user, payload):
        from urllib.parse import urlencode

        payload = {"team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"}, "trigger_id": "TR", **payload}
        with patch("api.slack_client.slack_api", side_effect=self._fake), \
                patch("api.card_ack.slack_client.slack_api", side_effect=self._fake):
            return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                                "application/x-www-form-urlencoded")

    def test_card_has_got_it_my_cards_and_request(self):
        from .package_delivery import card_blocks

        text = json.dumps(card_blocks(self.org, self.cards[0], 1, 104))
        for part in ("aidl_card_ack", "aidl_my_cards", "aidl_app_request_open", f"card|{self.cards[0]['no']}",
                     self.cards[0]["hook"]):
            self.assertIn(part, text)

    def test_got_it_updates_the_same_message_and_counts(self):
        row = self.rows[-1]
        self._post("U5", {"type": "block_actions", "container": {"channel_id": "D7", "message_ts": row.message_ts},
                          "actions": [{"action_id": "aidl_card_ack", "block_id": "aidl_card_footer",
                                       "value": row.card_no}]})
        row.refresh_from_db()
        self.assertIsNotNone(row.acked_at)
        upd = next(b for m, b in self.calls if m == "chat.update" and b.get("ts") == row.message_ts)
        self.assertIn("Acknowledged", json.dumps(upd))
        self.assertNotIn("aidl_card_ack", json.dumps(upd))                       # Got it is gone
        counter = [b for m, b in self.calls if m == "chat.postMessage" and b.get("channel") == "U1"]
        self.assertIn("Today: 1 acknowledged", json.dumps(counter))              # master's live message

    def test_my_cards_search_filters_views_and_paging(self):
        from . import card_ack

        view = card_ack.my_cards_view(self.org, self.member)
        text = json.dumps(view, ensure_ascii=False)
        for part in ("Search your 12 cards", "mc_status", "mc_topic", "mc_month", "mc_latest", "1 of 12",
                     "12 cards still need your acknowledgement"):
            self.assertIn(part, text)
        m = json.loads(view["private_metadata"])
        m = card_ack.my_cards_action(m, {"action_id": "mc_view_list", "value": "list"})
        listed = json.dumps(card_ack.my_cards_view(self.org, self.member, m), ensure_ascii=False)
        self.assertIn("Older →", listed)                                         # 10 a page
        m = card_ack.my_cards_action(m, {"action_id": "mc_latest", "selected_option": {"value": "2"}})
        self.assertIn("Showing *2* of 12", json.dumps(card_ack.my_cards_view(self.org, self.member, m)))
        word = self.cards[3]["title"].split()[0].lower()
        m = card_ack.my_cards_action(card_ack._blank_state(), {"action_id": "mc_search", "value": word})
        found = card_ack._filtered(card_ack.received(self.member), m)
        self.assertTrue(any(r.card_no == self.cards[3]["no"] for r in found))
        opened = card_ack.my_cards_view(self.org, self.member, open_no=self.cards[0]["no"])
        self.assertIn(f"/api/cards/pkg-{self.cards[0]['no']}/", json.dumps(opened))   # opens on that card

    def test_my_cards_says_when_the_next_card_arrives(self):
        from datetime import timedelta

        from django.utils import timezone

        from . import card_ack
        from .models import PackageDelivery
        from .plans import package_cards

        self.cards_all = package_cards(self.org)
        text = json.dumps(card_ack.my_cards_view(self.org, self.member), ensure_ascii=False)
        self.assertIn("Your next card is on its way", text)                     # nothing scheduled yet
        when = timezone.now() + timedelta(days=3)
        PackageDelivery.objects.create(organization_id=str(self.org.pk), user_id=str(self.member.pk), package="basic",
                                       card_no=self.cards_all[12]['no'], post_order=13, post_at=when,
                                       status="scheduled")
        text = json.dumps(card_ack.my_cards_view(self.org, self.member), ensure_ascii=False)
        self.assertIn(f"Next card:* <!date^{int(when.timestamp())}^", text)    # shown in the learner's time zone
        self.assertNotIn(card_ack.card_title(self.cards_all[12]['no']), text.split("Next card")[1][:80])

    def test_got_it_inside_my_cards_refreshes_popup(self):
        from . import card_ack

        view = card_ack.my_cards_view(self.org, self.member)
        row = self.rows[-1]
        self._post("U5", {"type": "block_actions", "view": {**view, "id": "V1"},
                          "actions": [{"action_id": "aidl_card_ack", "block_id": "mc_nav", "value": row.card_no}]})
        row.refresh_from_db()
        self.assertIsNotNone(row.acked_at)
        self.assertTrue(any(m == "views.update" and b.get("view_id") == "V1" for m, b in self.calls))
        self.assertTrue(any(m == "chat.update" and b.get("ts") == row.message_ts for m, b in self.calls))

    def test_one_reminder_after_7_days_then_overdue_and_admin_remind(self):
        from datetime import timedelta

        from django.utils import timezone

        from . import card_ack

        with patch("api.card_ack.slack_client.slack_api", side_effect=self._fake):
            sent = card_ack.send_reminders(self.org)
            self.assertEqual(card_ack.send_reminders(self.org), 0)               # only once per card
        self.assertEqual(sent, 10)                                               # cards older than 7 days
        replies = [b for m, b in self.calls if m == "chat.postMessage" and b.get("thread_ts")]
        self.assertEqual(len(replies), 10)                                       # thread replies under the cards
        self.assertIn("only reminder", json.dumps(replies[0]))
        later = timezone.now() + timedelta(days=4)
        stats = card_ack.admin_stats(self.org, now=later)
        self.assertEqual([p["name"] for p in stats["overdue"]], ["Jordan Ellis"])  # one row per person
        self.calls.clear()
        with patch("api.card_ack.slack_client.slack_api", side_effect=self._fake), \
                patch("api.card_ack.timezone.now", return_value=later):
            self.assertTrue(card_ack.remind_person(self.org, self.master, str(self.member.pk)))
        self.assertIn("Priya Master", json.dumps(self.calls))                    # "asked you to take a look"

    def test_admin_center_team_progress_and_review_show_acknowledgements(self):
        from django.utils import timezone

        from .card_review_slack import review_view
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context
        from .admin_setup import update

        for r in self.rows[:6]:
            r.acked_at = timezone.now()
            r.save()
        update(self.org, flow_started="2026-10-01")
        home = json.dumps(admin_card_blocks(build_admin_cards_context(self.master), "home"), ensure_ascii=False)
        for part in ("Cards acknowledged this month", "Last week", "Today: 6 acknowledged", "Most acknowledged",
                     "aidl_ack_alerts"):
            self.assertIn(part, home)
        team = json.dumps(__import__("api.slack_modals", fromlist=["x"]).team_progress_view(self.org), ensure_ascii=False)
        self.assertIn("6 of 12", team)
        self.assertIn("Awareness cards acknowledged", team)
        review = json.dumps(review_view(self.org, 1), ensure_ascii=False)
        self.assertIn("How topics are landing", review)
        self.assertIn("% acknowledged", review)

    def test_alerts_settings_saved_per_admin(self):
        from . import card_ack

        helper = make_user(self.org, microsoft_id="slack:T9:U2", full_name="Mo Helper")
        self.assertEqual(card_ack.prefs_for(self.org, self.master), {"live": "counter", "late": "alert"})
        self.assertEqual(card_ack.prefs_for(self.org, helper)["live"], "none")
        values = {"live": {"v": {"selected_option": {"value": "every"}}},
                  "late": {"v": {"selected_option": {"value": "weekly"}}}}
        self.assertIn("notice", card_ack.save_alerts(self.org, helper, {"state": {"values": values}}))
        self.assertEqual(card_ack.prefs_for(self.org, helper), {"live": "every", "late": "weekly"})

    def test_request_from_a_card_names_the_card(self):
        from .models import AppRequest

        no = self.cards[2]["no"]
        self._post("U5", {"type": "block_actions", "actions": [{"action_id": "aidl_app_request_open",
                                                                "value": f"card|{no}"}]})
        opened = next(b for m, b in self.calls if m in ("views.open", "views.update"))
        self.assertIn("Asked from card", json.dumps(opened))
        values = {"name": {"v": {"value": "Perplexity"}}, "purpose": {"v": {"value": "Research"}},
                  "data": {"v": {"selected_option": {"value": "green"}}}}
        self._post("U5", {"type": "view_submission", "view": {
            "callback_id": "aidl_app_request", "private_metadata": opened["view"]["private_metadata"],
            "state": {"values": values}}})
        self.assertEqual(AppRequest.objects.get().from_card, self.cards[2]["title"])


class BlockKitIdsTests(TestCase):
    """Slack refuses a view or message when two buttons in one block share an
    action_id (that kept My cards on "Loading…")."""

    def _check(self, blocks):
        for b in blocks:
            ids = [e.get("action_id") for e in b.get("elements") or [] if e.get("action_id")]
            self.assertEqual(len(ids), len(set(ids)), b)

    def test_my_cards_views_have_unique_action_ids(self):
        from datetime import timedelta

        from django.utils import timezone

        from . import card_ack
        from .models import PackageDelivery
        from .plans import package_cards

        org = make_org(plan="basic")
        member = make_user(org, role=AIDLUser.Role.LEARNER, licence_issued=True)
        for i, c in enumerate(package_cards(org)[:14]):
            PackageDelivery.objects.create(organization_id=str(org.pk), user_id=str(member.pk), package="basic",
                                           card_no=c["no"], post_order=i + 1, status="sent",
                                           post_at=timezone.now() - timedelta(days=30 - i))
        m = card_ack._blank_state()
        for view in ("card", "list"):
            m["view"] = view
            for page in (0, 1):
                m["page"] = page
                self._check(card_ack.my_cards_view(org, member, dict(m))["blocks"])

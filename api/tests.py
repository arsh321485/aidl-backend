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
from django.test import SimpleTestCase, TransactionTestCase as TestCase
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
        self.assertIn("password", resp.data)

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
        for tab in ("home", "add-admin", "add-user", "cards", "ai-apps", "it-apps"):
            self.assertContains(resp, f'data-tab="{tab}"')

    def test_invited_admin_sees_only_permitted_tabs(self):
        invited = make_user(
            self.org, perm_approve_apps=False, perm_access_cards=True, perm_create_card=False
        )
        resp = self._get("admin", invited)
        self.assertContains(resp, 'data-tab="home"')
        self.assertContains(resp, 'data-tab="cards"')
        for tab in ("add-admin", "add-user", "ai-apps", "it-apps"):
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

    def test_coverage_csv_requires_admin(self):
        self.assertEqual(self.client.get("/api/slack/cards/admin/coverage.csv").status_code, 401)
        resp = self.client.get(
            f"/api/slack/cards/admin/coverage.csv?token={create_access_token(self.primary)}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.primary.email, resp.content.decode())


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
        self.assertIn(("conversations.invite", {"channel": "C42", "users": "U1"}), calls)

        post = next(body for m, body in calls if m == "chat.postMessage")
        buttons = [b for b in post["blocks"] if b.get("block_id") == "aidl_tabs"][0]["elements"]
        labels = [b["text"]["text"] for b in buttons]
        self.assertEqual(len(labels), 6)
        self.assertFalse(any("Policy" in l for l in labels))

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
        self.assertIn("/api/slack/cards/admin/?token=", text)

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

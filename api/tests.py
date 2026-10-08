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


def _answer_policy(org: Organization) -> None:
    from .org_policy import POLICY_QUESTIONS, save_answers

    save_answers(org, {qid: options[0] for qid, options in POLICY_QUESTIONS.items()})


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
        self.assertIn(("conversations.invite", {"channel": "C42", "users": "U1"}), calls)

        post = next(body for m, body in calls if m == "chat.postMessage")
        buttons = [b for b in post["blocks"] if b.get("block_id") == "aidl_tabs"][0]["elements"]
        labels = [b["text"]["text"] for b in buttons]
        self.assertEqual(len(labels), 5)
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
        self.assertEqual(opened[0]["view"]["callback_id"], "aidl_add_admin")
        self.assertEqual(opened[0]["trigger_id"], "trig")

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
        opened = next(b for m, b in self.calls if m == "views.open")
        self.assertEqual(opened["view"]["callback_id"], "aidl_coverage")
        self.assertIn(learner.email, json.dumps(opened["view"]["blocks"]))
        self.assertNotIn(self.admin.email, json.dumps(opened["view"]["blocks"]))
        upload = next(c for c in self.http.call_args_list if c.args and c.args[0] == "https://files.slack.test/up")
        self.assertIn(b"name,email,role", upload.kwargs["data"])
        done = next(b for m, b in self.calls if m == "files.completeUploadExternal")
        self.assertEqual(done["channel_id"], "D1")
        self.assertEqual(done["files"][0]["id"], "F1")
        self.assertIn("/api/slack/cards/admin/coverage.csv?token=", json.dumps(opened["view"]["blocks"]))
        self.assertFalse([b for m, b in self.calls if m == "views.update"])  # DM copy worked, nothing to change

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
        updated = next(b for m, b in self.calls if m == "views.update")
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
        self.assertEqual(shown, ["Welcome to AIDL, Anshul!", "Start here: your AI Acceptable Use Policy"])
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
        _answer_policy(self.org)
        blocks = self._blocks()
        self.assertEqual(blocks[0].get("block_id"), "aidl_tabs")              # navigation on top
        self.assertEqual(blocks[1]["text"]["text"], "🚦 AIDL Admin Center")
        text = json.dumps(blocks, ensure_ascii=False)
        for label in ("Signed in as *Priya*", "Welcome back, Priya.", "LICENSES & SEATS", "Trial plan",
                      "Licenses issued", "GOVERNANCE SNAPSHOT", "👥 Admins", "✅ Apps approved",
                      "AI applications", "IT applications", "Rollout progress", "Export Coverage CSV"):
            self.assertIn(label, text)
        self.assertIn("aidl_aup_remind", text)                                 # unsigned users → alert
        self.assertIn("aidl_tab_add-admin", json.dumps(next(b for b in blocks if "Rollout" in json.dumps(b))))
        self.assertEqual(len(blocks[0]["elements"]), 5)

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
        self.assertEqual([d["text"] for d in dms], ["Welcome to AIDL, Jordan!", "Start here: your AI Acceptable Use Policy"])
        self.assertIn("aidl_ack_aup", json.dumps(dms[1]["blocks"]))       # starts on the AUP tab

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
        self._click("U7", "highway_code")
        self._click("U7", "traffic_light")
        self.assertFalse(AIDLUser.objects.get(microsoft_id="slack:T9:U7").licence_issued)
        self.assertTrue(any(m == "chat.postMessage" and b["channel"] == "U1" and "no free license seats" in b["text"]
                            for m, b in self.calls))

    def test_admin_center_has_no_add_user(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        tabs = admin_card_blocks(build_admin_cards_context(self.admin), "home")[0]["elements"]
        self.assertEqual([t["value"] for t in tabs], ["home", "add-admin", "cards", "ai-apps", "it-apps"])


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

        blocks = admin_card_blocks(build_admin_cards_context(self.admin), "home")
        header = next(b for b in blocks if "TEAM PROGRESS" in json.dumps(b))
        self.assertEqual(header["accessory"]["action_id"], "aidl_team_progress")
        fields = [f["text"] for f in blocks[blocks.index(header) + 1]["fields"]]
        self.assertTrue(fields[1].endswith("*2* of 3"))   # Highway Code accepted (admin not counted)
        self.assertTrue(fields[2].endswith("*1* of 3"))   # Traffic Light accepted
        self.assertTrue(fields[3].endswith("*2* of 3"))   # Licensed

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

    def test_admin_preview_and_progress(self):
        from .aup import generate_aup
        from .licensing import acknowledge, team_progress
        from .slack_modals import aup_view

        preview = json.dumps(aup_view(self.org), ensure_ascii=False)
        self.assertIn("Team version", preview)
        self.assertNotIn("aidl_ack_aup", preview)                            # admins only view it
        acknowledge(self.learner, "aup", generate_aup(self.org)["version"])
        t = team_progress(self.org)
        self.assertEqual((t["members"], t["aup"]), (1, 1))

    def test_send_aup_to_team_dms_learners_only(self):
        from .slack_client import encrypt_token
        from .slack_interactions import _send_aup_to_team

        self.org.slack_bot_token = encrypt_token("xoxb-t")
        self.org.save()
        calls = []
        with self.settings(SLACK_REPLY_SYNC=True), \
                patch("api.slack_client.slack_api", side_effect=lambda m, t, **k: calls.append((m, k.get("json"))) or {"ok": True}), \
                patch("api.slack_interactions.requests.post") as reply:
            reply.return_value.status_code = 200
            reply.return_value.text = "ok"
            _send_aup_to_team(self.org, "https://hooks.slack.test/r")
        dms = [j for m, j in calls if m == "chat.postMessage"]
        self.assertEqual([d["channel"] for d in dms], ["U7"])
        self.assertIn("aidl_ack_aup", json.dumps(dms[0]["blocks"]))
        self.assertIn("AUP sent to 1 of 1", reply.call_args.kwargs["json"]["text"])


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
        self.assertEqual(len(scheduled), 11)
        self.assertEqual(scheduled[0]["text"], "AIDL card: Traffic Light Check")  # sheet's posting order

    def test_leaving_cancels_scheduled_cards_and_rejoin_resumes(self):
        from .models import PackageDelivery

        self._event("U2")
        u2 = self._licence("U2")
        self.calls.clear()
        self._event("U2", "member_left_channel")
        self.assertEqual(len([m for m, _ in self.calls if m == "chat.deleteScheduledMessage"]), 11)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk), status="cancelled").count(), 11)
        self.calls.clear()
        self._event("U2")
        self.assertEqual(len([m for m, _ in self.calls if m == "chat.scheduleMessage"]), 11)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk), status="sent").count(), 1)

    def test_upgrade_to_basic_schedules_remaining_cards(self):
        from .models import PackageDelivery
        from .package_delivery import sync_organization

        self._event("U2")
        u2 = self._licence("U2")
        self.org.plan = "basic"
        self.org.save()
        with patch("api.slack_client.slack_api", side_effect=self._fake):
            sync_organization(self.org)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk)).count(), 24)
        self.assertEqual(PackageDelivery.objects.filter(user_id=str(u2.pk), status="scheduled").count(), 23)

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

        self._fill_trial()
        text = json.dumps(admin_card_blocks(build_admin_cards_context(self.admin), "home"), ensure_ascii=False)
        self.assertIn("Trial plan", text)
        self.assertIn("5* of 5 users", text)
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
        self.assertIn("Only the admin who set up AIDL", self._popup_text())

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


class SlackPolicyQuestionsTests(TestCase):
    """The 8 policy questions are asked in Slack (from the AUP buttons), not
    on the website."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed
    _fake = PlanLimitTests._fake

    def setUp(self):
        from .slack_client import encrypt_token

        self.org = make_org(slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                            slack_bot_token=encrypt_token("xoxb-t"))
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Raman")
        self.calls = []

    def _home(self):
        from .slack_blocks import admin_card_blocks
        from .slack_cards import build_admin_cards_context

        return json.dumps(admin_card_blocks(build_admin_cards_context(self.admin), "home"))

    def _press(self, action_id):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "trigger_id": "TR", "channel": {"id": "C42"}, "actions": [{"action_id": action_id, "value": "x"}]}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def test_home_asks_for_policy_until_answered(self):
        self.assertIn("aidl_policy_open", self._home())
        self.assertNotIn("aidl_aup_send", self._home())
        _answer_policy(self.org)
        self.assertIn("aidl_aup_send", self._home())
        self.assertIn("Edit policy answers", self._home())

    def test_aup_button_opens_questions_when_unanswered(self):
        self._press("aidl_aup_view")
        opened = [j for m, j in self.calls if m == "views.open"]
        self.assertEqual(opened[0]["view"]["callback_id"], "aidl_policy")
        self.assertEqual(len([b for b in opened[0]["view"]["blocks"] if b["type"] == "input"]), 8)

    def test_submitting_answers_saves_policy(self):
        from urllib.parse import urlencode

        from .org_policy import POLICY_QUESTIONS, policy_completed

        values = {qid: {"v": {"type": "radio_buttons", "selected_option": {"value": opts[1]}}}
                  for qid, opts in POLICY_QUESTIONS.items()}
        payload = {"type": "view_submission", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "view": {"callback_id": "aidl_policy", "private_metadata": "{}", "state": {"values": values}}}
        resp = self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")
        body = resp.json()
        self.assertEqual(body["response_action"], "update")          # AI-C-011: answers report
        report = json.dumps(body["view"], ensure_ascii=False)
        self.assertIn("YOUR POLICY ANSWERS", report)
        self.assertIn(list(POLICY_QUESTIONS.values())[0][1], report)
        self.org.refresh_from_db()
        self.assertTrue(policy_completed(self.org))

    def test_missing_answer_shows_error(self):
        from urllib.parse import urlencode

        payload = {"type": "view_submission", "team": {"id": "T9"}, "user": {"id": "U1", "team_id": "T9"},
                   "view": {"callback_id": "aidl_policy", "private_metadata": "{}", "state": {"values": {}}}}
        resp = self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")
        self.assertEqual(resp.json()["response_action"], "errors")
        self.assertIn("ai_policy", resp.json()["errors"])


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

    def test_code_locks_after_five_wrong_tries(self):
        token = self._signup().json()["otp_token"]
        for _ in range(5):
            self.client.post("/api/auth/signup/verify/", {"otp_token": token, "code": "111111"}, content_type="application/json")
        last = self.client.post("/api/auth/signup/verify/", {"otp_token": token, "code": self._code()},
                                content_type="application/json")
        self.assertIn("Too many", last.json()["code"][0])

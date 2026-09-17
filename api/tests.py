"""Admin Center backend tests — permission chips, policy versioning, the
Cards catalogue (quota/request/send/schedule), Add Application, and the
invite-email HTML-link regression."""

from __future__ import annotations

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
from django.test import TransactionTestCase as TestCase
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

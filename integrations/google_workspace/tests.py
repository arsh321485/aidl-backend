"""Google Workspace: sign-in and the Gmail flow, with Google faked (emails go to mail.outbox).

    python manage.py test integrations.google_workspace
"""

from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core import mail as outbox
from django.test import TestCase, override_settings

from api.microsoft_auth import create_oauth_state
from api.models import AIDLUser, Organization
from integrations.google_classroom.models import GoogleWorkspace

from . import links
from .google_auth import GoogleProfile
from .views import JOIN, MODE

GMAIL = "https://mail.google.com/mail/u/?authuser="


def person(sub="1", email="asha@acme.com", hd="acme.com", verified=True):
    return GoogleProfile(sub=sub, email=email, email_verified=verified, hd=hd, first_name="Asha", last_name="Rao",
                         full_name="Asha Rao", picture="")


@override_settings(BACKEND_URL="http://backend.test")
@mock.patch("integrations.google_workspace.google_auth.configured", return_value=True)
@mock.patch("api.card_image.warm")  # don't draw card images in the background
class GoogleWorkspaceTests(TestCase):

    def _callback(self, who, payload=MODE):
        state = create_oauth_state("organization", payload=payload)
        with mock.patch("integrations.google_workspace.google_auth.profile_from_code", return_value=who):
            return self.client.get("/api/google-workspace/callback/", {"state": state, "code": "abc"})

    def _start(self):
        """The admin (signed in) approves both starter cards and starts."""
        for item in ("traffic_light", "highway_code"):
            self.client.post("/api/google-workspace/setup/", {"action": "approve", "item": item})
        return self.client.post("/api/google-workspace/setup/", {"action": "start"}, follow=True)

    def _subjects(self, to):
        return [m.subject for m in outbox.outbox if m.to == [to]]

    # ---------- sign-in ----------

    def test_login_gives_googles_url_with_our_callback(self, *_):
        with mock.patch("integrations.google_classroom.google_oauth.client_id", return_value="cid"):
            data = self.client.get("/api/google-workspace/login/").json()
        self.assertEqual(data["mode"], "google_workspace")
        q = {k: v[0] for k, v in parse_qs(urlparse(data["auth_url"]).query).items()}
        self.assertEqual(q["redirect_uri"], "http://backend.test/api/google-workspace/callback/")
        self.assertEqual(q["scope"], "openid email profile")

    def test_first_person_of_a_domain_is_owner_admin_and_gets_the_admin_center_email(self, *_):
        resp = self._callback(person())
        self.assertEqual(resp["Location"], GMAIL + "asha@acme.com")  # AIDL lives in Gmail
        self.assertEqual(self._subjects("asha@acme.com"), ["AIDL Admin Center · acme.com"])
        self.assertIn("/api/google-workspace/go/?t=", outbox.outbox[0].alternatives[0][0])
        user = AIDLUser.objects.get(email="asha@acme.com")
        org = Organization.objects.get(pk=user.organization_id)
        self.assertEqual((org.name, org.owner_user_id, user.provider), ("acme.com", str(user.pk), "google_workspace"))
        self.assertEqual(GoogleWorkspace.objects.get(key="domain:acme.com").organization_id, str(org.pk))
        page = self.client.get("/api/google-workspace/")  # the cookie opens the admin page
        self.assertContains(page, "you're an AIDL admin")
        self.assertContains(page, "Send invites")

    def test_colleague_joins_the_same_org_as_learner(self, *_):
        self._callback(person())
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        ben = AIDLUser.objects.get(email="ben@acme.com")
        asha = AIDLUser.objects.get(email="asha@acme.com")
        self.assertEqual((ben.role, ben.organization_id), ("learner", asha.organization_id))
        self.assertEqual(Organization.objects.count(), 1)

    def test_personal_gmail_is_refused(self, *_):
        resp = self._callback(person(email="me@gmail.com", hd=""))
        self.assertContains(resp, "work Google Workspace account", status_code=400)
        self.assertContains(resp, "Try again with Google", status_code=400)
        self.assertFalse(AIDLUser.objects.exists())

    def test_expired_state_is_refused(self, *_):
        resp = self.client.get("/api/google-workspace/callback/", {"state": "nope", "code": "abc"})
        self.assertContains(resp, "sign-in session expired", status_code=400)

    def test_slack_admin_keeps_their_org_and_links_the_domain(self, *_):
        org = Organization.objects.create(name="Acme", slug="acme")
        AIDLUser.objects.create(email="asha@acme.com", full_name="Asha", role="admin", organization_id=str(org.pk),
                                organization_name="Acme", enroll_as="organization", provider="slack", is_active=True)
        self._callback(person())
        self.assertEqual(GoogleWorkspace.objects.get(key="domain:acme.com").organization_id, str(org.pk))
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        self.assertEqual(AIDLUser.objects.get(email="ben@acme.com").organization_id, str(org.pk))
        self.assertEqual(Organization.objects.count(), 1)

    def test_home_without_cookie_goes_to_google(self, *_):
        resp = self.client.get("/api/google-workspace/")
        self.assertEqual(resp["Location"], "/api/google-workspace/join/")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_without_a_mail_service_the_aidl_page_opens(self, *_):
        resp = self._callback(person())
        self.assertEqual(resp["Location"], "/api/google-workspace/")

    # ---------- the admin ----------

    def test_learner_waits_until_admin_starts(self, *_):
        self._callback(person())
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        page = self.client.get("/api/google-workspace/")
        self.assertContains(page, "Nothing to do yet")
        self.assertNotContains(page, "teacher")
        self.client.post("/api/google-workspace/setup/", {"action": "start"})  # learners can't press admin buttons
        from api import admin_setup

        self.assertFalse(admin_setup.started(Organization.objects.get()))

    def test_start_tells_the_admin_and_welcomes_people_who_already_joined(self, *_):
        self._callback(person())
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        self.assertEqual(self._subjects("ben@acme.com"), [])  # nothing until the programme starts
        self._callback(person())  # the admin again
        resp = self._start()
        self.assertContains(resp, "The programme has started")
        self.assertIn("🚀 AIDL is running at acme.com", self._subjects("asha@acme.com"))
        self.assertEqual(self._subjects("ben@acme.com"), ["👋 Welcome to your AI Driving Licence"])

    def test_admin_invites_colleagues_of_the_same_domain_only(self, *_):
        self._callback(person())
        resp = self.client.post("/api/google-workspace/invite/",
                                {"emails": "ben@acme.com, carla@ACME.com\nspy@other.com, nonsense, ben@acme.com"},
                                follow=True)
        self.assertContains(resp, "Invited 2 colleagues")
        self.assertContains(resp, "Skipped 2")
        self.assertEqual(len(self._subjects("ben@acme.com")), 1)
        self.assertEqual(len(self._subjects("carla@acme.com")), 1)
        self.assertEqual(self._subjects("spy@other.com"), [])
        self.assertIn("/api/google-workspace/join/", outbox.outbox[-1].body)

    def test_learners_cant_invite(self, *_):
        self._callback(person())
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        self.client.post("/api/google-workspace/invite/", {"emails": "carla@acme.com"})
        self.assertEqual(self._subjects("carla@acme.com"), [])

    # ---------- employees: the Gmail flow ----------

    def test_whole_journey_arrives_by_email_one_step_at_a_time(self, *_):
        self._callback(person())
        self._start()
        resp = self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)  # cookie = Ben now
        self.assertEqual(resp["Location"], GMAIL + "ben@acme.com")
        self.assertEqual(self._subjects("ben@acme.com"), ["👋 Welcome to your AI Driving Licence"])
        ben = AIDLUser.objects.get(email="ben@acme.com")

        resp = self.client.post("/api/google-workspace/action/", {"action": "start"}, follow=True)
        self.assertContains(resp, "Your next step is in your Gmail")
        cards = self.client.get("/api/google-workspace/").context["policy"]
        if cards:  # the company's AI policy cards come first, when it has some
            self.assertEqual(self._subjects("ben@acme.com")[-1], "📜 Your AI policy cards")
        for c in cards:
            self.client.post("/api/google-workspace/action/", {"action": "agree", "n": c["n"], "version": c["version"]})
        self.assertEqual(self._subjects("ben@acme.com")[-1], "🚦 Your Traffic Light Check")
        self.client.post("/api/google-workspace/action/", {"action": "traffic_light"})
        self.assertEqual(self._subjects("ben@acme.com")[-1], "📖 Your Highway Code")
        self.client.post("/api/google-workspace/action/", {"action": "highway_code"})
        self.assertEqual(self._subjects("ben@acme.com")[-1], "🎨 One last step: your licence avatar")

        ben.refresh_from_db()
        self.assertTrue(ben.licence_issued)
        ben.avatar_data = "iVBORw0KGgo="  # made on the avatar page, then back to AIDL
        ben.save(update_fields=["avatar_data"])
        self.client.get("/api/google-workspace/")
        # the licence is shown in the Gmail side panel, so it isn't emailed
        self.assertEqual(self._subjects("ben@acme.com")[-1], "🎨 One last step: your licence avatar")

        count = len(outbox.outbox)
        self.client.get("/api/google-workspace/")  # each step is emailed once
        self.assertEqual(len(outbox.outbox), count)

    def test_email_button_signs_in_and_opens_the_step(self, *_):
        self._callback(person())
        asha = AIDLUser.objects.get(email="asha@acme.com")
        self.client.cookies.clear()
        resp = self.client.get(links.link(asha).split("backend.test", 1)[1])
        self.assertEqual(resp["Location"], "/api/google-workspace/")
        self.assertContains(self.client.get("/api/google-workspace/"), "you're an AIDL admin")

    def test_bad_email_link_is_refused(self, *_):
        resp = self.client.get("/api/google-workspace/go/", {"t": "forged"})
        self.assertContains(resp, "link has expired", status_code=400)
        with self.assertRaises(ValueError):
            links.link(AIDLUser(pk=None), "/api/moodle/")

    # ---------- the Admin Center: team progress, reminders, export ----------

    def test_team_list_shows_invited_joined_and_in_progress(self, *_):
        self._callback(person())
        self.client.post("/api/google-workspace/invite/", {"emails": "ben@acme.com, carla@acme.com"})
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)  # Ben joins, Carla doesn't
        self._callback(person())
        page = self.client.get("/api/google-workspace/")
        self.assertContains(page, "Joined · waiting for you to start")
        self.assertContains(page, "hasn&#x27;t joined", count=1)  # Carla only
        summary = page.context["team_summary"]
        self.assertEqual((summary["in_progress"], summary["invited"], summary["licensed"]), (1, 1, 0))
        self._start()
        page = self.client.get("/api/google-workspace/")
        self.assertContains(page, "Hasn&#x27;t opened the Welcome email")

    def test_remind_resends_the_current_email_once_a_day(self, *_):
        self._callback(person())
        self._start()
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        self._callback(person())
        ben = AIDLUser.objects.get(email="ben@acme.com")
        resp = self.client.post("/api/google-workspace/remind/", {"who": f"u:{ben.pk}"}, follow=True)
        self.assertContains(resp, "Reminder sent")
        self.assertEqual(self._subjects("ben@acme.com"), ["👋 Welcome to your AI Driving Licence"] * 2)
        resp = self.client.post("/api/google-workspace/remind/", {"who": f"u:{ben.pk}"}, follow=True)
        self.assertContains(resp, "once a day")
        self.assertEqual(len(self._subjects("ben@acme.com")), 2)
        self.assertContains(resp, "Reminded today")

    def test_invites_can_be_reminded_after_a_day_and_remind_all(self, *_):
        from datetime import timedelta

        from django.utils import timezone

        from .models import WorkspaceInvite

        self._callback(person())
        self.client.post("/api/google-workspace/invite/", {"emails": "carla@acme.com"})
        resp = self.client.post("/api/google-workspace/remind/", {"who": "all"}, follow=True)
        self.assertContains(resp, "Nobody to remind")  # invited a moment ago
        WorkspaceInvite.objects.update(invited_at=timezone.now() - timedelta(days=2))
        resp = self.client.post("/api/google-workspace/remind/", {"who": "all"}, follow=True)
        self.assertContains(resp, "Reminded 1 person")
        self.assertEqual(len(self._subjects("carla@acme.com")), 2)

    def test_learners_cant_remind_or_export(self, *_):
        from datetime import timedelta

        from django.utils import timezone

        from .models import WorkspaceInvite

        self._callback(person())
        self.client.post("/api/google-workspace/invite/", {"emails": "carla@acme.com"})
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        WorkspaceInvite.objects.update(invited_at=timezone.now() - timedelta(days=2))
        resp = self.client.post("/api/google-workspace/remind/", {"who": "all"})
        self.assertIn("Only%20your%20AIDL%20admins", resp["Location"])
        self.assertEqual(len(self._subjects("carla@acme.com")), 1)  # just the invite
        self.assertContains(self.client.get("/api/google-workspace/export/"), "Admins only", status_code=403)

    def test_admin_downloads_the_coverage_csv(self, *_):
        self._callback(person())
        self._callback(person(sub="2", email="ben@acme.com"), payload=JOIN)
        self._callback(person())
        resp = self.client.get("/api/google-workspace/export/")
        self.assertEqual(resp["Content-Type"], "text/csv")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("ben@acme.com", resp.content.decode())

    def test_remind_with_a_made_up_key_does_nothing(self, *_):
        self._callback(person())
        for key in ("u:nope", "i:nope", "u:", "x:1", ""):
            resp = self.client.post("/api/google-workspace/remind/", {"who": key})
            self.assertEqual(resp.status_code, 302)

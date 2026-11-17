"""Moodle sign-in, end to end, with a fake Moodle (our own signing key).

    python manage.py test integrations.moodle
"""

import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import TestCase

from api.models import AIDLUser, Organization
from integrations.lti_core.launch import CLAIM, STAFF_ROLES
from integrations.lti_core.models import LtiPlatform

SITE = "https://moodle.test"
CLIENT_ID = "client-123"
DEPLOYMENT = "1"
STUDENT = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
TEACHER = STAFF_ROLES[0]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeMoodle(TestCase):
    """A registered Moodle site + the helpers to log in from it."""

    def setUp(self):
        self.platform = LtiPlatform.objects.create(
            kind="moodle", name="Test Moodle", issuer=SITE, client_id=CLIENT_ID, deployment_ids=DEPLOYMENT,
            auth_login_url=f"{SITE}/mod/lti/auth.php", jwks_url=f"{SITE}/mod/lti/certs.php")
        # Moodle's public key = our test key's public half.
        patcher = mock.patch("integrations.lti_core.launch._signing_key", return_value=KEY.public_key())
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- helpers: what Moodle would do ---

    def _start_login(self):
        """LTI step 1 → (state, nonce) Moodle got back from us."""
        resp = self.client.post("/api/moodle/login/", {"iss": SITE, "login_hint": "2", "client_id": CLIENT_ID,
                                                       "lti_message_hint": "abc"})
        self.assertEqual(resp.status_code, 302)
        url = urlparse(resp["Location"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", f"{SITE}/mod/lti/auth.php")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.assertEqual(q["client_id"], CLIENT_ID)
        self.assertEqual(q["lti_message_hint"], "abc")
        return q["state"], q["nonce"]

    def _token(self, real_nonce, sub="2", roles=(TEACHER,), **changes):
        now = int(time.time())
        claims = {
            "iss": SITE, "aud": CLIENT_ID, "sub": sub, "iat": now, "exp": now + 60, "nonce": real_nonce,
            "name": "Asha Rao", "given_name": "Asha", "family_name": "Rao", "email": f"user{sub}@school.test",
            CLAIM + "version": "1.3.0", CLAIM + "message_type": "LtiResourceLinkRequest",
            CLAIM + "deployment_id": DEPLOYMENT, CLAIM + "roles": list(roles),
            CLAIM + "context": {"id": "7", "title": "AI Basics"},
            CLAIM + "tool_platform": {"name": "Green Valley School"},
        }
        claims.update(changes)
        return jwt.encode(claims, KEY, algorithm="RS256")

    def _launch(self, **kwargs):
        state, nonce = self._start_login()
        return self.client.post("/api/moodle/launch/", {"id_token": self._token(nonce, **kwargs), "state": state})


class MoodleLoginTests(FakeMoodle):
    def test_first_teacher_creates_org_and_is_owner_admin(self):
        resp = self._launch()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("aidl_moodle", resp.cookies)
        user = AIDLUser.objects.get(microsoft_id=f"moodle:{self.platform.pk}:2")
        org = Organization.objects.get(pk=user.organization_id)
        self.assertEqual(org.name, "Green Valley School")
        self.assertEqual(user.role, "admin")
        self.assertEqual(org.owner_user_id, str(user.pk))
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.organization_id, str(org.pk))
        # The cookie opens the home page (the teacher's "start" box).
        self.assertContains(self.client.get("/api/moodle/"), "Get your learners started")

    def test_student_after_teacher_is_learner_in_same_org(self):
        self._launch()
        resp = self._launch(sub="9", roles=[STUDENT])
        self.assertEqual(resp.status_code, 200)
        student = AIDLUser.objects.get(microsoft_id=f"moodle:{self.platform.pk}:9")
        self.assertEqual(student.role, "learner")
        self.assertEqual(Organization.objects.count(), 1)

    def test_student_first_is_refused(self):
        resp = self._launch(sub="9", roles=[STUDENT])
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Organization.objects.exists())

    def test_second_launch_reuses_the_user(self):
        self._launch()
        self._launch(name="Asha R.")
        self.assertEqual(AIDLUser.objects.count(), 1)
        self.assertEqual(AIDLUser.objects.get().full_name, "Asha R.")

    def test_state_works_once(self):
        state, nonce = self._start_login()
        token = self._token(nonce)
        self.assertEqual(self.client.post("/api/moodle/launch/", {"id_token": token, "state": state}).status_code, 200)
        self.assertEqual(self.client.post("/api/moodle/launch/", {"id_token": token, "state": state}).status_code, 403)

    def test_bad_tokens_are_refused(self):
        for changes in ({"aud": "someone-else"}, {"iss": "https://evil.test"}, {"nonce": "wrong"},
                        {CLAIM + "deployment_id": "99"}, {"exp": int(time.time()) - 600}):
            with self.subTest(changes=list(changes)):
                self.assertEqual(self._launch(**changes).status_code, 403)
        self.assertFalse(AIDLUser.objects.exists())

    def test_token_signed_by_another_key_is_refused(self):
        state, nonce = self._start_login()
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(jwt.decode(self._token(nonce), options={"verify_signature": False}), other, algorithm="RS256")
        self.assertEqual(self.client.post("/api/moodle/launch/", {"id_token": token, "state": state}).status_code, 403)

    def test_unknown_site_is_refused(self):
        resp = self.client.post("/api/moodle/login/", {"iss": "https://other.test", "login_hint": "2"})
        self.assertEqual(resp.status_code, 400)

    def test_connect_page_shows_tool_urls(self):
        resp = self.client.get("/api/moodle/connect/")
        self.assertContains(resp, "/api/moodle/launch/")
        self.assertContains(resp, "/api/moodle/login/")
        self.assertContains(resp, "/api/lti/jwks/")

    def test_home_without_cookie(self):
        self.assertEqual(self.client.get("/api/moodle/").status_code, 401)


@mock.patch("api.card_image.warm")  # don't draw card images in the background
class MoodleLearnerFlowTests(FakeMoodle):
    """Step 2: teacher starts the programme → learner goes card by card → licence."""

    def _as_teacher(self):
        self._launch()

    def _as_student(self):
        self._launch(sub="9", roles=[STUDENT])
        return AIDLUser.objects.get(microsoft_id=f"moodle:{self.platform.pk}:9")

    def _start_programme(self):
        self._as_teacher()
        for item in ("traffic_light", "highway_code"):
            self.client.post("/api/moodle/setup/", {"action": "approve", "item": item})
        self.client.post("/api/moodle/setup/", {"action": "start"})

    def test_learner_waits_until_teacher_starts(self, _warm):
        self._as_teacher()
        self._launch(sub="9", roles=[STUDENT])
        self.assertContains(self.client.get("/api/moodle/"), "Nothing to do yet")

    def test_start_needs_both_cards_approved(self, _warm):
        self._as_teacher()
        resp = self.client.post("/api/moodle/setup/", {"action": "start"}, follow=True)
        self.assertContains(resp, "Approve the Traffic Light Check and the Highway Code first")

    def test_student_cant_use_teacher_buttons(self, _warm):
        self._as_teacher()
        self._as_student()
        resp = self.client.post("/api/moodle/setup/", {"action": "approve", "item": "traffic_light"}, follow=True)
        self.assertContains(resp, "Only your school")

    def test_full_flow_to_licence(self, _warm):
        self._start_programme()
        student = self._as_student()
        self.assertContains(self.client.get("/api/moodle/"), "Let's go")

        self.client.post("/api/moodle/action/", {"action": "start"})
        self.assertContains(self.client.get("/api/moodle/"), "Traffic Light Check")

        self.client.post("/api/moodle/action/", {"action": "traffic_light"})
        self.assertContains(self.client.get("/api/moodle/"), "Get my licence")

        self.client.post("/api/moodle/action/", {"action": "highway_code"})
        student.refresh_from_db()
        self.assertTrue(student.licence_issued)
        self.assertTrue(student.licence_number)
        # No avatar yet → asked to make one.
        self.assertContains(self.client.get("/api/moodle/"), "Create my avatar")

        student.avatar_data = "iVBORw0KGgo="  # any avatar
        student.save(update_fields=["avatar_data"])
        resp = self.client.get("/api/moodle/")
        self.assertContains(resp, "road-ready")
        self.assertContains(resp, student.licence_number)

    def test_cards_must_be_done_in_order(self, _warm):
        self._start_programme()
        student = self._as_student()
        self.client.post("/api/moodle/action/", {"action": "start"})
        self.client.post("/api/moodle/action/", {"action": "highway_code"})  # skipping ahead is ignored
        self.client.post("/api/moodle/action/", {"action": "traffic_light"})
        student.refresh_from_db()
        self.assertFalse(student.licence_issued)

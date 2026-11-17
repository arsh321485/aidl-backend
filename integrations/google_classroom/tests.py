"""Google Classroom sign-up / sign-in, end to end, with a fake Google (our own signing key).

    python manage.py test integrations.google_classroom
"""

import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import TestCase, override_settings

from api.auth_jwt import decode_token
from api.models import AIDLUser, Organization
from integrations.google_classroom.models import GoogleWorkspace

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CLIENT_ID = "test-client.apps.googleusercontent.com"
FRONTEND = "https://frontend.test/auth/callback"


@override_settings(AUTH_SUCCESS_REDIRECT=FRONTEND, BACKEND_URL="https://backend.test")
class GoogleClassroomSignIn(TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": "secret",
                                             "GOOGLE_CLASSROOM_REDIRECT_URI": ""})
        env.start()
        self.addCleanup(env.stop)
        patcher = mock.patch("integrations.google_classroom.google_oauth._signing_key",
                             return_value=KEY.public_key())
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- helpers: what the browser + Google would do ---

    def _id_token(self, sub="111", email="asha@school.edu", hd="school.edu", **changes):
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": sub, "iat": now, "exp": now + 300,
                  "email": email, "email_verified": True, "name": "Asha Rao", "given_name": "Asha",
                  "family_name": "Rao", "picture": "https://lh3.googleusercontent.com/a/x"}
        if hd:
            claims["hd"] = hd
        claims.update(changes)
        return jwt.encode(claims, KEY, algorithm="RS256")

    def _state(self, enroll_as="organization"):
        resp = self.client.get("/api/google-classroom/login/", {"enroll_as": enroll_as})
        self.assertEqual(resp.status_code, 200)
        auth = urlparse(resp.json()["auth_url"])
        q = {k: v[0] for k, v in parse_qs(auth.query).items()}
        self.assertEqual(auth.netloc, "accounts.google.com")
        self.assertEqual(q["client_id"], CLIENT_ID)
        self.assertEqual(q["redirect_uri"], "https://backend.test/api/google-classroom/callback/")
        return q["state"]

    def _sign_in(self, enroll_as="organization", **token_args):
        """Full round trip → the frontend redirect's query params."""
        state = self._state(enroll_as)
        with mock.patch("integrations.google_classroom.google_oauth.exchange_code",
                        return_value=self._id_token(**token_args)):
            back = self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": state})
        self.assertEqual(back.status_code, 302)
        url = urlparse(back["Location"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", FRONTEND)
        return {k: v[0] for k, v in parse_qs(url.query).items()}

    # --- sign-up ---

    def test_first_teacher_creates_the_school_and_is_its_admin(self):
        r = self._sign_in()
        self.assertEqual(r["mode"], "google_classroom")
        user = AIDLUser.objects.get(pk=decode_token(r["access_token"])["sub"])
        self.assertEqual((user.microsoft_id, user.provider, user.role), ("google:111", "google_classroom", "admin"))
        org = Organization.objects.get(pk=user.organization_id)
        self.assertEqual((org.name, org.owner_user_id), ("school.edu", str(user.pk)))
        self.assertEqual(GoogleWorkspace.objects.get(key="domain:school.edu").organization_id, str(org.pk))

    def test_same_domain_joins_the_same_school_as_learner(self):
        self._sign_in()
        r = self._sign_in(sub="222", email="ravi@school.edu")
        ravi = AIDLUser.objects.get(microsoft_id="google:222")
        asha = AIDLUser.objects.get(microsoft_id="google:111")
        self.assertEqual(ravi.role, "learner")
        self.assertEqual(ravi.organization_id, asha.organization_id)
        self.assertEqual(r["role"], "learner")
        self.assertEqual(Organization.objects.count(), 1)

    def test_other_domain_gets_its_own_school(self):
        self._sign_in()
        self._sign_in(sub="333", email="t@other.edu", hd="other.edu")
        self.assertEqual(Organization.objects.count(), 2)
        self.assertEqual(AIDLUser.objects.get(microsoft_id="google:333").role, "admin")

    def test_personal_gmail_gets_a_one_person_classroom(self):
        self._sign_in(sub="444", email="me@gmail.com", hd="")
        self._sign_in(sub="555", email="you@gmail.com", hd="")
        self.assertEqual(Organization.objects.count(), 2)  # never joined by "gmail.com"
        self.assertTrue(GoogleWorkspace.objects.filter(key="user:444").exists())

    def test_individual_signup_has_no_organization(self):
        self._sign_in(enroll_as="individual")
        user = AIDLUser.objects.get(microsoft_id="google:111")
        self.assertEqual((user.enroll_as, user.organization_id), ("individual", ""))
        self.assertEqual(Organization.objects.count(), 0)

    # --- sign-in ---

    def test_second_sign_in_is_the_same_account(self):
        self._sign_in()
        self._sign_in()
        self.assertEqual(AIDLUser.objects.filter(microsoft_id="google:111").count(), 1)
        self.assertEqual(AIDLUser.objects.get(microsoft_id="google:111").role, "admin")
        self.assertEqual(Organization.objects.count(), 1)

    def test_existing_aidl_account_with_verified_email_is_reused(self):
        existing = AIDLUser.objects.create(microsoft_id="web:asha", email="asha@school.edu", provider="website")
        r = self._sign_in(enroll_as="individual")
        self.assertEqual(decode_token(r["access_token"])["sub"], str(existing.pk))

    def test_unverified_email_is_not_linked(self):
        existing = AIDLUser.objects.create(microsoft_id="web:asha", email="asha@school.edu", provider="website")
        r = self._sign_in(enroll_as="individual", email_verified=False)
        self.assertNotEqual(decode_token(r["access_token"])["sub"], str(existing.pk))

    def test_disabled_account_is_refused(self):
        self._sign_in()
        AIDLUser.objects.filter(microsoft_id="google:111").update(is_active=False)
        r = self._sign_in()
        self.assertEqual(r["error"], "account_disabled")
        self.assertNotIn("access_token", r)

    # --- refusals ---

    def test_token_for_another_app_is_refused(self):
        r = self._sign_in(aud="someone-else")
        self.assertEqual(r["error"], "invalid_token")
        self.assertEqual(AIDLUser.objects.count(), 0)

    def test_unknown_state_is_refused(self):
        resp = self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": "made-up"})
        self.assertIn("error=state_not_found", resp["Location"])

    def test_state_works_only_once(self):
        state = self._state()
        with mock.patch("integrations.google_classroom.google_oauth.exchange_code", return_value=self._id_token()):
            self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": state})
            again = self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": state})
        self.assertIn("error=", again["Location"])
        self.assertNotIn("access_token", again["Location"])

    def test_cancel_on_google(self):
        resp = self.client.get("/api/google-classroom/callback/", {"error": "access_denied"})
        self.assertIn("error=access_denied", resp["Location"])

    def test_school_full_refuses_new_learner(self):
        self._sign_in()
        with mock.patch("api.plans.can_join", return_value=False):
            r = self._sign_in(sub="222", email="ravi@school.edu")
        self.assertEqual(r["error"], "no_free_seats")
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:222").exists())

    def test_not_configured(self):
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": ""}):
            resp = self.client.get("/api/google-classroom/login/")
        self.assertEqual(resp.status_code, 503)


# --- step 2: classes + students ---

SCOPES = " ".join(["openid", "email", "profile",
                   "https://www.googleapis.com/auth/classroom.courses.readonly",
                   "https://www.googleapis.com/auth/classroom.rosters.readonly",
                   "https://www.googleapis.com/auth/classroom.profile.emails"])


class FakeClassroom:
    """Google Classroom's API: courses and students, by access token."""

    def __init__(self):
        self.courses = [{"id": "c1", "name": "AI Basics", "section": "Grade 9", "courseState": "ACTIVE",
                         "alternateLink": "https://classroom.google.com/c/c1"},
                        {"id": "c2", "name": "Robotics", "courseState": "ACTIVE"}]
        self.students = {
            "c1": [{"userId": "s1", "profile": {"name": {"fullName": "Ravi Kumar"}, "emailAddress": "ravi@school.edu"}},
                   {"userId": "s2", "profile": {"name": {"fullName": "Meera Shah"}, "emailAddress": "meera@school.edu"}}],
            "c2": [],
        }
        self.tokens_seen = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.tokens_seen.append(headers["Authorization"])
        resp = mock.Mock(ok=True, status_code=200)
        if url.endswith("/courses"):
            assert params["teacherId"] == "me"
            resp.json.return_value = {"courses": self.courses}
        else:
            resp.json.return_value = {"students": self.students[url.split("/")[-2]]}
        return resp


class GoogleClassroomClasses(GoogleClassroomSignIn):
    """Inherits the fake Google from the sign-in tests (but not their tests)."""

    def setUp(self):
        super().setUp()
        self.fake = FakeClassroom()
        patcher = mock.patch("integrations.google_classroom.classroom_api.requests.get", side_effect=self.fake.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect(self, scope=SCOPES, refresh_token="refresh-1", **token_args):
        resp = self.client.get("/api/google-classroom/classes/connect/")
        self.assertEqual(resp.status_code, 302)
        q = {k: v[0] for k, v in parse_qs(urlparse(resp["Location"]).query).items()}
        self.assertIn("classroom.rosters.readonly", q["scope"])
        self.assertEqual((q["access_type"], q["prompt"]), ("offline", "consent"))
        tokens = {"id_token": self._id_token(**token_args), "access_token": "access-1", "scope": scope}
        if refresh_token:
            tokens["refresh_token"] = refresh_token
        with mock.patch("integrations.google_classroom.google_oauth.exchange_tokens", return_value=tokens):
            return self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": q["state"]})

    def test_teacher_connects_and_sees_classes_and_students(self):
        self._sign_in()
        resp = self._connect()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("Found+2+classes", resp["Location"])
        page = self.client.get("/api/google-classroom/classes/")
        self.assertContains(page, "AI Basics")
        self.assertContains(page, "Ravi Kumar")
        self.assertContains(page, "meera@school.edu")
        self.assertContains(page, "Robotics")

    def test_refresh_token_is_stored_encrypted(self):
        from integrations.google_classroom.models import GoogleCredential
        from api.microsoft_auth import open_refresh_token

        self._sign_in()
        self._connect()
        cred = GoogleCredential.objects.get()
        self.assertNotEqual(cred.refresh_token, "refresh-1")
        self.assertEqual(open_refresh_token(cred.refresh_token), "refresh-1")

    def test_refresh_fetches_again_with_the_stored_token(self):
        from integrations.google_classroom.models import GoogleCourse, GoogleStudent

        self._sign_in()
        self._connect()
        self.fake.courses = self.fake.courses[:1]  # Robotics archived
        self.fake.students["c1"] = self.fake.students["c1"][:1]  # Meera left
        with mock.patch("integrations.google_classroom.google_oauth.refresh_access_token",
                        return_value="access-2") as refresh:
            resp = self.client.post("/api/google-classroom/classes/sync/")
        refresh.assert_called_once_with("refresh-1")
        self.assertEqual(self.fake.tokens_seen[-1], "Bearer access-2")
        self.assertIn("1+class", resp["Location"])
        self.assertEqual(list(GoogleCourse.objects.values_list("name", flat=True)), ["AI Basics"])
        self.assertEqual(list(GoogleStudent.objects.values_list("full_name", flat=True)), ["Ravi Kumar"])

    def test_json_endpoint_with_aidl_token(self):
        r = self._sign_in()
        self._connect()
        resp = self.client.get("/api/google-classroom/api/classes/",
                               HTTP_AUTHORIZATION=f"Bearer {r['access_token']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["connected"])
        names = {c["name"]: c for c in data["classes"]}
        self.assertEqual(names["AI Basics"]["student_count"], 2)
        self.assertEqual({s["name"] for s in names["AI Basics"]["students"]}, {"Ravi Kumar", "Meera Shah"})

    def test_json_endpoint_needs_sign_in(self):
        self.assertIn(self.client.get("/api/google-classroom/api/classes/").status_code, (401, 403))

    def test_learner_cannot_connect(self):
        self._sign_in()
        self._sign_in(sub="222", email="ravi@school.edu")  # joins as learner
        resp = self._connect(sub="222", email="ravi@school.edu")
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "Only AIDL admins", status_code=403)

    def test_unknown_google_account_must_sign_up_first(self):
        resp = self._connect(sub="999", email="new@school.edu")
        self.assertContains(resp, "Sign up first", status_code=403)

    def test_unticked_permissions_are_refused(self):
        self._sign_in()
        resp = self._connect(scope="openid email profile")
        self.assertContains(resp, "tick every box", status_code=400)

    def test_classroom_api_switched_off(self):
        self._sign_in()
        off = mock.Mock(ok=False, status_code=403)
        off.json.return_value = {"error": {"status": "PERMISSION_DENIED",
                                           "message": "Google Classroom API has not been used in project 1"}}
        with mock.patch("integrations.google_classroom.classroom_api.requests.get", return_value=off):
            resp = self._connect()
        self.assertContains(resp, "Google Classroom API is switched off", status_code=400)

    def test_page_without_session_offers_connect(self):
        page = self.client.get("/api/google-classroom/classes/")
        self.assertContains(page, "Connect Google Classroom")


# The step-2 class inherits from the sign-in class for its helpers; don't run the sign-in tests twice.
for _name in [n for n in dir(GoogleClassroomSignIn) if n.startswith("test_")]:
    setattr(GoogleClassroomClasses, _name, None)

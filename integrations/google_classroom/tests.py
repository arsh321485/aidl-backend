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

    def test_admin_gets_the_google_classroom_link_and_is_signed_in_to_admin_center(self):
        r = self._sign_in()
        self.assertTrue(r["admin_center_url"].startswith("https://backend.test/api/google-classroom/open/?email="))
        self.assertEqual(r["open_admin_center"], "1")
        # That link goes straight on to Google Classroom, as the signed-in account.
        go = self.client.get(r["admin_center_url"].removeprefix("https://backend.test"))
        self.assertEqual(go.status_code, 302)
        self.assertTrue(go["Location"].startswith("https://classroom.google.com/?authuser="))
        # The same browser opens the Admin Center without signing in again.
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "Admin Center")

    def test_learner_gets_no_admin_center_link(self):
        self._sign_in()
        r = self._sign_in(sub="222", email="ravi@school.edu")
        self.assertNotIn("admin_center_url", r)

    def test_individual_gets_no_admin_center_link(self):
        r = self._sign_in(enroll_as="individual")
        self.assertNotIn("admin_center_url", r)

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


# --- step 3: classes in the AIDL programme ---

class GoogleClassroomProgramme(GoogleClassroomClasses):
    def _teacher_with_classes(self):
        self._sign_in()
        self._connect()
        return AIDLUser.objects.get(microsoft_id="google:111")

    def _choose(self, course_id="c1", on=True):
        return self.client.post("/api/google-classroom/classes/choose/", {"course_id": course_id, "on": "1" if on else "0"})

    def test_adding_a_class_makes_its_students_learners(self):
        teacher = self._teacher_with_classes()
        resp = self._choose()
        self.assertIn("2+students+in+AIDL", resp["Location"])
        for gid in ("s1", "s2"):
            s = AIDLUser.objects.get(microsoft_id=f"google:{gid}")
            self.assertEqual((s.role, s.organization_id, s.provider), ("learner", teacher.organization_id, "google_classroom"))
        self.assertEqual(AIDLUser.objects.get(microsoft_id="google:s1").full_name, "Ravi Kumar")
        page = self.client.get("/api/google-classroom/classes/")
        self.assertContains(page, "✅ In AIDL")
        self.assertContains(page, "✅ Learner")

    def test_unchosen_class_adds_nobody(self):
        self._teacher_with_classes()
        self.assertFalse(AIDLUser.objects.filter(microsoft_id__in=["google:s1", "google:s2"]).exists())

    def test_added_student_signs_in_with_google_into_the_school(self):
        teacher = self._teacher_with_classes()
        self._choose()
        r = self._sign_in(sub="s1", email="ravi@gmail.com", hd="")  # personal Gmail: would otherwise get own org
        user = AIDLUser.objects.get(pk=decode_token(r["access_token"])["sub"])
        self.assertEqual(user.microsoft_id, "google:s1")
        self.assertEqual((user.role, user.organization_id), ("learner", teacher.organization_id))
        self.assertEqual(AIDLUser.objects.filter(microsoft_id="google:s1").count(), 1)

    def test_existing_account_with_same_email_is_linked(self):
        teacher = self._teacher_with_classes()
        existing = AIDLUser.objects.create(microsoft_id="web:ravi", email="ravi@school.edu", provider="website")
        self._choose()
        existing.refresh_from_db()
        self.assertEqual(existing.organization_id, teacher.organization_id)
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:s1").exists())

    def test_student_in_another_organization_is_left_there(self):
        other = Organization.objects.create(name="Other", slug="other")
        AIDLUser.objects.create(microsoft_id="google:s1", email="ravi@school.edu", organization_id=str(other.pk))
        self._teacher_with_classes()
        resp = self._choose()
        self.assertIn("already+in+another", resp["Location"])
        self.assertEqual(AIDLUser.objects.get(microsoft_id="google:s1").organization_id, str(other.pk))

    def test_no_free_seats(self):
        self._teacher_with_classes()
        with mock.patch("api.plans.can_join", return_value=False):
            resp = self._choose()
        self.assertIn("no+free+seats", resp["Location"])
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:s1").exists())
        self.assertContains(self.client.get("/api/google-classroom/classes/"), "No free seat")

    def test_refresh_adds_new_students_of_chosen_classes_only(self):
        self._teacher_with_classes()
        self._choose("c1")
        self.fake.students["c1"].append({"userId": "s3", "profile": {"name": {"fullName": "New Kid"}}})
        self.fake.students["c2"].append({"userId": "s4", "profile": {"name": {"fullName": "Robo Kid"}}})
        with mock.patch("integrations.google_classroom.google_oauth.refresh_access_token", return_value="a2"):
            self.client.post("/api/google-classroom/classes/sync/")
        self.assertTrue(AIDLUser.objects.filter(microsoft_id="google:s3").exists())
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:s4").exists())

    def test_removing_a_class_keeps_learners(self):
        self._teacher_with_classes()
        self._choose()
        resp = self._choose(on=False)
        self.assertIn("keep+their+AIDL+accounts", resp["Location"])
        self.assertTrue(AIDLUser.objects.filter(microsoft_id="google:s1", is_active=True).exists())
        self.assertNotContains(self.client.get("/api/google-classroom/classes/"), "✅ In AIDL")

    def test_cannot_choose_another_teachers_class(self):
        self._teacher_with_classes()
        self.client.post("/api/google-classroom/classes/signout/")
        self._sign_in(sub="333", email="t@other.edu", hd="other.edu")
        self.fake.courses = []
        self._connect(sub="333", email="t@other.edu", hd="other.edu")
        resp = self._choose("c1")
        self.assertIn("isn%27t+in+your+list", resp["Location"])
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:s1").exists())

    def test_json_choose(self):
        r = self._sign_in()
        self._connect()
        resp = self.client.post("/api/google-classroom/api/classes/choose/", {"course_id": "c1", "in_programme": True},
                                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {r['access_token']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["result"], {"added": 2})
        self.assertEqual(data["classes_in_programme"], 1)


for _name in [n for n in dir(GoogleClassroomClasses) if n.startswith("test_")]:
    setattr(GoogleClassroomProgramme, _name, None)


# --- step 4: the learner flow from the shared link ---

@mock.patch("api.card_image.warm")  # don't draw card images in the background
class GoogleClassroomLearners(GoogleClassroomProgramme):
    def _class_in_aidl(self):
        """Teacher signs up, connects Classroom, adds class c1 (students s1, s2)."""
        teacher = self._teacher_with_classes()
        self._choose("c1")
        return teacher

    def _start_programme(self):
        for item in ("traffic_light", "highway_code"):
            self.client.post("/api/google-classroom/classes/setup/", {"action": "approve", "item": item})
        return self.client.post("/api/google-classroom/classes/setup/", {"action": "start"}, follow=True)

    def _open_link(self, **token_args):
        """A student clicks the shared link and signs in with Google."""
        self.client.cookies.clear()
        resp = self.client.get("/api/google-classroom/learn/")
        self.assertEqual(resp.status_code, 302)
        q = {k: v[0] for k, v in parse_qs(urlparse(resp["Location"]).query).items()}
        self.assertEqual(q["scope"], "openid email profile")  # learners never get the Classroom scopes
        with mock.patch("integrations.google_classroom.google_oauth.exchange_code",
                        return_value=self._id_token(**token_args)):
            return self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": q["state"]},
                                   follow=True)

    def test_admin_center_shows_setup_and_share_link(self, _warm):
        self._class_in_aidl()
        page = self.client.get("/api/google-classroom/admin/")
        self.assertContains(page, "4 · Card approval")
        self.assertContains(page, "/api/google-classroom/learn/")

    def test_start_needs_both_cards_approved(self, _warm):
        self._class_in_aidl()
        resp = self.client.post("/api/google-classroom/classes/setup/", {"action": "start"}, follow=True)
        self.assertContains(resp, "Approve the Traffic Light Check and the Highway Code first")

    def test_learner_waits_until_teacher_starts(self, _warm):
        self._class_in_aidl()
        resp = self._open_link(sub="s1", email="ravi@school.edu")
        self.assertContains(resp, "Nothing to do yet")

    def test_full_flow_to_licence(self, _warm):
        self._class_in_aidl()
        self.assertContains(self._start_programme(), "The programme has started")
        resp = self._open_link(sub="s1", email="ravi@school.edu")
        self.assertContains(resp, "Let's go")

        self.client.post("/api/google-classroom/learn/action/", {"action": "start"})
        self.assertContains(self.client.get("/api/google-classroom/learn/"), "Traffic Light Check")
        self.client.post("/api/google-classroom/learn/action/", {"action": "traffic_light"})
        self.assertContains(self.client.get("/api/google-classroom/learn/"), "Get my licence")
        self.client.post("/api/google-classroom/learn/action/", {"action": "highway_code"})

        student = AIDLUser.objects.get(microsoft_id="google:s1")
        self.assertTrue(student.licence_issued)
        self.assertContains(self.client.get("/api/google-classroom/learn/"), "Create my avatar")
        student.avatar_data = "iVBORw0KGgo="
        student.save(update_fields=["avatar_data"])
        self.assertContains(self.client.get("/api/google-classroom/learn/"), student.licence_number)

    def test_cards_must_be_done_in_order(self, _warm):
        self._class_in_aidl()
        self._start_programme()
        self._open_link(sub="s1", email="ravi@school.edu")
        self.client.post("/api/google-classroom/learn/action/", {"action": "start"})
        self.client.post("/api/google-classroom/learn/action/", {"action": "highway_code"})  # skipping is ignored
        self.client.post("/api/google-classroom/learn/action/", {"action": "traffic_light"})
        self.assertFalse(AIDLUser.objects.get(microsoft_id="google:s1").licence_issued)

    def test_student_of_a_class_not_in_aidl_is_turned_away(self, _warm):
        self._class_in_aidl()
        self._start_programme()
        resp = self._open_link(sub="s9", email="nobody@school.edu")
        self.assertContains(resp, "added your Google Classroom class to AIDL yet", status_code=403)
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:s9").exists())

    def test_student_cant_use_teacher_buttons(self, _warm):
        self._class_in_aidl()
        self._open_link(sub="s1", email="ravi@school.edu")
        resp = self.client.post("/api/google-classroom/classes/setup/", {"action": "start"})
        self.assertContains(resp, "Only AIDL admins", status_code=403)

    def test_teacher_opening_the_link_goes_to_the_admin_center(self, _warm):
        self._class_in_aidl()
        resp = self._open_link()  # sub 111 = the teacher
        self.assertContains(resp, "Admin Center")
        self.assertContains(resp, "5 · Start the programme")


for _name in [n for n in dir(GoogleClassroomProgramme) if n.startswith("test_")]:
    setattr(GoogleClassroomLearners, _name, None)


# --- step 5: Admin Center (core) ---

class GoogleClassroomAdminCenter(GoogleClassroomLearners):
    def setUp(self):
        super().setUp()
        warm = mock.patch("api.card_image.warm")  # in setUp: a class decorator would patch the inherited tests twice
        warm.start()
        self.addCleanup(warm.stop)
        from django.conf import settings
        settings.PRIMARY_ADMIN_CACHE = False  # admins change within one test
        self.addCleanup(setattr, settings, "PRIMARY_ADMIN_CACHE", True)

    def _school(self):
        """Teacher (master admin) with class c1 in AIDL: learners Ravi (s1) and Meera (s2)."""
        self._class_in_aidl()
        return (AIDLUser.objects.get(microsoft_id="google:111"), AIDLUser.objects.get(microsoft_id="google:s1"),
                AIDLUser.objects.get(microsoft_id="google:s2"))

    def _as(self, sub, email):
        """Sign in to the Admin Center as this Google account (through the student link)."""
        return self._open_link(sub=sub, email=email)

    def test_home_shows_steps_plan_and_team(self):
        self._school()
        page = self.client.get("/api/google-classroom/admin/")
        self.assertContains(page, "👑 Master admin")
        self.assertContains(page, "2 learners from 1 class in AIDL")
        self.assertContains(page, "✅ 3 · Your team")
        self.assertContains(page, "seats")

    def test_start_from_the_admin_center(self):
        self._school()
        for item in ("traffic_light", "highway_code"):
            self.client.post("/api/google-classroom/admin/action/", {"action": "approve", "item": item})
        resp = self.client.post("/api/google-classroom/admin/action/", {"action": "start"}, follow=True)
        self.assertContains(resp, "The programme has started")
        self.assertContains(resp, "✅ 5 · Programme started")

    def test_learner_cannot_open_the_admin_center(self):
        self._school()
        self._as("s1", "ravi@school.edu")
        resp = self.client.get("/api/google-classroom/admin/")
        self.assertContains(resp, "Admins only", status_code=403)

    def test_not_signed_in_goes_to_google(self):
        resp = self.client.get("/api/google-classroom/admin/team/")
        self.assertRedirects(resp, "/api/google-classroom/learn/", fetch_redirect_response=False)

    def test_team_page_lists_learners_with_their_class(self):
        self._school()
        page = self.client.get("/api/google-classroom/admin/team/")
        self.assertContains(page, "Ravi Kumar")
        self.assertContains(page, "AI Basics")
        self.assertContains(page, "Not started")

    def test_remove_and_add_back_a_learner(self):
        _t, ravi, _m = self._school()
        resp = self.client.post("/api/google-classroom/admin/team/action/",
                                {"action": "remove", "user_id": ravi.pk}, follow=True)
        self.assertContains(resp, "Ravi Kumar was removed")
        ravi.refresh_from_db()
        self.assertIsNotNone(ravi.slack_left_at)
        self.assertContains(self.client.get("/api/google-classroom/classes/"), "Removed by an admin")
        # Refresh doesn't quietly put them back.
        with mock.patch("integrations.google_classroom.google_oauth.refresh_access_token", return_value="a2"):
            self.client.post("/api/google-classroom/classes/sync/")
        ravi.refresh_from_db()
        self.assertIsNotNone(ravi.slack_left_at)

        resp = self.client.post("/api/google-classroom/admin/team/action/",
                                {"action": "restore", "user_id": ravi.pk}, follow=True)
        self.assertContains(resp, "back in the programme")
        ravi.refresh_from_db()
        self.assertIsNone(ravi.slack_left_at)

    def test_removed_learner_is_blocked_in_the_flow(self):
        _t, ravi, _m = self._school()
        self.client.post("/api/google-classroom/admin/team/action/", {"action": "remove", "user_id": ravi.pk})
        resp = self._as("s1", "ravi@school.edu")
        self.assertContains(resp, "no longer in the programme")

    def test_make_admin_with_permissions_then_remove(self):
        _t, ravi, _m = self._school()
        resp = self.client.post("/api/google-classroom/admin/admins/action/",
                                {"action": "add", "user_id": ravi.pk, "perm": ["approve_cards"]}, follow=True)
        self.assertContains(resp, "is now an AIDL admin")
        ravi.refresh_from_db()
        self.assertEqual(ravi.role, "admin")
        self.assertTrue(ravi.perm_approve_cards)
        self.assertFalse(ravi.perm_manage_admins)
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "✅ 2 · Admins")

        resp = self.client.post("/api/google-classroom/admin/admins/action/",
                                {"action": "remove", "user_id": ravi.pk}, follow=True)
        self.assertContains(resp, "is no longer an admin")
        ravi.refresh_from_db()
        self.assertEqual(ravi.role, "learner")
        self.assertFalse(ravi.perm_approve_cards)

    def test_master_admin_cannot_be_removed(self):
        teacher, ravi, _m = self._school()
        self.client.post("/api/google-classroom/admin/admins/action/",
                         {"action": "add", "user_id": ravi.pk, "perm": ["manage_admins"]})
        self._as("s1", "ravi@school.edu")  # Ravi is an admin now
        resp = self.client.post("/api/google-classroom/admin/admins/action/",
                                {"action": "remove", "user_id": teacher.pk}, follow=True)
        self.assertContains(resp, "The master admin can")
        teacher.refresh_from_db()
        self.assertEqual(teacher.role, "admin")

    def test_admin_cannot_give_permissions_they_lack(self):
        _t, ravi, meera = self._school()
        self.client.post("/api/google-classroom/admin/admins/action/",
                         {"action": "add", "user_id": ravi.pk, "perm": ["manage_admins"]})
        self._as("s1", "ravi@school.edu")
        self.client.post("/api/google-classroom/admin/admins/action/",
                         {"action": "add", "user_id": meera.pk, "perm": ["manage_admins", "billing"]})
        meera.refresh_from_db()
        self.assertEqual(meera.role, "admin")
        self.assertTrue(meera.perm_manage_admins)
        self.assertFalse(meera.perm_billing)  # Ravi doesn't have billing

    def test_admin_without_manage_admins_cannot_add(self):
        _t, ravi, meera = self._school()
        self.client.post("/api/google-classroom/admin/admins/action/", {"action": "add", "user_id": ravi.pk})
        self._as("s1", "ravi@school.edu")
        resp = self.client.post("/api/google-classroom/admin/admins/action/",
                                {"action": "add", "user_id": meera.pk}, follow=True)
        self.assertContains(resp, "Only the master admin")
        meera.refresh_from_db()
        self.assertEqual(meera.role, "learner")

    def test_admin_seat_limit(self):
        _t, ravi, meera = self._school()
        Organization.objects.filter(pk=ravi.organization_id).update(admin_seat_limit=2)
        self.client.post("/api/google-classroom/admin/admins/action/", {"action": "add", "user_id": ravi.pk})
        resp = self.client.post("/api/google-classroom/admin/admins/action/",
                                {"action": "add", "user_id": meera.pk}, follow=True)
        self.assertContains(resp, "allows 2 admins")

    def test_admin_without_card_permission_cannot_start(self):
        _t, ravi, _m = self._school()
        self.client.post("/api/google-classroom/admin/admins/action/", {"action": "add", "user_id": ravi.pk})
        self._as("s1", "ravi@school.edu")
        resp = self.client.post("/api/google-classroom/admin/action/",
                                {"action": "approve", "item": "traffic_light"}, follow=True)
        self.assertContains(resp, "don&#x27;t include approving cards")

    def test_plans_link_and_coverage(self):
        self._school()
        resp = self.client.get("/api/google-classroom/admin/plans/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/plans?token=", resp["Location"])
        csv = self.client.get("/api/google-classroom/admin/coverage.csv")
        self.assertEqual(csv["Content-Type"], "text/csv")
        self.assertIn("Ravi Kumar", csv.content.decode())


for _name in [n for n in dir(GoogleClassroomLearners) if n.startswith("test_")]:
    setattr(GoogleClassroomAdminCenter, _name, None)


# --- step 6: Admin Center, AI policy ---

class GoogleClassroomPolicy(GoogleClassroomAdminCenter):
    def _company(self):
        from api import regulators
        return self.client.post("/api/google-classroom/admin/policy/",
                                {"countries": [regulators.COUNTRIES[0]], "industry": regulators.INDUSTRIES[0]},
                                follow=True)

    def test_policy_asks_for_company_first(self):
        self._school()
        page = self.client.get("/api/google-classroom/admin/policy/")
        self.assertContains(page, "Where do you operate?")
        self.assertContains(page, "from Google")

    def test_company_then_topics_and_regulators(self):
        self._school()
        resp = self._company()
        self.assertContains(resp, "Add a policy card")
        self.assertContains(resp, "0 of 14 approved")
        self.assertEqual(self.client.get("/api/google-classroom/admin/regulators/").status_code, 200)

    def test_policy_card_page_and_incomplete_answers(self):
        from api import policy_cards as pc
        self._school()
        self._company()
        n = pc.TOPICS[0]
        page = self.client.get(f"/api/google-classroom/admin/policy/{n}/")
        self.assertContains(page, pc.SHORT[n])
        resp = self.client.post(f"/api/google-classroom/admin/policy/{n}/", {"action": "approve", "when": "today"})
        self.assertContains(resp, "Please check")

    def test_approved_card_reaches_the_learner_flow(self):
        from api import policy_cards as pc
        from api import aup_store
        teacher, _r, _m = self._school()
        self._start_programme()
        org = Organization.objects.get(pk=teacher.organization_id)
        pc.approve(org, teacher, 1, {}, card_lines=["Use only the AI tools on the school's list."])
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "1 of 14 policy cards approved")

        self._open_link(sub="s1", email="ravi@school.edu")
        self.client.post("/api/google-classroom/learn/action/", {"action": "start"})
        page = self.client.get("/api/google-classroom/learn/")
        self.assertContains(page, pc.SHORT[1])
        version = aup_store.required_points(org)[1]
        self.client.post("/api/google-classroom/learn/action/", {"action": "agree", "n": "1", "version": version})
        ravi = AIDLUser.objects.get(microsoft_id="google:s1")
        self.assertEqual(aup_store.pending_points(ravi, org), [])

    def test_upload_and_logo_are_paid_features(self):
        self._school()
        self.assertContains(self.client.get("/api/google-classroom/admin/upload/"), "A paid feature")
        self.assertContains(self.client.get("/api/google-classroom/admin/logo/"), "A paid feature")

    def test_logo_on_a_paid_plan(self):
        import io
        from PIL import Image
        teacher, _r, _m = self._school()
        Organization.objects.filter(pk=teacher.organization_id).update(plan="basic")
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), "red").save(buf, format="PNG")
        buf.seek(0)
        buf.name = "logo.png"
        resp = self.client.post("/api/google-classroom/admin/logo/", {"file": buf}, follow=True)
        self.assertContains(resp, "Your logo is saved")
        self.assertTrue(Organization.objects.get(pk=teacher.organization_id).logo_data)

    def test_add_an_ai_app(self):
        from api.slack_cards import AI_CATEGORIES, DATA_ALLOWED
        self._school()
        resp = self.client.post("/api/google-classroom/admin/apps/", {
            "kind": "ai", "name": "Gemini for Education", "category": AI_CATEGORIES[0],
            "data": DATA_ALLOWED[0][0], "status": "Approved"}, follow=True)
        self.assertContains(resp, "Gemini for Education")

    def test_admin_without_policy_permission_is_refused(self):
        _t, ravi, _m = self._school()
        self.client.post("/api/google-classroom/admin/admins/action/", {"action": "add", "user_id": ravi.pk})
        self._as("s1", "ravi@school.edu")
        self.assertContains(self.client.get("/api/google-classroom/admin/policy/"),
                            "permissions don", status_code=403)
        self.assertContains(self.client.get("/api/google-classroom/admin/apps/"), "permissions don", status_code=403)

    def test_admin_with_apps_permission_can_open_policy(self):
        _t, ravi, _m = self._school()
        self.client.post("/api/google-classroom/admin/admins/action/",
                         {"action": "add", "user_id": ravi.pk, "perm": ["approve_apps"]})
        self._as("s1", "ravi@school.edu")
        self.assertEqual(self.client.get("/api/google-classroom/admin/policy/").status_code, 200)


for _name in [n for n in dir(GoogleClassroomAdminCenter) if n.startswith("test_")]:
    setattr(GoogleClassroomPolicy, _name, None)

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

    def _sign_in(self, enroll_as="organization", granted="openid email profile", **token_args):
        """Full round trip → the frontend redirect's query params. granted = the
        scopes the person ticked on Google's consent screen."""
        state = self._state(enroll_as)
        tokens = {"id_token": self._id_token(**token_args), "access_token": "access-1", "scope": granted,
                  "refresh_token": "refresh-1"}
        with mock.patch("integrations.google_classroom.google_oauth.exchange_tokens", return_value=tokens):
            back = self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": state})
        self.assertEqual(back.status_code, 302)
        url = urlparse(back["Location"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", FRONTEND)
        return {k: v[0] for k, v in parse_qs(url.query).items()}

    def _details_done(self, students="200"):
        """Every school has done setup step 1 (onboarding: roughly how many students), as a
        school that signed up has; tests of the Admin Center itself start from there."""
        from integrations.google_classroom.models import SchoolSetup

        for org_id in Organization.objects.values_list("pk", flat=True):
            SchoolSetup.objects.update_or_create(organization_id=str(org_id), defaults={"approx_students": students})

    # --- sign-up ---

    def test_first_teacher_creates_the_school_and_is_its_admin(self):
        r = self._sign_in()
        self.assertEqual(r["mode"], "google_classroom")
        user = AIDLUser.objects.get(pk=decode_token(r["access_token"])["sub"])
        self.assertEqual((user.microsoft_id, user.provider, user.role), ("google:111", "google_classroom", "admin"))
        org = Organization.objects.get(pk=user.organization_id)
        self.assertEqual((org.name, org.owner_user_id), ("school.edu", str(user.pk)))
        self.assertEqual(GoogleWorkspace.objects.get(key="domain:school.edu").organization_id, str(org.pk))

    def test_school_sign_in_asks_for_classroom_permissions_individual_does_not(self):
        for enroll_as, wanted in (("organization", True), ("individual", False)):
            resp = self.client.get("/api/google-classroom/login/", {"enroll_as": enroll_as})
            scope = parse_qs(urlparse(resp.json()["auth_url"]).query)["scope"][0]
            self.assertEqual("auth/classroom.courses " in f"{scope} ", wanted)
            self.assertEqual("classroom.announcements" in scope, wanted)

    def test_admin_gets_the_google_classroom_link_and_is_signed_in_to_admin_center(self):
        r = self._sign_in()
        self.assertTrue(r["admin_center_url"].startswith("https://backend.test/api/google-classroom/open/?email="))
        self.assertEqual(r["open_admin_center"], "1")
        # A new school: that link opens the school's short setup first.
        go = self.client.get(r["admin_center_url"].removeprefix("https://backend.test"))
        self.assertEqual(go["Location"], "/api/google-classroom/admin/setup/")
        # Once the school details are in (no AIDL class yet): Google Classroom, as the signed-in account.
        self._details_done()
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
        tokens = {"id_token": self._id_token(), "access_token": "a", "scope": "openid email profile"}
        with mock.patch("integrations.google_classroom.google_oauth.exchange_tokens", return_value=tokens):
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
        self.announcements = {}  # id -> text
        self.materials = {}  # id -> Classwork material (the 5 teacher-only steps)
        self.made = 0
        self.topics = []
        self.coursework = {}  # id -> body (the learners' cards)
        self.submissions = {}  # coursework id -> [studentSubmission]
        self.calls = []  # (method, url) of every write
        self.fail_writes = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.tokens_seen.append(headers["Authorization"])
        resp = mock.Mock(ok=True, status_code=200)
        if url.endswith("/courses"):
            assert params["teacherId"] == "me"
            resp.json.return_value = {"courses": self.courses}
        elif url.endswith("/courseWork"):
            resp.json.return_value = {"courseWork": [{"id": i, "associatedWithDeveloper": True, **c}
                                                     for i, c in self.coursework.items()]}
        elif url.endswith("/studentSubmissions"):
            resp.json.return_value = {"studentSubmissions": self.submissions.get(url.split("/")[-2], [])}
        elif url.endswith("/topics"):
            resp.json.return_value = {"topic": self.topics}
        elif url.endswith("/courseWorkMaterials"):
            # Like Google: materials don't say which app made them.
            resp.json.return_value = {"courseWorkMaterial": [{"id": i, **m} for i, m in self.materials.items()]}
        elif url.endswith("/announcements"):  # the teacher's own posts are not AIDL's
            resp.json.return_value = {"announcements": [
                {"id": i, "text": t, "associatedWithDeveloper": not i.startswith("own")}
                for i, t in self.announcements.items()]}
        elif url.endswith("/teachers"):  # the class owner, plus any co-teachers
            resp.json.return_value = {"teachers": getattr(self, "teachers", {}).get(url.split("/")[-2],
                                                                                   [{"userId": "owner"}])}
        elif url.endswith("/invitations"):
            resp.json.return_value = {"invitations": [i for i in getattr(self, "invitations", [])
                                                      if i["courseId"] == params["courseId"]]}
        elif "/userProfiles/" in url:
            resp.json.return_value = self.profiles[url.split("/")[-1]]
        elif url.split("/")[-2] == "courses":  # one course
            course = next((c for c in self.courses if c["id"] == url.split("/")[-1]), None)
            if course is None:
                resp.ok, resp.status_code = False, 404
                resp.json.return_value = {"error": {"status": "NOT_FOUND", "message": "not found"}}
            else:
                resp.json.return_value = course
        else:
            resp.json.return_value = {"students": self.students[url.split("/")[-2]]}
        return resp

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, data=None):
        self.calls.append((method, url))
        if method == "POST" and url.endswith(("/courseWork", "/courseWorkMaterials")) and not self.fail_writes:
            self.created = getattr(self, "created", []) + [json["title"]]  # Classwork, oldest first
        resp = mock.Mock(ok=True, status_code=200)
        if self.fail_writes:
            resp.ok, resp.status_code = False, 403
            resp.json.return_value = {"error": {"status": "PERMISSION_DENIED", "message": "not allowed"}}
        elif method == "POST" and url.endswith("/courses"):
            new_id = f"aidl{len(self.courses)}"
            course = {**json, "id": new_id, "courseState": json.get("courseState", "PROVISIONED"),
                      "alternateLink": f"https://classroom.google.com/c/{new_id}"}
            course.pop("ownerId")
            self.courses.append(course)
            self.students[new_id] = []
            resp.json.return_value = course
        elif url.startswith("https://www.googleapis.com/upload/drive"):
            self.uploads = getattr(self, "uploads", []) + [data]
            resp.json.return_value = {"id": f"f{len(self.uploads)}"}
        elif url.endswith("/permissions"):
            self.shared = getattr(self, "shared", []) + [(url.split("/")[-2], json["emailAddress"])]
            resp.json.return_value = {}
        elif method == "POST" and url.endswith(":modifyAssignees"):
            work = self.coursework[url.split("/")[-1].split(":")[0]]
            ids = set((work.get("individualStudentsOptions") or {}).get("studentIds") or []) \
                if work.get("assigneeMode") == "INDIVIDUAL_STUDENTS" else set()
            opts = json["modifyIndividualStudentsOptions"]
            ids = (ids | set(opts.get("addStudentIds", []))) - set(opts.get("removeStudentIds", []))
            work.update(assigneeMode="INDIVIDUAL_STUDENTS", individualStudentsOptions={"studentIds": sorted(ids)})
            resp.json.return_value = work
        elif method == "POST" and url.endswith("/courseWork"):
            self.made += 1
            cid = f"cw{self.made}"
            self.coursework[cid] = dict(json)
            resp.json.return_value = {"id": cid, **json}
        elif method == "DELETE" and "/students/" in url:
            course, gid = url.split("/")[-3], url.split("/")[-1]
            self.students[course] = [s for s in self.students[course] if s["userId"] != gid]
            self.kicked = getattr(self, "kicked", []) + [gid]
            resp.json.return_value = {}
        elif method == "DELETE" and "/topics/" in url:
            self.topics = [t for t in self.topics if t["topicId"] != url.split("/")[-1]]
            resp.json.return_value = {}
        elif method == "POST" and url.endswith("/invitations"):
            self.invitations = getattr(self, "invitations", [])
            members = {x["userId"] for x in self.students.get(json["courseId"], [])}
            if json["userId"] in members or any(i["userId"] == json["userId"] for i in self.invitations):
                resp.ok, resp.status_code = False, 409
                resp.json.return_value = {"error": {"status": "ALREADY_EXISTS", "message": "exists"}}
            elif json["userId"].startswith("outsider"):
                resp.ok, resp.status_code = False, 403
                resp.json.return_value = {"error": {"status": "PERMISSION_DENIED", "message": "not in domain"}}
            else:
                invite = {"id": f"inv{len(self.invitations) + 1}", **json}
                self.invitations.append(invite)
                resp.json.return_value = invite
        elif method == "DELETE" and "/invitations/" in url:
            self.invitations = [i for i in self.invitations if i["id"] != url.split("/")[-1]]
            resp.json.return_value = {}
        elif method == "DELETE" and "/courseWorkMaterials/" in url:
            self.materials.pop(url.split("/")[-1], None)
            resp.json.return_value = {}
        elif method == "DELETE" and "/courseWork/" in url:
            self.coursework.pop(url.split("/")[-1], None)
            resp.json.return_value = {}
        elif method == "POST" and url.endswith("/topics"):
            topic = {"topicId": f"t{len(self.topics) + 1}", "name": json["name"]}
            self.topics.append(topic)
            resp.json.return_value = topic
        elif method == "POST" and url.endswith("/courseWorkMaterials"):
            self.made += 1
            mid = f"m{self.made}"
            self.materials[mid] = dict(json)
            if json["title"] == "🪪 Your AI Driving Licence":  # the licence: a Classwork item for one student
                self.licence_items = getattr(self, "licence_items", []) + [
                    (json["individualStudentsOptions"]["studentIds"], json["description"], json.get("topicId"))]
                self.licence_materials = getattr(self, "licence_materials", []) + [json.get("materials")]
            resp.json.return_value = {"id": mid, **json}
        elif method == "PATCH" and "/courseWork/" in url:
            self.coursework[url.split("/")[-1]].update(json)
            resp.json.return_value = self.coursework[url.split("/")[-1]]
        elif method == "PATCH" and "/courseWorkMaterials/" in url:
            mid = url.split("/")[-1]
            if mid not in self.materials:
                resp.ok, resp.status_code = False, 404
                resp.json.return_value = {"error": {"status": "NOT_FOUND", "message": "not found"}}
            else:
                self.materials[mid].update(json)
                resp.json.return_value = self.materials[mid]
        elif method == "POST" and url.endswith("/announcements"):
            aid = f"ann{len(self.announcements) + 1}"
            self.announcements[aid] = json["text"]
            if json.get("assigneeMode") == "INDIVIDUAL_STUDENTS":
                self.private = getattr(self, "private", []) + [(json["individualStudentsOptions"]["studentIds"],
                                                                 json["text"])]
                self.private_materials = getattr(self, "private_materials", []) + [json.get("materials")]
            resp.json.return_value = {"id": aid, **json}
        elif method == "DELETE" and "/announcements/" in url:
            self.announcements.pop(url.split("/")[-1], None)
            resp.json.return_value = {}
        elif method == "PATCH":
            aid = url.split("/")[-1]
            self.announcements[aid] = json["text"]
            resp.json.return_value = {"id": aid, **json}
        return resp


class GoogleClassroomClasses(GoogleClassroomSignIn):
    """Inherits the fake Google from the sign-in tests (but not their tests)."""

    def setUp(self):
        super().setUp()
        self.fake = FakeClassroom()
        for name in ("get", "request"):
            patcher = mock.patch(f"integrations.google_classroom.classroom_api.requests.{name}",
                                 side_effect=getattr(self.fake, name))
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


ALL_SCOPES = " ".join([SCOPES, "https://www.googleapis.com/auth/classroom.courses",
                        "https://www.googleapis.com/auth/classroom.announcements",
                        "https://www.googleapis.com/auth/classroom.courseworkmaterials",
                        "https://www.googleapis.com/auth/classroom.topics",
                        "https://www.googleapis.com/auth/classroom.coursework.students"])


@override_settings(GOOGLE_CLASSROOM_SYNC=True)
@mock.patch("api.card_image.warm", new=mock.Mock())
class GoogleClassroomAidlClass(GoogleClassroomClasses):
    """Sign-up creates the AIDL class (like Slack's #aidl) with the Admin Center post."""

    def test_sign_up_creates_the_aidl_class_and_posts_the_admin_center(self):
        from integrations.google_classroom.models import AidlClass, GoogleCourse

        self._sign_in(granted=ALL_SCOPES)
        row = AidlClass.objects.get()
        self.assertEqual((row.course_id, row.state, row.error), ("aidl2", "ACTIVE", ""))
        created = [c for c in self.fake.courses if c["id"] == "aidl2"][0]
        self.assertEqual((created["name"], created["section"]), ("AIDL", "AI Driving Licence"))
        # Nothing on the Stream (Classroom shows teachers every Stream post), and no students'
        # topic or welcome until a student joins: only the Admin Center.
        self.assertEqual(self.fake.announcements, {})
        # The 2 setup steps are teacher-only drafts in Classwork, step 1 created last (= on top),
        # then the setup summary above them.
        titles = [m["title"] for m in self.fake.materials.values()]
        self.assertEqual(titles, ["⬜ Step 2 · Your AI policy", "👉 Step 1 · School details",
                                  "⭐ Plan: Trial · 1/2 teachers · 0/5 students", "📋 Setup progress · 0 of 2 done"])
        plan = self.fake.materials["m3"]
        self.assertEqual(plan["state"], "DRAFT")
        self.assertEqual(plan["materials"][0]["link"]["url"], "https://backend.test/api/google-classroom/admin/upgrade/")
        self.assertIn("👉 Trial (free): 2 teachers, 5 students", plan["description"])
        summary = self.fake.materials["m4"]["description"]
        self.assertIn("👉 1. School details", summary)
        self.assertIn("Next: open", summary)
        self.assertTrue(all(m["state"] == "DRAFT" for m in self.fake.materials.values()))
        # ... all under the "AIDL Admin Center" topic, made once.
        self.assertEqual(self.fake.topics, [{"topicId": "t1", "name": "🚦 AIDL Admin Center"}])
        self.assertTrue(all(m["topicId"] == "t1" for m in self.fake.materials.values()))
        self.assertEqual(self.fake.materials["m2"]["materials"][0]["link"]["url"],
                         "https://backend.test/api/google-classroom/admin/step/1/")
        # Once the school details are in, Continue on the website opens the AIDL class itself.
        self._details_done()
        r = self._sign_in(granted=ALL_SCOPES)
        go = self.client.get(r["admin_center_url"].removeprefix("https://backend.test"))
        self.assertEqual(go["Location"], "https://classroom.google.com/c/aidl2?authuser=asha%40school.edu")
        # Its classes were read, and the AIDL class is in the programme.
        self.assertEqual(GoogleCourse.objects.count(), 3)
        self.assertTrue(GoogleCourse.objects.get(course_id="aidl2").in_programme)
        self.assertFalse(GoogleCourse.objects.get(course_id="c1").in_programme)
        # Step 3 shows only the AIDL class as the programme; other classes can't be added,
        # only their students invited to the AIDL class.
        classes_page = self.client.get("/api/google-classroom/classes/")
        self.assertContains(classes_page, "AI Driving Licence")
        self.assertEqual([c["name"] for c in classes_page.context["data"]["classes"]], ["AIDL"])
        self.assertIn("AI Basics", [c["name"] for c in classes_page.context["other_classes"]])
        resp = self.client.post("/api/google-classroom/classes/choose/", {"course_id": "c1", "on": "1"})
        self.assertIn("Only+your+AIDL+class", resp["Location"])
        self.assertFalse(GoogleCourse.objects.get(course_id="c1").in_programme)
        page = self.client.get("/api/google-classroom/admin/")
        self.assertContains(page, "https://classroom.google.com/c/aidl2")
        self.assertContains(page, "Back to Google Classroom")

    def test_signing_in_again_reuses_the_class_and_updates_the_post(self):
        from integrations.google_classroom.models import AidlClass

        self._sign_in(granted=ALL_SCOPES)
        self._sign_in(granted=ALL_SCOPES)
        self.assertEqual(AidlClass.objects.get().course_id, "aidl2")
        self.assertEqual([c["name"] for c in self.fake.courses].count("AIDL"), 1)
        self.assertEqual([m for m, u in self.fake.calls if "announcements" in u], [])
        self.assertFalse([m for m in self.fake.materials.values() if m["title"].startswith("👋")])

    def test_steps_open_their_pages(self):
        self._sign_in(granted=ALL_SCOPES)
        for n, where in ((1, "/api/google-classroom/admin/setup/"), (2, "/api/google-classroom/admin/setup/policy/")):
            resp = self.client.get(f"/api/google-classroom/admin/step/{n}/")
            self.assertEqual(resp["Location"], where)
        self.assertContains(self.client.get("/api/google-classroom/admin/setup/"), "About how many students?")
        # A step cookie left from when there were 5 steps is ignored.
        self.client.cookies["aidl_gclassroom_step"] = "4"
        self.assertContains(self.client.get("/api/google-classroom/admin/setup/"), "About how many students?")

    def test_an_admin_action_updates_classroom_right_away(self):
        self._sign_in(granted=ALL_SCOPES)
        with mock.patch("integrations.google_classroom.google_oauth.refresh_access_token", return_value="access-9"):
            self.client.post("/api/google-classroom/admin/setup/", {"name": "Green Valley School", "students": "200"})
        titles = {m["title"] for m in self.fake.materials.values()}
        self.assertIn("✅ Step 1 · School details", titles)
        self.assertIn("👉 Step 2 · Your AI policy", titles)
        self.assertIn("✅ 1. School details", self.fake.materials["m4"]["description"])
        self.assertEqual(self.fake.materials["m4"]["title"], "📋 Setup progress · 1 of 2 done")
        self.assertEqual(len(self.fake.materials), 4)  # updated, not added again

    def test_signing_in_again_keeps_two_steps(self):
        self._sign_in(granted=ALL_SCOPES)
        self._sign_in(granted=ALL_SCOPES)
        self.assertEqual(len(self.fake.materials), 4)
        self.assertEqual(len(self.fake.topics), 1)
        del self.fake.materials["m1"]  # a teacher deleted step 2
        self._sign_in(granted=ALL_SCOPES)
        self.assertEqual(len(self.fake.materials), 4)

    def test_a_class_with_the_old_five_steps_gets_the_new_two(self):
        from integrations.google_classroom.models import AidlClass

        self._sign_in(granted=ALL_SCOPES)
        for n in range(1, 6):  # a class set up before: 5 steps, no version
            self.fake.materials[f"old{n}"] = {"title": f"⬜ Step {n} · Old step", "state": "DRAFT"}
        row = AidlClass.objects.get()
        for k in ("1", "2"):  # the class only has the old steps
            del self.fake.materials[row.step_ids[k]]
        row.step_ids = {k: v for k, v in row.step_ids.items() if k not in ("1", "2", "steps_v")}
        row.step_ids.update({str(n): f"old{n}" for n in range(1, 6)})
        row.save()
        self._sign_in(granted=ALL_SCOPES)
        titles = [m["title"] for m in self.fake.materials.values()]
        self.assertFalse([t for t in titles if "Old step" in t])
        self.assertIn("👉 Step 1 · School details", titles)
        self.assertIn("⬜ Step 2 · Your AI policy", titles)
        self.assertEqual(len(self.fake.materials), 4)

    def test_old_admin_center_post_is_taken_off_the_stream(self):
        from integrations.google_classroom.models import AidlClass

        self._sign_in(granted=ALL_SCOPES)
        self.fake.announcements["old"] = "🚦 AIDL Admin Center ..."
        AidlClass.objects.update(announcement_id="old")
        self._sign_in(granted=ALL_SCOPES)
        self.assertNotIn("old", self.fake.announcements)
        self.assertEqual(AidlClass.objects.get().announcement_id, "")
        self.assertEqual(self.fake.announcements, {})

    def test_a_class_aidl_lost_track_of_starts_afresh(self):
        """The database was cleared and the teacher signs up again: the AIDL class is reused,
        without the old school's welcome, plan, steps, cards and licences."""
        from integrations.google_classroom.models import AidlClass

        self._sign_in(granted=ALL_SCOPES)
        self.fake.announcements["ann9"] = "🎉 Well done, Zoya! ... Licence number: ..."  # the old school's
        self.fake.announcements["own1"] = "Exam on Friday"  # the teacher's own post
        self.fake.coursework["cw9"] = {"title": "🚦 Traffic Light Check"}
        self.fake.materials["own2"] = {"title": "Reading list", "state": "PUBLISHED"}  # the teacher's own
        old = set(self.fake.materials) - {"own2"}
        AidlClass.objects.all().delete()
        self._sign_in(granted=ALL_SCOPES)
        self.assertEqual(AidlClass.objects.get().course_id, "aidl2")  # the same class
        self.assertFalse(old & set(self.fake.materials))
        self.assertIn("own2", self.fake.materials)
        self.assertEqual(len(self.fake.materials), 5)  # theirs + one plan, one summary, 2 steps
        self.assertEqual(self.fake.announcements, {"own1": "Exam on Friday"})
        self.assertEqual(self.fake.coursework, {})

    def test_deleted_class_is_made_again(self):
        self._sign_in(granted=ALL_SCOPES)
        self.fake.courses = [c for c in self.fake.courses if c["name"] != "AIDL"]
        self._sign_in(granted=ALL_SCOPES)
        self.assertEqual([m for m, u in self.fake.calls if u.endswith("/courses")], ["POST", "POST"])

    def test_failure_never_stops_the_sign_in_and_shows_in_admin_center(self):
        self.fake.fail_writes = True
        r = self._sign_in(granted=ALL_SCOPES)
        self.assertIn("access_token", r)
        self.assertEqual(r["open_admin_center"], "1")
        self._details_done()
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "set up the AIDL class")

    def test_without_the_permissions_admin_center_offers_to_create_it(self):
        from integrations.google_classroom.models import AidlClass

        self._sign_in()
        self.assertFalse(AidlClass.objects.exists())
        self.assertEqual(self.fake.calls, [])
        self._details_done()
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "Create AIDL class in Google Classroom")

    def test_learner_sign_in_creates_nothing(self):
        from integrations.google_classroom.models import AidlClass

        self._sign_in(granted=ALL_SCOPES)
        self._sign_in(sub="222", email="ravi@school.edu", granted=ALL_SCOPES)
        self.assertEqual(AidlClass.objects.count(), 1)
        self.assertEqual(len([u for m, u in self.fake.calls if u.endswith("/courses")]), 1)


class GoogleClassroomLearnerCards(GoogleClassroomAidlClass):
    """Students of the AIDL class: cards as Classroom questions, answers, licence."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("integrations.google_classroom.google_oauth.refresh_access_token", return_value="access-2")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _school_with_a_student(self):
        self._sign_in(granted=ALL_SCOPES)
        self._details_done()
        self.fake.students["aidl2"] = [{"userId": "s7", "profile": {"name": {"fullName": "Zoya Khan"},
                                                                   "emailAddress": "zoya@school.edu"}}]

    def _start(self):
        for item in ("traffic_light", "highway_code"):
            self.client.post("/api/google-classroom/admin/action/", {"action": "approve", "item": item})
        self.client.post("/api/google-classroom/admin/action/", {"action": "start"})

    def _save_avatar(self, microsoft_id):
        import base64
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (40, 40), "#2EC866").save(buf, "PNG")
        AIDLUser.objects.filter(microsoft_id=microsoft_id).update(
            avatar_data=base64.b64encode(buf.getvalue()).decode())

    def _answer(self, title, gid="s7"):
        cid = next(i for i, c in self.fake.coursework.items() if c["title"] == title)
        answer = self.fake.coursework[cid]["multipleChoiceQuestion"]["choices"][0]
        self.fake.submissions[cid] = [{"userId": gid, "state": "TURNED_IN",
                                       "multipleChoiceSubmission": {"answer": answer}}]

    def _sync(self):
        self.client.post("/api/google-classroom/classes/sync/")

    def test_joining_the_aidl_class_makes_a_learner(self):
        self._school_with_a_student()
        self._sync()
        zoya = AIDLUser.objects.get(microsoft_id="google:s7")
        self.assertEqual((zoya.role, zoya.full_name), ("learner", "Zoya Khan"))

    def test_no_cards_until_the_programme_starts(self):
        self._school_with_a_student()
        self._sync()
        self.assertEqual(self.fake.coursework, {})

    def test_cards_are_questions_in_classroom_then_the_licence(self):
        self._school_with_a_student()
        self._start()
        cards = list(self.fake.coursework.values())
        self.assertEqual([c["title"] for c in cards], ["📖 The Highway Code", "🚦 Traffic Light Check"])  # TL on top
        self.assertTrue(all(c["workType"] == "MULTIPLE_CHOICE_QUESTION" and c["state"] == "PUBLISHED"
                            for c in cards))
        self.assertIn({"topicId": "t2", "name": "🎓 Your AI Driving Licence"}, self.fake.topics)

        # Answered out of order: the Highway Code counts only after the Traffic Light Check.
        self._answer("📖 The Highway Code")
        self._sync()
        zoya = AIDLUser.objects.get(microsoft_id="google:s7")
        self.assertFalse(zoya.licence_issued)
        self._answer("🚦 Traffic Light Check")
        self._sync()
        zoya.refresh_from_db()
        self.assertTrue(zoya.licence_issued)
        (ids, text, topic), = self.fake.licence_items
        self.assertEqual((ids, topic), (["s7"], "t2"))  # in Classwork with the cards, not on the Stream
        self.assertIn(zoya.licence_number, text)
        self.assertIn("Only you and your teachers can see this", text)
        self.assertFalse(any("Licence number" in t for t in self.fake.announcements.values()))
        self._sync()  # posted once
        self.assertEqual(len(self.fake.licence_items), 1)
        self.assertEqual(len(self.fake.coursework), 2)  # published once

    def test_the_licence_post_has_the_licence_card_picture(self):
        self._sign_in(granted=f"{ALL_SCOPES} https://www.googleapis.com/auth/drive.file")
        self.fake.students["aidl2"] = [{"userId": "s7", "profile": {"name": {"fullName": "Zoya Khan"},
                                                                   "emailAddress": "zoya@school.edu"}}]
        self._start()
        self._answer("🚦 Traffic Light Check")
        self._answer("📖 The Highway Code")
        self._sync()  # the avatar first
        self._save_avatar("google:s7")
        self._sync()
        self.assertTrue(self.fake.uploads[0].startswith(b"--aidl-licence-boundary"))
        self.assertIn(b"\x89PNG", self.fake.uploads[0])
        self.assertEqual(self.fake.shared, [("f1", "zoya@school.edu")])
        self.assertEqual(self.fake.licence_materials[-1],
                         [{"driveFile": {"driveFile": {"id": "f1"}, "shareMode": "VIEW"}}])

    def test_a_new_google_name_is_followed_and_the_licence_posted_again(self):
        self._sign_in(granted=f"{ALL_SCOPES} https://www.googleapis.com/auth/drive.file")
        self.fake.students["aidl2"] = [{"userId": "s7", "profile": {"name": {"fullName": "Zoyakhan"},
                                                                   "emailAddress": "zoya@school.edu"}}]
        self._start()
        self._answer("🚦 Traffic Light Check")
        self._answer("📖 The Highway Code")
        self._sync()  # the avatar first
        self._save_avatar("google:s7")
        self._sync()
        self.assertEqual(len(self.fake.uploads), 1)
        self.fake.students["aidl2"][0]["profile"]["name"]["fullName"] = "Zoya Khan"
        self._sync()
        zoya = AIDLUser.objects.get(microsoft_id="google:s7")
        self.assertEqual((zoya.first_name, zoya.last_name, zoya.full_name), ("Zoya", "Khan", "Zoya Khan"))
        self.assertEqual(len(self.fake.uploads), 2)  # a new licence picture
        self.assertEqual(len(self.fake.licence_items), 2)
        self.assertEqual(sum(1 for m in self.fake.materials.values() if "Licence number" in m["description"]), 1)
        self._sync()  # once
        self.assertEqual(len(self.fake.uploads), 2)

    def test_avatar_like_slack_then_the_licence_card_is_drawn_again(self):
        import base64
        import io

        from PIL import Image

        self._sign_in(granted=f"{ALL_SCOPES} https://www.googleapis.com/auth/drive.file")
        self.fake.students["aidl2"] = [{"userId": "s7", "profile": {"name": {"fullName": "Zoya Khan"},
                                                                   "emailAddress": "zoya@school.edu"}}]
        self._start()
        self._answer("🚦 Traffic Light Check")
        self._answer("📖 The Highway Code")
        self._sync()
        zoya = AIDLUser.objects.get(microsoft_id="google:s7")
        (ask,) = [m for m in self.fake.materials.values() if m["title"] == "🧑 Create your licence avatar"]
        self.assertEqual((ask["assigneeMode"], ask["individualStudentsOptions"]), ("INDIVIDUAL_STUDENTS",
                                                                                  {"studentIds": ["s7"]}))
        self.assertIn(f"/api/avatar/start/?u={zoya.pk}", ask["materials"][0]["link"]["url"])
        self._sync()  # asked once
        self.assertEqual(sum(1 for m in self.fake.materials.values() if m["title"].startswith("🧑")), 1)
        # No licence until the avatar is saved.
        self.assertFalse(getattr(self.fake, "uploads", []))
        self.assertFalse([m for m in self.fake.materials.values() if "Licence number" in m["description"]])

        buf = io.BytesIO()
        Image.new("RGBA", (40, 40), "#2EC866").save(buf, "PNG")
        AIDLUser.objects.filter(pk=zoya.pk).update(avatar_data=base64.b64encode(buf.getvalue()).decode())
        self._sync()
        self.assertEqual(len(self.fake.uploads), 1)  # the licence card, with the avatar
        self.assertEqual(sum(1 for m in self.fake.materials.values() if "Licence number" in m["description"]), 1)
        self.assertFalse([m for m in self.fake.materials.values() if m["title"].startswith("🧑")])
        self._sync()  # once
        self.assertEqual(len(self.fake.uploads), 1)

    def test_upgrading_on_the_website_updates_the_plan_item(self):
        from api.models import Organization

        self._school_with_a_student()
        self._sync()
        resp = self.client.get("/api/google-classroom/admin/upgrade/")
        self.assertTrue(resp["Location"].startswith("http"))
        self.assertIn("/plans?token=", resp["Location"])
        Organization.objects.update(plan="enterprise", seats_purchased=50)
        self._sync()
        (plan,) = [m for m in self.fake.materials.values() if m["title"].startswith("⭐")]
        self.assertEqual(plan["title"], "⭐ Plan: Enterprise · 1/∞ teachers · 1 students · unlimited")
        self.assertIn("👉 Enterprise", plan["description"])

    def test_a_requested_upgrade_shows_as_waiting(self):
        from api import billing
        from api.models import Organization

        self._school_with_a_student()
        self._sync()
        org = Organization.objects.get()
        data = billing._load(org)
        data["pending"] = {"plan": "basic"}  # what the plans page saves when there's no payment set up
        billing._save(org, data)
        self._sync()
        (plan,) = [m for m in self.fake.materials.values() if m["title"].startswith("⭐")]
        self.assertTrue(plan["title"].startswith("⭐ Plan: Trial"))
        self.assertTrue(plan["title"].endswith("⏳ Basic requested"))
        self.assertIn("You asked for Basic", plan["description"])

    def test_a_text_only_licence_post_is_replaced_once_drive_is_allowed(self):
        from integrations.google_classroom.models import AidlClass, GoogleCredential

        self._school_with_a_student()
        self._start()
        self._answer("🚦 Traffic Light Check")
        self._answer("📖 The Highway Code")
        self._sync()
        self.assertEqual(self.fake.licence_materials, [None])  # no Drive permission yet: text only
        old = next(iter(AidlClass.objects.get().licences.values()))["id"]
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/drive.file")
        self._sync()
        self.assertNotIn(old, self.fake.materials)
        self.assertEqual(len(self.fake.licence_materials), 2)
        self._sync()  # once
        self.assertEqual(len(self.fake.licence_materials), 2)

    def test_duplicate_welcomes_from_side_by_side_syncs_are_cleaned_to_one(self):
        from integrations.google_classroom.models import AidlClass

        self._school_with_a_student()
        self._sync()
        # Two syncs that ran at the same time each posted their own copy.
        for mid in ("dup1", "dup2"):
            self.fake.materials[mid] = {"title": "👋 Welcome to AIDL · start here", "description": "old",
                                        "creationTime": "2999-01-01T00:00:00Z", "topicId": "t2"}
        self._start()
        welcomes = [i for i, m in self.fake.materials.items() if m["title"].startswith("👋")]
        self.assertEqual(len(welcomes), 1)
        self.assertEqual(AidlClass.objects.get().step_ids["welcome_item"], welcomes[0])

    def test_copies_of_the_cards_from_side_by_side_syncs_are_removed(self):
        self._school_with_a_student()
        self._start()
        (light,) = [c for c in self.fake.coursework.values() if c["title"] == "🚦 Traffic Light Check"]
        self.fake.coursework["copy1"] = dict(light)  # a second sync posted the card again
        self.fake.coursework["own1"] = {**light, "title": "Teacher's own homework"}
        self._sync()
        titles = [c["title"] for c in self.fake.coursework.values()]
        self.assertEqual(titles.count("🚦 Traffic Light Check"), 1)
        self.assertIn("Teacher's own homework", titles)
        welcomes = [m for m in self.fake.materials.values() if m["title"].startswith("👋")]
        self.assertEqual(len(welcomes), 1)

    def test_the_welcome_moves_off_the_stream_and_stays_above_the_cards(self):
        from integrations.google_classroom.models import AidlClass

        self._school_with_a_student()
        self._sync()  # the student joined: the welcome is up
        # As it was before: the welcome was a Stream post, which teachers see too.
        self.fake.announcements["annw"] = "👋 Welcome to AIDL ..."
        row = AidlClass.objects.get()
        welcome = row.step_ids.pop("welcome_item")
        del self.fake.materials[welcome]
        row.step_ids = {**row.step_ids, "welcome": "annw"}
        row.save(update_fields=["step_ids"])
        self._start()
        self.assertEqual(self.fake.announcements, {})
        items = [m["title"] for m in self.fake.materials.values() if m.get("topicId") == "t2"]
        self.assertEqual(items, ["👋 Welcome to AIDL · start here"])  # once
        # Posted after the cards, so it lists above them (Classwork shows the newest first).
        created = self.fake.created
        self.assertGreater(len(created) - 1 - created[::-1].index("👋 Welcome to AIDL · start here"),
                           created.index("🚦 Traffic Light Check"))
        self.assertIn("Start with 🚦 Traffic Light Check", next(
            m for m in self.fake.materials.values() if m["title"].startswith("👋"))["description"])

    def test_the_students_topic_shows_only_once_a_student_joins(self):
        self._sign_in(granted=ALL_SCOPES)
        self._sync()
        self.assertEqual([t["name"] for t in self.fake.topics], ["🚦 AIDL Admin Center"])
        self.assertFalse([m for m in self.fake.materials.values() if m["title"].startswith("👋")])
        self._start()  # started, still nobody: nothing for students yet
        self.assertEqual([t["name"] for t in self.fake.topics], ["🚦 AIDL Admin Center"])
        self.assertEqual(self.fake.coursework, {})
        self.fake.students["aidl2"] = [{"userId": "s7", "profile": {"name": {"fullName": "Zoya Khan"},
                                                                   "emailAddress": "zoya@school.edu"}}]
        self._sync()
        topic = next(t["topicId"] for t in self.fake.topics if t["name"] == "🎓 Your AI Driving Licence")
        (welcome,) = [m for m in self.fake.materials.values() if m["title"].startswith("👋")]
        self.assertEqual((welcome["state"], welcome["topicId"]), ("PUBLISHED", topic))
        self.assertNotIn("assigneeMode", welcome)  # all students
        self.assertNotIn("Teachers", welcome["description"])  # for students only
        self.assertEqual(len(self.fake.coursework), 2)  # and their cards
        self._sync()  # once
        self.assertEqual(sum(1 for m in self.fake.materials.values() if m["title"].startswith("👋")), 1)

    def test_the_students_topic_goes_when_the_only_student_leaves_before_any_cards(self):
        self._school_with_a_student()
        self._sync()
        self.assertTrue([m for m in self.fake.materials.values() if m["title"].startswith("👋")])
        self.fake.students["aidl2"] = []
        self._sync()
        self.assertFalse([m for m in self.fake.materials.values() if m["title"].startswith("👋")])
        self.assertEqual([t["name"] for t in self.fake.topics], ["🚦 AIDL Admin Center"])

    def test_a_licence_posted_on_the_stream_moves_to_classwork(self):
        from integrations.google_classroom.models import AidlClass

        self._school_with_a_student()
        self._start()
        self._answer("🚦 Traffic Light Check")
        self._answer("📖 The Highway Code")
        self._sync()
        # As it was before: the licence was a Stream post, which teachers see too.
        row = AidlClass.objects.get()
        (uid, entry), = row.licences.items()
        self.fake.announcements["annold"] = "🎉 Well done, Zoya! ... Licence number: ..."
        row.licences = {uid: {"id": "annold", "image": False}}
        row.save(update_fields=["licences"])
        self._sync()
        self.assertNotIn("annold", self.fake.announcements)
        self.assertEqual(AidlClass.objects.get().licences[uid]["kind"], "material")
        self.assertEqual(len(self.fake.licence_items), 2)
        self._sync()  # once
        self.assertEqual(len(self.fake.licence_items), 2)

    def test_a_student_who_is_also_an_aidl_admin_still_gets_the_licence(self):
        self._school_with_a_student()
        self._start()
        AIDLUser.objects.filter(microsoft_id="google:s7").update(role=AIDLUser.Role.ADMIN)
        self._answer("🚦 Traffic Light Check")
        self._answer("📖 The Highway Code")
        self._sync()
        zoya = AIDLUser.objects.get(microsoft_id="google:s7")
        self.assertTrue(zoya.licence_issued)
        (ids, _text, _topic), = self.fake.licence_items
        self.assertEqual(ids, ["s7"])

    def test_a_card_with_an_old_title_is_renamed_not_sent_again(self):
        self._school_with_a_student()
        self._start()
        cid = next(i for i, c in self.fake.coursework.items() if c["title"] == "🚦 Traffic Light Check")
        self.fake.coursework[cid]["title"] = "🏠 Old School AI policy · Privacy on company devices"
        ids = set(self.fake.coursework)
        self._sync()
        self.assertEqual(set(self.fake.coursework), ids)  # the same question: answers stay
        self.assertEqual(self.fake.coursework[cid]["title"], "🚦 Traffic Light Check")

    def test_policy_card_titles_leave_out_the_school_name(self):
        from api.models import Organization
        from integrations.google_classroom.learners import _cards

        org = Organization.objects.create(name="Sakshi Vanjare's classroom")
        self.assertTrue(all(org.name not in c["title"] for c in _cards(org)))

    def test_a_card_deleted_in_classroom_is_published_again(self):
        self._school_with_a_student()
        self._start()
        tl = next(i for i, c in self.fake.coursework.items() if c["title"] == "🚦 Traffic Light Check")
        del self.fake.coursework[tl]
        self._sync()
        titles = sorted(c["title"] for c in self.fake.coursework.values())
        self.assertEqual(titles, ["📖 The Highway Code", "🚦 Traffic Light Check"])

    def test_a_deleted_cards_topic_is_made_again_with_the_cards_in_it(self):
        self._school_with_a_student()
        self._start()
        self.fake.topics = [t for t in self.fake.topics if t["name"] != "🎓 Your AI Driving Licence"]
        for c in self.fake.coursework.values():
            c.pop("topicId", None)  # Classroom moves them to "No topic"
        self._sync()
        (topic,) = [t for t in self.fake.topics if t["name"] == "🎓 Your AI Driving Licence"]
        self.assertTrue(all(c.get("topicId") == topic["topicId"] for c in self.fake.coursework.values()))

    def test_cards_go_only_to_students_the_plan_lets_in(self):
        self._school_with_a_student()
        self._start()
        for c in self.fake.coursework.values():
            self.assertEqual((c["assigneeMode"], c["individualStudentsOptions"]["studentIds"]),
                             ("INDIVIDUAL_STUDENTS", ["s7"]))

    def test_trial_lets_in_five_students_and_tells_the_sixth(self):
        self._school_with_a_student()
        self.fake.students["aidl2"] = [{"userId": f"s{i}", "profile": {"name": {"fullName": f"Student {i}"},
                                                                       "emailAddress": f"s{i}@school.edu"}}
                                       for i in range(1, 7)]
        self._start()
        allowed = self.fake.coursework[next(iter(self.fake.coursework))]["individualStudentsOptions"]["studentIds"]
        self.assertEqual(len(allowed), 5)
        (ids, text), = self.fake.private
        self.assertEqual(len(ids), 1)
        self.assertNotIn(ids[0], allowed)
        self.assertIn("plan is full", text)
        self._sync()  # told once
        self.assertEqual(len(self.fake.private), 1)

    def test_basic_is_for_teachers_only(self):
        from api.models import Organization

        self._school_with_a_student()
        Organization.objects.update(plan="basic", seats_purchased=50)
        self._start()
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:s7").exists())
        self.assertEqual(self.fake.coursework, {})  # no student allowed: nothing published
        (ids, text), = self.fake.private
        self.assertEqual(ids, ["s7"])
        self.assertIn("teachers only", text)
        page = self.client.get("/api/google-classroom/admin/")
        self.assertContains(page, "teachers-only plan")

    def test_on_a_teachers_only_plan_student_invitations_are_cancelled(self):
        from api.models import Organization
        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()
        self.fake.students["aidl2"] = []
        Organization.objects.update(plan="basic", seats_purchased=50)
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        teacher = AIDLUser.objects.get(role=AIDLUser.Role.ADMIN)
        teacher_gid = teacher.microsoft_id.removeprefix("google:")
        self.fake.invitations = [
            {"id": "i1", "courseId": "aidl2", "userId": "s9", "role": "STUDENT"},
            {"id": "i2", "courseId": "aidl2", "userId": teacher_gid, "role": "STUDENT"},  # taking the course
            {"id": "i3", "courseId": "aidl2", "userId": "t5", "role": "TEACHER"},
            {"id": "i4", "courseId": "c1", "userId": "s8", "role": "STUDENT"},  # another class
        ]
        self.fake.profiles = {"s9": {"name": {"fullName": "Piyush Dhote"}, "emailAddress": "piyush@school.edu"}}
        self._start()
        self.assertEqual([i["id"] for i in self.fake.invitations], ["i2", "i3", "i4"])
        self._sync()  # the plan item shows who
        (plan,) = [m for m in self.fake.materials.values() if m["title"].startswith("⭐")]
        self.assertIn("Piyush Dhote (piyush@school.edu) · student invitation cancelled", plan["description"])
        # The teacher is emailed, once per cancelled invitation.
        from django.core import mail

        (email,) = [m for m in mail.outbox if "student invitation cancelled" in m.subject]
        self.assertEqual(email.to, [teacher.email])
        self.assertIn("Piyush Dhote (piyush@school.edu)", email.body)
        self.assertIn("teachers only", email.body)
        # ... and warned beforehand, on top of the Admin Center (teachers only) and on the website.
        (warn,) = [m for m in self.fake.materials.values() if m["title"].startswith("⛔")]
        self.assertEqual((warn["state"], warn["topicId"]), ("DRAFT", "t1"))
        self.assertIn("Don't invite students", warn["description"])
        made = self.fake.created  # newer = higher in Classwork: above the setup summary
        self.assertGreater(made.index(warn["title"]), max(i for i, t in enumerate(made) if t.startswith("📋")))
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "AIDL cancels their invitations")

    def test_the_dont_invite_warning_goes_away_after_an_upgrade(self):
        from api.models import Organization

        self._school_with_a_student()
        Organization.objects.update(plan="basic", seats_purchased=50)
        self._sync()
        self._sync()
        self.assertEqual(sum(1 for m in self.fake.materials.values() if m["title"].startswith("⛔")), 1)
        Organization.objects.update(plan="enterprise", seats_purchased=50)
        self._sync()
        self.assertFalse([m for m in self.fake.materials.values() if m["title"].startswith("⛔")])

    def test_a_doubled_dont_invite_warning_is_cleaned_up(self):
        from api.models import Organization
        from integrations.google_classroom.models import AidlClass

        self._school_with_a_student()
        Organization.objects.update(plan="basic", seats_purchased=50)
        self._sync()
        # Two syncs at once each made one, and the kept id was lost.
        (warn,) = [m for m in self.fake.materials.values() if m["title"].startswith("⛔")]
        self.fake.materials["mx"] = dict(warn)
        row = AidlClass.objects.get()
        row.step_ids = {**row.step_ids, "no_students": ""}
        row.save(update_fields=["step_ids"])
        Organization.objects.update(seats_purchased=60)  # any admin action refreshes the Admin Center
        from integrations.google_classroom import aidl_class

        aidl_class.refresh(Organization.objects.get())
        self.assertEqual(sum(1 for m in self.fake.materials.values() if m["title"].startswith("⛔")), 1)

    def test_on_trial_a_third_teacher_invitation_is_cancelled(self):
        from django.core import mail

        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self.fake.teachers = {"aidl2": [{"userId": "owner"}]}  # Trial: 2 teachers, so room for one more
        self.fake.invitations = [
            {"id": "i1", "courseId": "aidl2", "userId": "t2", "role": "TEACHER"},
            {"id": "i2", "courseId": "aidl2", "userId": "t3", "role": "TEACHER"},
        ]
        self.fake.profiles = {"t3": {"name": {"fullName": "Ravi Teacher"}, "emailAddress": "ravi@school.edu"}}
        self._sync()
        self.assertEqual([i["id"] for i in self.fake.invitations], ["i1"])  # the second teacher fits; the third doesn't
        (email,) = [m for m in mail.outbox if "teacher invitation cancelled" in m.subject]
        self.assertIn("Ravi Teacher (ravi@school.edu)", email.body)
        self.assertIn("allows 2 teachers", email.body)
        self._sync()  # the plan item shows who
        (plan,) = [m for m in self.fake.materials.values() if m["title"].startswith("⭐")]
        self.assertIn("Ravi Teacher (ravi@school.edu) · teacher invitation cancelled", plan["description"])
        self.assertFalse([m for m in self.fake.materials.values() if m["title"].startswith("⛔ Don't invite more")])

        # Once the second teacher joins, the class is full: a warning for teachers, on top of the Admin Center.
        self.fake.teachers["aidl2"].append({"userId": "t2"})
        self.fake.invitations = []
        self._sync()  # no admin action needed
        (warn,) = [m for m in self.fake.materials.values() if m["title"].startswith("⛔ Don't invite more")]
        self.assertEqual(warn["state"], "DRAFT")
        self.assertIn("allows 2 teachers", warn["description"])
        self.fake.teachers["aidl2"].pop()  # a teacher left: room again
        self._sync()
        self.assertFalse([m for m in self.fake.materials.values() if m["title"].startswith("⛔ Don't invite more")])

    def _enterprise(self, seats=50):
        from api.models import Organization

        Organization.objects.update(plan="enterprise", seats_purchased=seats)

    def test_invite_all_students_from_my_other_classes(self):
        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()  # Zoya (s7) is in the AIDL class
        self._enterprise()
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self.fake.students["c1"] = [  # AI Basics: Ravi, Meera, Zoya again, and someone outside the school
            {"userId": s, "profile": {"name": {"fullName": n}, "emailAddress": f"{s}@school.edu"}}
            for s, n in (("s1", "Ravi Kumar"), ("s2", "Meera Shah"), ("s7", "Zoya Khan"), ("outsider1", "Out Sider"))]
        self.fake.students["c2"] = [{"userId": "s1", "profile": {"name": {"fullName": "Ravi Kumar"}}},
                                    {"userId": "s3", "profile": {"name": {"fullName": "Asha Rao"}}}]
        self.client.post("/api/google-classroom/classes/sync/")
        self._sync()
        page = self.client.get("/api/google-classroom/classes/")
        self.assertContains(page, "Invite students from your other classes")
        self.assertContains(page, "3 not in AIDL yet")  # AI Basics: everyone but Zoya
        self.assertContains(page, "Select all students")  # ticks every class at once
        resp = self.client.post("/api/google-classroom/classes/invite/", {"course_ids": ["c1", "c2"]}, follow=True)
        invited = sorted(i["userId"] for i in self.fake.invitations if i["courseId"] == "aidl2")
        self.assertEqual(invited, ["s1", "s2", "s3"])  # each once; Zoya's already in
        self.assertTrue(all(i["role"] == "STUDENT" for i in self.fake.invitations))
        self.assertContains(resp, "3 students invited")
        self.assertContains(resp, "couldn")  # the outsider
        # Again: nobody twice.
        resp = self.client.post("/api/google-classroom/classes/invite/", {"course_ids": ["c1"]}, follow=True)
        self.assertEqual(len(self.fake.invitations), 3)
        self.assertContains(resp, "0 students invited")

    def test_enterprise_has_no_student_limit(self):
        from api.licensing import seats_left
        from api.models import Organization
        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()
        self._enterprise(seats=5)  # however few seats were bought
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self.fake.students["c1"] = [{"userId": f"s{i}", "profile": {"name": {"fullName": f"Student {i}"}}}
                                    for i in range(10, 20)]
        self.client.post("/api/google-classroom/classes/sync/")
        self._sync()
        resp = self.client.post("/api/google-classroom/classes/invite/", {"course_ids": ["c1"]}, follow=True)
        self.assertEqual(len(self.fake.invitations), 10)  # all of them
        self.assertNotContains(resp, "no free seats")
        self.assertGreater(seats_left(Organization.objects.get()), 100_000)  # licences never run out either
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "/ unlimited")

    def test_invite_from_classes_needs_the_roster_permission(self):
        self._school_with_a_student()
        self._enterprise()
        self.client.post("/api/google-classroom/classes/sync/")
        resp = self.client.post("/api/google-classroom/classes/invite/", {"course_ids": ["c1"]}, follow=True)
        self.assertContains(resp, "needs permission to invite students")
        self.assertFalse(getattr(self.fake, "invitations", []))

    def test_invite_by_email_into_the_aidl_class(self):
        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()  # Zoya (s7, zoya@school.edu) is in the AIDL class
        self._enterprise()
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self._sync()
        page = self.client.get("/api/google-classroom/classes/")
        self.assertContains(page, "Invite students by email")
        pasted = ("Riya Sharma <Riya@School.edu>, aman@school.edu\n"
                  "zoya@school.edu\taman@school.edu\nnot an email\noutsider1@other.org")
        resp = self.client.post("/api/google-classroom/classes/invite-emails/", {"emails": pasted}, follow=True)
        invited = [i["userId"] for i in self.fake.invitations]
        self.assertEqual(invited, ["riya@school.edu", "aman@school.edu"])  # each once; Zoya's in already
        self.assertContains(resp, "2 students invited")
        self.assertContains(resp, "1 already in the class")
        self.assertContains(resp, "couldn")  # outsider1: Google refused
        resp = self.client.post("/api/google-classroom/classes/invite-emails/", {"emails": "aman@school.edu"},
                                follow=True)  # again: Google says it exists
        self.assertEqual(len(self.fake.invitations), 2)
        self.assertContains(resp, "0 students invited")

    def test_bulk_invite_is_for_enterprise_only(self):
        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()  # Trial: students are added in Classroom's People tab
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self.fake.students["c1"] = [{"userId": "s1", "profile": {"name": {"fullName": "Ravi Kumar"}}}]
        self.client.post("/api/google-classroom/classes/sync/")
        page = self.client.get("/api/google-classroom/classes/")
        self.assertContains(page, "Enterprise feature")
        self.assertNotContains(page, "Invite these students")
        for url, data in (("/api/google-classroom/classes/invite-emails/", {"emails": "aman@school.edu"}),
                          ("/api/google-classroom/classes/invite/", {"course_ids": ["c1"]})):
            resp = self.client.post(url, data, follow=True)
            self.assertContains(resp, "part of the Enterprise plan")
        self.assertFalse(getattr(self.fake, "invitations", []))
        self.assertFalse([m for m in self.fake.materials.values() if m["title"] == "👥 Add students"])

    def test_invitations_are_kept_when_the_plan_has_room(self):
        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self.fake.invitations = [{"id": "i1", "courseId": "aidl2", "userId": "s9", "role": "STUDENT"}]
        self._start()
        self.assertEqual(len(self.fake.invitations), 1)

    def test_like_slack_a_student_without_room_is_taken_out_of_the_class(self):
        from api.models import Organization
        from integrations.google_classroom.models import GoogleCredential

        self._school_with_a_student()
        Organization.objects.update(plan="basic", seats_purchased=50)
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self._start()
        self.assertEqual(self.fake.kicked, ["s7"])
        self.assertEqual(self.fake.students["aidl2"], [])
        self.assertFalse(getattr(self.fake, "private", []))  # no note: they can't see the class any more
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:s7").exists())
        (plan,) = [m for m in self.fake.materials.values() if m["title"].startswith("⭐")]
        self.assertIn("Taken out of the AIDL class", plan["description"])
        self.assertIn("Zoya Khan (zoya@school.edu)", plan["description"])
        self._sync()  # once
        self.assertEqual(self.fake.kicked, ["s7"])

    def test_trial_allows_two_teachers(self):
        self._school_with_a_student()
        self._sync()
        zoya = AIDLUser.objects.get(microsoft_id="google:s7")
        self.client.post("/api/google-classroom/admin/admins/action/", {"action": "add", "user_id": zoya.pk})
        self.assertEqual(AIDLUser.objects.get(pk=zoya.pk).role, "admin")
        AIDLUser.objects.create(microsoft_id="google:s8", email="x@school.edu", full_name="X", role="learner",
                                organization_id=zoya.organization_id, is_active=True)
        third = AIDLUser.objects.get(microsoft_id="google:s8")
        resp = self.client.post("/api/google-classroom/admin/admins/action/",
                                {"action": "add", "user_id": third.pk}, follow=True)
        self.assertContains(resp, "Your plan allows 2 teachers")
        self.assertEqual(AIDLUser.objects.get(pk=third.pk).role, "learner")

    def test_someone_not_in_the_class_is_ignored(self):
        self._school_with_a_student()
        self._start()
        self._answer("🚦 Traffic Light Check", gid="stranger")
        self._sync()
        self.assertFalse(AIDLUser.objects.filter(microsoft_id="google:stranger").exists())


# The step-2 class inherits from the sign-in class for its helpers; don't run the sign-in tests twice.
for _name in [n for n in dir(GoogleClassroomSignIn) if n.startswith("test_")]:
    setattr(GoogleClassroomClasses, _name, None)
    setattr(GoogleClassroomAidlClass, _name, None)
for _name in [n for n in dir(GoogleClassroomAidlClass) if n.startswith("test_")]:
    setattr(GoogleClassroomLearnerCards, _name, None)


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

    def test_admin_center_asks_for_the_school_details_first(self, _warm):
        self._class_in_aidl()
        resp = self.client.get("/api/google-classroom/admin/")
        self.assertEqual(resp["Location"], "/api/google-classroom/admin/setup/")
        self._details_done()
        page = self.client.get("/api/google-classroom/admin/")
        self.assertContains(page, "One step left: your AI policy")
        self.assertNotContains(page, "Card approval")
        self.assertNotContains(page, "Start the programme")

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
        self._details_done()
        resp = self._open_link()  # sub 111 = the teacher
        self.assertContains(resp, "Admin Center")
        self.assertContains(resp, "Review your AI policy")


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
        self._details_done()
        return (AIDLUser.objects.get(microsoft_id="google:111"), AIDLUser.objects.get(microsoft_id="google:s1"),
                AIDLUser.objects.get(microsoft_id="google:s2"))

    def _as(self, sub, email):
        """Sign in to the Admin Center as this Google account (through the student link)."""
        return self._open_link(sub=sub, email=email)

    def test_home_shows_plan_numbers_and_no_old_steps(self):
        self._school()
        page = self.client.get("/api/google-classroom/admin/")
        self.assertContains(page, "👑 Master admin")
        self.assertContains(page, "<b>2</b> learners in AIDL", html=False)
        self.assertContains(page, "expected about 50–200")
        self.assertContains(page, "Enterprise covers your whole school")  # Trial: 5 students, ~200 expected
        for gone in ("Card approval", "Start the programme", "Admins (optional)", "🧑‍💼 Admins"):
            self.assertNotContains(page, gone)

    def test_policy_in_use_starts_the_programme_by_itself(self):
        from api import admin_setup, card_approval
        from api import policy_cards as pc

        teacher, _r, _m = self._school()
        org = Organization.objects.get(pk=teacher.organization_id)
        pc.approve(org, teacher, 1, {}, card_lines=["Use only the AI tools on the school's list."])
        page = self.client.get("/api/google-classroom/admin/")
        org.refresh_from_db()
        self.assertTrue(admin_setup.started(org))
        self.assertTrue(card_approval.onboarding_ready(org))  # the starter cards, approved for them
        self.assertContains(page, "✅ Programme running")

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
        Organization.objects.filter(pk=ravi.organization_id).update(plan="enterprise", seats_purchased=50)  # 3 admins
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
        _t, ravi, meera = self._school()  # Trial: 2 teachers (plan_limits)
        self.client.post("/api/google-classroom/admin/admins/action/", {"action": "add", "user_id": ravi.pk})
        resp = self.client.post("/api/google-classroom/admin/admins/action/",
                                {"action": "add", "user_id": meera.pk}, follow=True)
        self.assertContains(resp, "Your plan allows 2 teachers")

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
        self.assertContains(resp, "0 of 3 approved")  # the Trial: 3 topics
        self.assertEqual(self.client.get("/api/google-classroom/admin/regulators/").status_code, 200)

    def test_policy_card_page_and_incomplete_answers(self):
        from api import policy_cards as pc
        self._school()
        self._company()
        from django.utils.html import escape
        n = pc.FREE_TOPICS[0]
        page = self.client.get(f"/api/google-classroom/admin/policy/{n}/")
        self.assertContains(page, escape(pc.SHORT[n]))
        resp = self.client.post(f"/api/google-classroom/admin/policy/{n}/", {"action": "approve", "when": "today"})
        self.assertContains(resp, "Please check")

    def test_approved_card_reaches_the_learner_flow(self):
        from api import policy_cards as pc
        from api import aup_store
        teacher, _r, _m = self._school()
        self._start_programme()
        org = Organization.objects.get(pk=teacher.organization_id)
        pc.approve(org, teacher, 1, {}, card_lines=["Use only the AI tools on the school's list."])
        self.assertContains(self.client.get("/api/google-classroom/admin/"), "In use: 1 policy card ")

        self._open_link(sub="s1", email="ravi@school.edu")
        self.client.post("/api/google-classroom/learn/action/", {"action": "start"})
        page = self.client.get("/api/google-classroom/learn/")
        self.assertContains(page, pc.SHORT[1])
        version = aup_store.required_points(org)[1]
        self.client.post("/api/google-classroom/learn/action/", {"action": "agree", "n": "1", "version": version})
        ravi = AIDLUser.objects.get(microsoft_id="google:s1")
        self.assertEqual(aup_store.pending_points(ravi, org), [])

    def test_trial_has_3_free_topics_like_slack_and_teams(self):
        from api import policy_cards as pc
        self._school()
        page = self._company()
        self.assertContains(page, "Your Trial includes 3 policy topics")
        self.assertContains(page, "🆓 Free in your Trial", count=3)
        self.assertContains(page, "⭐ Paid plan", count=11)
        self.assertContains(page, "See plans &amp; upgrade")
        titles = [t["title"] for t in page.context["topics"]]
        self.assertEqual(titles[:3], [pc.SHORT[n] for n in pc.FREE_TOPICS])  # free ones first
        self.assertNotContains(page, "· point")

    def test_a_paid_topic_on_the_trial_shows_the_upgrade_page_and_cant_be_approved(self):
        from api import aup_store
        teacher, _r, _m = self._school()
        self._company()
        page = self.client.get("/api/google-classroom/admin/policy/1/")
        self.assertContains(page, "is on the paid plans")
        self.assertContains(page, "11 extra policy topics")
        self.client.post("/api/google-classroom/admin/policy/1/", {"action": "approve", "when": "today"})
        self.assertIsNone(aup_store.card(Organization.objects.get(pk=teacher.organization_id), 1))

    def test_all_14_topics_on_a_paid_plan(self):
        teacher, _r, _m = self._school()
        Organization.objects.filter(pk=teacher.organization_id).update(plan="enterprise")
        page = self._company()
        self.assertContains(page, "0 of 14 approved")
        self.assertNotContains(page, "⭐ Paid plan")
        self.assertNotContains(self.client.get("/api/google-classroom/admin/policy/1/"), "is on the paid plans")

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


# --- the school's short setup after sign-up (onboarding.py) ---

class GoogleClassroomSetup(GoogleClassroomAidlClass):
    """Sign up → school details (approx. students) → AI policy in one click → done, automatically."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("integrations.google_classroom.google_oauth.refresh_access_token", return_value="access-2")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _org(self):
        return Organization.objects.get()

    def test_new_school_lands_in_classroom_and_starts_the_setup_from_classwork(self):
        r = self._sign_in(granted=ALL_SCOPES)
        go = self.client.get(r["admin_center_url"].removeprefix("https://backend.test"))
        self.assertTrue(go["Location"].startswith("https://classroom.google.com/c/aidl2"))  # Classroom first
        self.assertIn("👉 Step 1 · School details", [m["title"] for m in self.fake.materials.values()])
        # Classwork's Step 1 opens the setup in a new tab, with a way back to Classroom.
        go = self.client.get("/api/google-classroom/admin/step/1/")
        page = self.client.get(go["Location"])
        self.assertContains(page, "About how many students?")
        self.assertContains(page, 'value="school.edu"')  # the name from Google, to change
        self.assertContains(page, "Back to Google Classroom")
        self.client.post("/api/google-classroom/admin/setup/", {"name": "Green Valley School", "students": "200"})
        self.client.post("/api/google-classroom/admin/setup/policy/", {
            "action": "use", "speaker": "the Principal", "storage": "Google Drive", "report": "it@school.edu",
            "tools": "Google Gemini"})
        saved = self.client.get("/api/google-classroom/admin/setup/policy/?saved=1")
        self.assertContains(saved, 'class="btn go back-to-classroom" href="https://classroom.google.com/c/aidl2"')

    def test_details_need_a_name_and_an_estimate(self):
        from integrations.google_classroom.models import SchoolSetup

        self._sign_in(granted=ALL_SCOPES)
        resp = self.client.post("/api/google-classroom/admin/setup/", {"name": "Green Valley", "students": ""})
        self.assertContains(resp, "roughly how many students")
        resp = self.client.post("/api/google-classroom/admin/setup/", {"name": "  ", "students": "200"})
        self.assertContains(resp, "school&#x27;s name")
        resp = self.client.post("/api/google-classroom/admin/setup/",
                                {"name": "Green Valley School", "students": "500", "teachers": "50"})
        # Opened from Classwork: it says so and goes back to Classroom (Step 2 is there), not on to the policy.
        self.assertEqual(resp["Location"], "/api/google-classroom/admin/setup/?saved=1")
        saved = self.client.get(resp["Location"])
        self.assertContains(saved, "School details saved")
        self.assertContains(saved, "Step 2 · Your AI policy")
        self.assertContains(saved, "Taking you back to Google Classroom")
        self.assertNotContains(saved, "Use this policy")
        row = SchoolSetup.objects.get()
        self.assertEqual((row.approx_students, row.approx_teachers), ("500", "50"))
        self.assertEqual(self._org().name, "Green Valley School")
        # Then the Admin Center opens, and Continue goes to Google Classroom.
        self.assertEqual(self.client.get("/api/google-classroom/admin/").status_code, 200)
        self.assertTrue(self.client.get("/api/google-classroom/open/")["Location"].startswith(
            "https://classroom.google.com/c/aidl2"))

    def test_policy_is_prefilled_and_one_click_starts_everything(self):
        from api import admin_setup, aup_store, card_approval

        self._sign_in(granted=ALL_SCOPES)
        self.client.post("/api/google-classroom/admin/setup/", {"name": "Green Valley School", "students": "200"})
        page = self.client.get("/api/google-classroom/admin/setup/policy/")
        self.assertContains(page, 'value="the Principal"')
        self.assertContains(page, 'value="Google Drive"')
        self.assertContains(page, 'value="asha@school.edu"')
        self.assertEqual(len(page.context["cards"]), 3)  # the Trial's 3 topics
        self.assertFalse([c for c in page.context["cards"] if c["problems"]])
        org = self._org()
        self.assertFalse(admin_setup.started(org))

        resp = self.client.post("/api/google-classroom/admin/setup/policy/", {
            "action": "use", "speaker": "the Head Teacher", "storage": "Google Drive",
            "report": "it@school.edu", "tools": "Google Gemini, NotebookLM"})
        self.assertEqual(resp["Location"], "/api/google-classroom/admin/setup/policy/?saved=1")
        org.refresh_from_db()
        self.assertEqual(len(aup_store.cards(org)), 3)
        self.assertTrue(card_approval.onboarding_ready(org))  # no approving by hand
        self.assertTrue(admin_setup.started(org))  # no Start button
        saved = self.client.get(resp["Location"])
        self.assertContains(saved, "Your AI policy is in use")
        self.assertContains(saved, "The programme has started")
        self.assertContains(saved, "Enterprise covers your whole school")
        self.assertNotContains(saved, "About how many students")  # this step only
        done = self.client.get("/api/google-classroom/admin/setup/done/")
        self.assertContains(done, "AIDL is ready for Green Valley School")
        self.assertContains(done, "Programme started")
        self.assertContains(done, "Enterprise covers your whole school")  # ~200 students on a 5-student Trial
        self.assertContains(done, "Inviting everyone automatically is part of Enterprise")
        # Classroom's setup summary says it's done.
        self.assertIn("🎉 Setup done · 2 of 2", [m["title"] for m in self.fake.materials.values()])

    def test_an_empty_answer_is_asked_for_and_nothing_is_approved(self):
        from api import admin_setup, aup_store

        self._sign_in(granted=ALL_SCOPES)
        self.client.post("/api/google-classroom/admin/setup/", {"name": "Green Valley School", "students": "50"})
        resp = self.client.post("/api/google-classroom/admin/setup/policy/", {
            "action": "use", "speaker": "", "storage": "Google Drive", "report": "it@school.edu", "tools": "Gemini"})
        self.assertContains(resp, "Please fix these first")
        self.assertEqual(aup_store.cards(self._org()), {})
        self.assertFalse(admin_setup.started(self._org()))

    def test_enterprise_invites_the_students_of_the_other_classes(self):
        from integrations.google_classroom.models import GoogleCredential, SchoolSetup

        self._sign_in(granted=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        Organization.objects.update(plan="enterprise", seats_purchased=50)
        GoogleCredential.objects.update(scopes=f"{ALL_SCOPES} https://www.googleapis.com/auth/classroom.rosters")
        self.client.post("/api/google-classroom/admin/setup/", {"name": "Green Valley School", "students": "1000+"})
        self.client.post("/api/google-classroom/admin/setup/policy/", {
            "action": "use", "speaker": "the Principal", "storage": "Google Drive", "report": "it@school.edu",
            "tools": "Google Gemini"})
        invited = sorted(i["userId"] for i in self.fake.invitations if i["courseId"] == "aidl2")
        self.assertTrue(invited)
        self.assertEqual(SchoolSetup.objects.get().invited["invited"], len(invited))
        done = self.client.get("/api/google-classroom/admin/setup/done/")
        self.assertContains(done, f"{len(invited)} student")
        self.assertNotContains(done, "Enterprise covers")  # no limit: no upsell

    def test_without_an_aidl_class_the_steps_follow_each_other(self):
        self._sign_in()  # no Classroom permissions: no AIDL class to go back to
        resp = self.client.post("/api/google-classroom/admin/setup/", {"name": "Green Valley School", "students": "50"})
        self.assertEqual(resp["Location"], "/api/google-classroom/admin/setup/policy/")

    def test_done_and_policy_need_the_details_first(self):
        self._sign_in(granted=ALL_SCOPES)
        for url in ("/api/google-classroom/admin/setup/policy/", "/api/google-classroom/admin/setup/done/"):
            self.assertEqual(self.client.get(url)["Location"], "/api/google-classroom/admin/setup/")

    def test_learner_cannot_open_the_setup(self):
        self._sign_in(granted=ALL_SCOPES)
        self._sign_in(sub="222", email="ravi@school.edu", granted=ALL_SCOPES)
        self.assertContains(self.client.get("/api/google-classroom/admin/setup/"), "Admins only", status_code=403)


for _name in [n for n in dir(GoogleClassroomAidlClass) if n.startswith("test_")]:
    setattr(GoogleClassroomSetup, _name, None)

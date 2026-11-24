"""Enterprise "whole school": the IT admin connects, AIDL finds every student and adds them
straight into the AIDL class. Google is faked.

    python manage.py test integrations.google_classroom.test_school_directory
"""

import time
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from api.microsoft_auth import open_refresh_token
from api.models import AIDLUser, Organization
from integrations.google_classroom import google_oauth, school_directory, session
from integrations.google_classroom.google_oauth import GoogleError
from integrations.google_classroom.models import AidlClass, GoogleWorkspace, SchoolDirectory

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CLIENT_ID = "test-client.apps.googleusercontent.com"
ALL = " ".join(("openid email profile", *google_oauth.SCHOOL_SCOPES))
P = "integrations.google_classroom."


class FakeGoogle:
    """The school's accounts (by folder) and the AIDL class's roster."""

    def __init__(self):
        self.units = [{"name": "Students", "orgUnitPath": "/Students"},
                      {"name": "Grade 9", "orgUnitPath": "/Students/Grade 9"},
                      {"name": "Staff", "orgUnitPath": "/Staff"}]
        self.users = {"/Students": [self.user("s1", "Ravi Kumar"), self.user("s2", "Meera Shah"),
                                    self.user("s3", "Old Student", suspended=True)],
                      "/Students/Grade 9": [self.user("s2", "Meera Shah"), self.user("s4", "Zoya Khan")],
                      "/Staff": [self.user("t1", "Teacher One")]}
        self.roster = [{"userId": "s1", "profile": {"emailAddress": "s1@school.edu"}}]  # Ravi is in already
        self.teachers = [{"userId": "t0", "profile": {"emailAddress": "asha@school.edu"}}]
        self.added, self.invited = [], []
        self.forbid = set()  # ids Google won't let the admin add directly
        self.admin = True

    @staticmethod
    def user(uid, name, suspended=False):
        return {"id": uid, "primaryEmail": f"{uid}@school.edu", "name": {"fullName": name}, "suspended": suspended}

    def admin_flags(self, token, email):
        if not self.admin:
            raise GoogleError("not_admin", "This Google account isn't an admin of your school's Google Workspace. "
                                           "Only your IT admin can connect the whole school.")
        return {"isAdmin": True}

    def list_users_in(self, token, path):  # the unit and the units under it, like Google's query
        return [u for p, us in self.users.items() if p == path or p.startswith(path + "/") for u in us]

    def add_student(self, token, course, uid):
        if uid in self.forbid:
            raise GoogleError("classroom_forbidden", "no")
        if any(m["userId"] == uid for m in self.roster):
            raise GoogleError("already_exists", "there")
        self.roster.append({"userId": uid, "profile": {"emailAddress": f"{uid}@school.edu"}})
        self.added.append(uid)
        return {}

    def create_invitation(self, token, course, uid, role="STUDENT"):
        self.invited.append(uid)
        return {}


@override_settings(GOOGLE_CLASSROOM_SYNC=True, BACKEND_URL="https://backend.test")
class WholeSchool(TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": "secret",
                                             "GOOGLE_CLASSROOM_REDIRECT_URI": "", "GOOGLE_CLASSROOM_ADD_PAUSE": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.fake = FakeGoogle()
        patches = {
            "google_oauth._signing_key": mock.Mock(return_value=KEY.public_key()),
            "google_oauth.refresh_access_token": mock.Mock(return_value="access-2"),
            "directory_api.admin_flags": self.fake.admin_flags,
            "directory_api.list_org_units": lambda token: self.fake.units,
            "directory_api.list_users_in": self.fake.list_users_in,
            "classroom_api.list_students": lambda token, c: list(self.fake.roster),
            "classroom_api.list_teachers": lambda token, c: list(self.fake.teachers),
            "classroom_api.add_student": self.fake.add_student,
            "classroom_api.create_invitation": self.fake.create_invitation,
            "learners.sync": mock.Mock(return_value={}),  # the learner sync has its own tests
        }
        for name, new in patches.items():
            p = mock.patch(P + name, new)
            p.start()
            self.addCleanup(p.stop)

        self.org = Organization.objects.create(name="school.edu", slug="school-edu", plan="enterprise",
                                               seats_purchased=50)
        GoogleWorkspace.objects.create(key="domain:school.edu", name="school.edu", domain="school.edu",
                                       organization_id=str(self.org.pk))
        self.teacher = AIDLUser.objects.create(email="asha@school.edu", full_name="Asha Rao", microsoft_id="google:t0",
                                               provider="google_classroom", role=AIDLUser.Role.ADMIN,
                                               enroll_as=AIDLUser.EnrollAs.ORGANIZATION,
                                               organization_id=str(self.org.pk), is_active=True)
        AidlClass.objects.create(organization_id=str(self.org.pk), teacher_user_id=str(self.teacher.pk),
                                 course_id="aidl1", state="ACTIVE")

    # --- helpers ---

    def _sign_in(self, user):
        from datetime import datetime, timezone as tz

        now = datetime.now(tz.utc)
        self.client.cookies[session.COOKIE] = jwt.encode(
            {"sub": str(user.pk), "type": session.TOKEN_TYPE, "iat": now, "exp": now + timedelta(days=1)},
            settings.JWT_SECRET, algorithm="HS256")

    def _id_token(self, sub="900", email="it@school.edu", hd="school.edu"):
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": sub, "iat": now, "exp": now + 300,
                  "email": email, "email_verified": True, "name": "Ira Tech", "given_name": "Ira",
                  "family_name": "Tech"}
        if hd:
            claims["hd"] = hd
        return jwt.encode(claims, KEY, algorithm="RS256")

    def _it_admin_link(self):
        self._sign_in(self.teacher)
        page = self.client.get("/api/google-classroom/admin/school/")
        self.assertEqual(page.status_code, 200)
        link = page.context["it_admin_link"]
        self.client.cookies.clear()  # the IT admin opens it in their own browser
        return link.removeprefix("http://testserver")

    def _connect(self, granted=ALL, **token_args):
        """The IT admin opens the teacher's link → Google → back to AIDL."""
        link = self._it_admin_link()
        page = self.client.get(link)
        self.assertContains(page, "Connect school.edu to AIDL")
        go = self.client.get(page.context["go_url"])
        self.assertEqual(go.status_code, 302)
        q = {k: v[0] for k, v in parse_qs(urlparse(go["Location"]).query).items()}
        self.assertIn("admin.directory.user.readonly", q["scope"])
        self.assertEqual(q["access_type"], "offline")
        tokens = {"id_token": self._id_token(**token_args), "access_token": "access-1", "scope": granted,
                  "refresh_token": "refresh-it"}
        with mock.patch(P + "google_oauth.exchange_tokens", return_value=tokens):
            return self.client.get("/api/google-classroom/callback/", {"code": "abc", "state": q["state"]})

    def _count(self, units=("/Students",)):
        return self.client.post("/api/google-classroom/admin/school/action/",
                                {"action": "count", "unit": list(units)}, follow=True)

    def _send(self):
        return self.client.post("/api/google-classroom/admin/school/action/", {"action": "send"}, follow=True)

    # --- who can connect ---

    def test_not_enterprise_sees_the_upgrade_note(self):
        Organization.objects.update(plan="basic")
        self._sign_in(self.teacher)
        page = self.client.get("/api/google-classroom/admin/school/")
        self.assertContains(page, "part of the <b>Enterprise</b> plan")
        self.assertNotIn("it_admin_link", page.context)

    def test_teacher_gets_a_link_for_the_it_admin(self):
        self._sign_in(self.teacher)
        page = self.client.get("/api/google-classroom/admin/school/")
        self.assertContains(page, "I'm the IT admin, connect with Google")
        self.assertContains(page, "/api/google-classroom/school/it-admin/?t=")

    def test_it_admin_connects_and_becomes_an_aidl_admin(self):
        back = self._connect()
        self.assertEqual(back.status_code, 302)
        self.assertTrue(back["Location"].startswith("/api/google-classroom/admin/school/"))
        row = SchoolDirectory.objects.get(organization_id=str(self.org.pk))
        it = AIDLUser.objects.get(email="it@school.edu")
        self.assertEqual((it.role, it.organization_id), (AIDLUser.Role.ADMIN, str(self.org.pk)))
        self.assertEqual((row.admin_user_id, row.domain), (str(it.pk), "school.edu"))
        self.assertNotEqual(row.refresh_token, "refresh-it")  # stored encrypted
        self.assertEqual(open_refresh_token(row.refresh_token), "refresh-it")
        # Signed in: the page now lists the school's folders.
        page = self.client.get("/api/google-classroom/admin/school/")
        self.assertContains(page, "Connected by it@school.edu")
        self.assertContains(page, "/Students/Grade 9")

    def test_a_teacher_who_isnt_a_workspace_admin_is_refused(self):
        self.fake.admin = False
        back = self._connect(sub="901", email="ravi.teacher@school.edu")
        self.assertEqual(back.status_code, 403)
        self.assertContains(back, "Only your IT admin can connect the whole school", status_code=403)
        self.assertFalse(SchoolDirectory.objects.exists())
        self.assertFalse(AIDLUser.objects.filter(email="ravi.teacher@school.edu").exists())

    def test_another_schools_admin_is_refused(self):
        back = self._connect(email="it@other.edu", hd="other.edu")
        self.assertContains(back, "@school.edu", status_code=400)
        self.assertFalse(SchoolDirectory.objects.exists())

    def test_unticked_permissions_are_refused(self):
        back = self._connect(granted="openid email profile")
        self.assertContains(back, "tick every box", status_code=400)
        self.assertFalse(SchoolDirectory.objects.exists())

    def test_broken_or_old_link(self):
        self.assertContains(self.client.get("/api/google-classroom/school/it-admin/", {"t": "nope"}),
                            "isn&#x27;t valid", status_code=400)
        token = school_directory.link_token(self.org, "school.edu")
        with mock.patch("django.core.signing.time.time", return_value=time.time() + 8 * 86400):
            self.assertContains(self.client.get("/api/google-classroom/school/it-admin/", {"t": token}),
                                "expired", status_code=400)

    # --- students ---

    def test_count_takes_the_folder_and_its_subfolders_once_without_suspended(self):
        self._connect()
        resp = self._count(["/Students", "/Nope"])
        self.assertContains(resp, "3 students found")  # Ravi, Meera (twice in Google), Zoya; not the suspended one
        row = SchoolDirectory.objects.get()
        self.assertEqual(row.student_units, ["/Students"])  # an unknown folder is ignored
        self.assertEqual(row.preview, ["Meera Shah", "Ravi Kumar", "Zoya Khan"])
        self.assertContains(resp, "Send AIDL to all 3 students")

    def test_count_needs_a_folder(self):
        self._connect()
        self.assertContains(self._count([]), "Tick the folders that hold your students")

    def test_send_adds_everyone_directly_once(self):
        self._connect()
        self._count()
        resp = self._send()
        self.assertEqual(sorted(self.fake.added), ["s2", "s4"])  # Ravi was in the class already
        self.assertEqual(self.fake.invited, [])
        row = SchoolDirectory.objects.get()
        self.assertEqual((row.status, row.send_all), (school_directory.DONE, True))
        self.assertEqual(row.progress, {"total": 3, "in_class": 1, "added": 2, "invited": 0, "failed": 0,
                                        "class_full": 0})
        self.assertContains(resp, "Check for new students now")
        # Again: nobody twice.
        self._send()
        self.assertEqual(sorted(self.fake.added), ["s2", "s4"])
        self.assertEqual(SchoolDirectory.objects.get().progress["in_class"], 3)

    def test_students_google_wont_add_directly_are_invited(self):
        self.fake.forbid = {"s4"}
        self._connect()
        self._count()
        self._send()
        self.assertEqual((self.fake.added, self.fake.invited), (["s2"], ["s4"]))

    def test_a_full_class_stops_adding(self):
        self._connect()
        self._count()
        with mock.patch.dict("os.environ", {"GOOGLE_CLASSROOM_CLASS_LIMIT": "3"}):  # Asha + Ravi + one more
            self._send()
        row = SchoolDirectory.objects.get()
        self.assertEqual((row.progress["added"], row.progress["class_full"]), (1, 1))

    def test_no_aidl_class_yet(self):
        AidlClass.objects.update(course_id="")
        self._connect()
        self._count()
        self.assertContains(self._send(), "AIDL class isn")
        self.assertEqual(self.fake.added, [])

    def test_learner_sync_runs_after_adding(self):
        from integrations.google_classroom import learners

        self._connect()
        self._count()
        learners.sync.reset_mock()
        self._send()
        learners.sync.assert_called_with(self.org)  # after the students were added (the Admin Center also syncs)

    # --- every day ---

    def test_daily_check_adds_new_students(self):
        from integrations.google_classroom.poller import sync_all

        self._connect()
        self._count()
        self._send()
        self.fake.users["/Students"].append(self.fake.user("s5", "New Kid"))
        sync_all()  # checked less than a day ago: nothing yet
        self.assertNotIn("s5", self.fake.added)
        SchoolDirectory.objects.update(last_sync_at=timezone.now() - timedelta(hours=25))
        totals = sync_all()
        self.assertIn("s5", self.fake.added)
        self.assertEqual(totals.get("school_added"), 1)

    def test_daily_check_stops_after_a_downgrade(self):
        self._connect()
        self._count()
        self._send()
        Organization.objects.update(plan="basic")
        SchoolDirectory.objects.update(last_sync_at=timezone.now() - timedelta(hours=25))
        self.fake.users["/Students"].append(self.fake.user("s5", "New Kid"))
        school_directory.sync_due()
        self.assertNotIn("s5", self.fake.added)
        self.assertIn("Enterprise", SchoolDirectory.objects.get().error)

    def test_disconnect(self):
        self._connect()
        self.client.post("/api/google-classroom/admin/school/action/", {"action": "disconnect"})
        self.assertFalse(SchoolDirectory.objects.exists())

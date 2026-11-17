# AIDL integrations (LMS platforms)

Each platform lives in its own folder, so the three of us can work without
touching each other's files.

```
integrations/
├── lti_core/      LTI 1.3 — shared by Moodle, Canvas, Schoology, Brightspace, Blackboard
│   ├── models.py      LtiPlatform (one LMS site = one AIDL organization), LtiLoginState
│   ├── launch.py      the 2-step LTI login: login_redirect() and verify_launch()
│   ├── keys.py        AIDL's own key pair → /api/lti/jwks/
│   └── management/commands/lti_new_key.py
└── moodle/        everything Moodle-only
    ├── views.py       /api/moodle/login/, /api/moodle/launch/, /api/moodle/
    ├── accounts.py    launch → AIDLUser + Organization (same rules as Slack)
    ├── session.py     the "aidl_moodle" sign-in cookie
    ├── templates/moodle/
    ├── tests.py
    └── management/commands/moodle_register.py
```

## Team rules

- Work only in your platform's folder. Business rules (cards, licences, plans)
  stay in `api/` and are *called* from here, not copied or changed.
- A new platform touches only 3 shared files, one line each: `config/settings.py`
  (INSTALLED_APPS), `config/urls.py`, and `requirements.txt` if needed.
- One branch per platform (`feature/moodle-lti`, ...). `git pull origin main`
  before you start; merge with a pull request.
- Changing `lti_core/` affects every LMS: tell the others first.

## Moodle — progress

| Step | What | Status |
|---|---|---|
| 1 | Sign in from a Moodle course (LTI 1.3 launch) | ✅ done |
| 2 | Learner flow (policy cards → Traffic Light → Highway Code → licence) | next |
| 3 | Send the result to the Moodle gradebook | |
| 4 | Admin Center for Moodle admins | |
| 5 | Class list from Moodle (who's in the programme) | |

## Connect a Moodle site (step 1)

`<BACKEND>` = `BACKEND_URL` (local: `http://localhost:8000`; set `BACKEND_URL` in `.env`).

1. In Moodle (as site admin): **Site administration → Plugins → Activity modules →
   External tool → Manage tools → configure a tool manually**:

   | Field | Value |
   |---|---|
   | Tool name | AIDL |
   | Tool URL | `<BACKEND>/api/moodle/launch/` |
   | LTI version | LTI 1.3 |
   | Public key type | Keyset URL |
   | Public keyset | `<BACKEND>/api/lti/jwks/` |
   | Initiate login URL | `<BACKEND>/api/moodle/login/` |
   | Redirection URI(s) | `<BACKEND>/api/moodle/launch/` |
   | Tool configuration usage | Show in activity chooser and as a preconfigured tool |
   | Default launch container | **New window** (AIDL inside a frame needs https and third-party cookies) |
   | Privacy → share name / email | Always |

2. Save, then click the tool's **"View configuration details"** icon. Copy the
   **Platform ID**, **Client ID** and **Deployment ID**, and register the site:

   ```powershell
   python manage.py moodle_register --site <Platform ID> --client-id <Client ID> --deployment-id <Deployment ID> --name "My test Moodle"
   ```

3. In a course: **Add an activity → AIDL**. Open it as a teacher first: that
   creates the AIDL organization and makes the teacher its owner admin.
   Students can open it after that.

Moodle never calls AIDL directly during login (the browser does), but AIDL
downloads Moodle's keys from `<site>/mod/lti/certs.php`. So a local backend
works with a local Moodle, or with the public sandbox at https://sandbox.moodledemo.net
(it resets every hour, so repeat steps 1–2 each time).

`LTI_PRIVATE_KEY` (from `python manage.py lti_new_key`) is optional for
sign-in; the gradebook step will need it.

## Tests

```powershell
python manage.py test integrations.moodle
```

---

# Google Classroom (Google Workspace for Education)

Not LTI: Google Classroom uses **Sign in with Google** (OAuth 2.0 / OpenID Connect).
Everything is in `integrations/google_classroom/`.

| Step | What | Status |
|---|---|---|
| 1 | Sign up / sign in with Google ("Add to Google Classroom") | ✅ done |
| 2 | Read the teacher's classes and students (Classroom API) | ✅ done |
| 3 | Teacher chooses classes → their students become AIDL learners | ✅ done |
| 4 | Learner flow for Google students (cards → licence) from a shared link | ✅ done |
| 5 | Admin Center, core: setup steps, team, admins, plan, coverage | ✅ done |
| 6 | Admin Center, policy: policy cards, own AUP, regulators, apps, logo | ✅ done |

## Sign-up / sign-in rules

- One button does both: a new Google account signs up, a known one signs in.
- One school = one AIDL organization, found by the Google Workspace domain
  (`hd`, verified by Google). The first person from `school.edu` creates it and
  becomes owner admin; everyone else from `school.edu` joins as a learner.
- A personal Gmail account (no domain) gets its own one-person organization.
- `enroll_as=individual` makes a personal AIDL account with no organization.
- An existing AIDL account with the same **verified** email is reused.

## Endpoints

| | |
|---|---|
| `GET /api/google-classroom/login/?enroll_as=organization` | `{auth_url}`; add `&redirect=1` to go straight to Google |
| `GET /api/google-classroom/callback/` | Google returns here → frontend `/auth/callback?access_token=…&mode=google_classroom` (or `?error=…`) |
| `GET /api/google-classroom/classes/` | Step 2 page: an admin's classes + students (Connect / Refresh buttons) |
| `GET /api/google-classroom/classes/connect/` | → Google, asking for the read-only Classroom permissions (offline) |
| `GET /api/google-classroom/api/classes/` | same as JSON, `Authorization: Bearer <AIDL access token>` |
| `POST /api/google-classroom/api/classes/sync/` | fetch from Google again, JSON |
| `POST /api/google-classroom/classes/choose/` | step 3: `course_id`, `on=1` add the class to AIDL / `on=0` take it out |
| `POST /api/google-classroom/api/classes/choose/` | same, JSON `{"course_id": "...", "in_programme": true}` |
| `POST /api/google-classroom/classes/setup/` | step 4: approve the 2 starter cards / start the programme |
| `GET /api/google-classroom/learn/` | step 4: the link teachers post in Classroom → Google sign-in → learner flow |
| `POST /api/google-classroom/learn/action/` | a learner's button |
| `GET /api/google-classroom/admin/` | step 5: Admin Center home (plan, 5 setup steps, student link) |
| `GET /api/google-classroom/admin/team/` | learners' progress per class; remove / add back |
| `GET /api/google-classroom/admin/admins/` | add / remove admins and their permissions |
| `GET /api/google-classroom/admin/plans/` · `admin/coverage.csv` | upgrade (website plans page) · coverage export |
| `GET /api/google-classroom/admin/policy/` · `policy/<n>/` | step 6: company → the 14 policy cards; one card's questions + approve |
| `GET /api/google-classroom/admin/regulators/` · `upload/` · `logo/` · `apps/` | regulators · own AUP (paid) · logo (paid) · AI / IT apps |

Step 2 is for AIDL admins (teachers) only. They allow three read-only
permissions once: classes, class rosters, students' emails. The refresh token
is stored encrypted (`GoogleCredential`), so **Refresh** works without Google
asking again. Classes and students are copied into `GoogleCourse` /
`GoogleStudent`.

Step 3: **Add to AIDL** on a class makes each of its students an AIDL learner
of the teacher's organization (one seat each, `microsoft_id = google:<id>`, the
same id Google sign-in uses), or links the AIDL account they already have
(same Google account or email). When they sign in with Google they land in
the school. Students in another AIDL organization stay there; with no free
seats they are listed as "No free seat". Refresh adds new students of chosen
classes. **Remove from AIDL** only stops adding students; nobody's account or
progress is deleted.

Step 4: on the classes page the teacher approves the Traffic Light Check and
the Highway Code, presses **Start**, and posts the **link for your students**
(`<BACKEND>/api/google-classroom/learn/`) in their class. A student opens it,
signs in with Google (identity only, no Classroom permissions) and gets the
same flow as Moodle (policy cards → Traffic Light → Highway Code → licence,
`integrations/moodle/flow.py`). Google accounts that aren't in a chosen class
are turned away.

Step 5 (Admin Center, core): `admin_center.py` + `admin_views.py`, same rules as
Slack / Teams through `api/` (`admin_perms`, `licensing`, `plans`). Admins who
open the student link land here. The team is counted with
`licensing.learners()`, because `admin_setup.summary()` only counts Slack people.
Removing a learner sets the shared `slack_left_at` field (licence suspended;
Refresh won't add them back); "Add back" clears it if there's a seat.

Step 6 (Admin Center, AI policy): `policy_views.py`, the same `api/` modules
and form helpers as the Teams Admin Center (`policy_cards`, `aup_store`,
`aup_upload`, `regulators`, `teams_portal._policy_context` / `_read_policy`),
with our own copies of its pages. An approved card reaches Google learners in
their flow the next time they open the link (the shared delivery only posts
to Slack / Teams people, so nothing is posted). App *requests* come from
Slack / Teams cards only, so the apps page here is the list + add.

## Set up Google (once)

1. https://console.cloud.google.com → new project (e.g. "AIDL").
2. **APIs & Services → OAuth consent screen**: User type *External*, app name AIDL,
   scopes `openid`, `email`, `profile`. While in *Testing*, add your test Google
   accounts under *Test users*.
3. **Credentials → Create credentials → OAuth client ID → Web application**.
   Authorized redirect URI: `<BACKEND>/api/google-classroom/callback/`
   (local: `http://localhost:8000/api/google-classroom/callback/`).
4. Put in `.env`:

   ```
   GOOGLE_CLIENT_ID=....apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   # optional, default <BACKEND_URL>/api/google-classroom/callback/
   GOOGLE_CLASSROOM_REDIRECT_URI=http://localhost:8000/api/google-classroom/callback/
   ```

5. Step 2: **APIs & Services → Library → Google Classroom API → Enable**, and in
   **Google Auth Platform → Data access → Add or remove scopes** add
   `classroom.courses.readonly`, `classroom.rosters.readonly`, `classroom.profile.emails`.
6. `python manage.py migrate google_classroom`

## Tests

```powershell
python manage.py test integrations.google_classroom
```

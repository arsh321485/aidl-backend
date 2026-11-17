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

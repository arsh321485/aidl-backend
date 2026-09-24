# AIDL Backend (Django + MongoDB)

Python 3.13 + **Django 6** + MongoDB Atlas + Teams/Microsoft OAuth.

## Stack

- Django + Django REST Framework
- MongoDB (`django-mongodb-backend`)
- Microsoft Teams signup (MSAL + JWT)

## Project structure

```
Backend-AIDL-Project/
├── manage.py
├── .env
├── requirements.txt
├── config/                 # Django settings + urls
├── api/
│   ├── models.py           # AIDLUser, Organization, ...
│   ├── serializers.py      # SignupSerializer, LoginSerializer, AIDLUserSerializer
│   ├── auth_views.py       # website signup/login + Teams login/callback/me
│   ├── microsoft_auth.py
│   ├── auth_jwt.py
│   ├── views.py
│   └── urls.py
└── mongo_migrations/
```

## Setup

```powershell
cd e:\office-projects\Backend-AIDL-Project
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver localhost:8000
```

Open:
- API index: http://localhost:8000/api/
- Health: http://localhost:8000/api/health/

Frontend local API base:

```env
VITE_API_BASE=http://localhost:8000
```

## Website signup / login APIs (for frontend)

| Method | Endpoint | Auth | Notes |
|--------|----------|------|-------|
| POST | `/api/auth/signup/` | none | create account, returns JWTs + user |
| POST | `/api/auth/signin/` | none | email + password + enroll_as, returns JWTs + user |
| POST | `/api/auth/login/` | none | same as signin (alias) |
| GET | `/api/auth/me/` | Bearer | current user profile |
| POST | `/api/auth/refresh/` | none | body: `refresh_token` |
| POST | `/api/auth/logout/` | none | client deletes tokens |

Local: `http://localhost:8000/api/auth/signup/`  
Production: `https://aidl-backend.onrender.com/api/auth/signup/`

`POST /api/auth/signup/` JSON body:

```json
{
  "enroll_as": "individual",
  "first_name": "Alex",
  "last_name": "Morgan",
  "email": "alex@company.com",
  "password": "CreateA-StrongPass1",
  "confirm_password": "CreateA-StrongPass1",
  "mobile_number": "+15550000000",
  "country": "United States",
  "state": "California",
  "city": "San Francisco",
  "license_class": "class_l",
  "organization_name": ""
}
```

For organization signup set `"enroll_as": "organization"` and send `organization_name`.

`201` success shape:

```json
{
  "message": "Account created successfully.",
  "access_token": "<jwt>",
  "refresh_token": "<jwt>",
  "token_type": "Bearer",
  "user": { "id": "...", "email": "alex@company.com", "enroll_as": "individual" }
}
```

Store `access_token` and send it as `Authorization: Bearer <access_token>` on later calls.

## Teams auth APIs (for frontend)

| Method | Endpoint | Notes |
|--------|----------|-------|
| GET | `/api/auth/teams/login/?enroll_as=organization` | returns `auth_url` |
| GET | `/api/auth/teams/callback/` | Microsoft redirect (do not call from FE) |
| GET | `/api/auth/teams/launch/` | Bearer — returns `teams_url` |
| GET | `/api/auth/me/` | Bearer token |
| POST | `/api/auth/refresh/` | body: `refresh_token` |
| POST | `/api/auth/logout/` | client clears tokens |

`enroll_as`: `individual` | `organization`

After Microsoft login, backend redirects to `AUTH_SUCCESS_REDIRECT` with query params:

| Param | Meaning |
|-------|---------|
| `access_token`, `refresh_token` | JWTs to store on the frontend |
| `teams_url` | **Open this.** AIDL channel deep link when available, else falls back to Teams chat/home |
| `teams_channel_url` | Present only when the AIDL channel deep link was resolved (same value as `teams_url` in that case) |
| `teams_platform_url` | Always the plain Teams chat/home URL — use for an explicit "open Teams home" link |
| `landed_on` | `"channel"` or `"chat"` — tells the FE which one `teams_url` actually is |
| `enroll_as`, `mode`, `email`, `full_name`, `open_teams`, `teams_connected` | as before |

`GET /api/auth/teams/launch/` and `GET /api/auth/me/` return the same `teams_url` / `teams_platform_url` / `teams_channel_url` / `landed_on` shape for post-login use (e.g. a "Reopen Teams" button).

The AIDL channel is only created/found when `MS_AIDL_TEAM_ID` (an existing Microsoft Team's group id) is set — Graph can create a **channel** inside a team but not a new **Team**. Until that env var is set, `landed_on` stays `"chat"` and everyone falls back to Teams chat/home.

## Azure / redirect URLs

Two different URLs — do not mix them:

| Env var | Points to | Example |
|---------|-----------|---------|
| `MS_REDIRECT_URI` | Backend (Microsoft → API) | `https://aidl-backend.onrender.com/api/auth/teams/callback/` |
| `AUTH_SUCCESS_REDIRECT` | Frontend (API → Vue `/auth/callback`) | `https://aidl-frontend-8owk.vercel.app/auth/callback` |

### Local laptop

```env
MS_REDIRECT_URI=http://localhost:8000/api/auth/teams/callback/
AUTH_SUCCESS_REDIRECT=http://localhost:5173/auth/callback
# or http://localhost:5184/auth/callback if that is your Vite port
```

### Production (Render)

See [`RENDER_ENV.txt`](RENDER_ENV.txt) — paste into Render Dashboard → Environment, then restart.

Azure App Registration Redirect URI must match `MS_REDIRECT_URI` exactly.

Confirm: `GET https://aidl-backend.onrender.com/api/`  
→ `microsoft_redirect_uri` and `auth_success_redirect` must not be localhost in production.

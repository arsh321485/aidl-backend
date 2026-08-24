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
│   ├── models.py           # AIDLUser, OAuthState
│   ├── auth_views.py       # Teams login/callback/me
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

After Microsoft login, backend redirects to `AUTH_SUCCESS_REDIRECT` with `access_token`, `refresh_token`, and `teams_url`.

## Azure keys (real Microsoft login)

### Local

```env
MS_REDIRECT_URI=http://localhost:8000/api/auth/teams/callback/
AUTH_SUCCESS_REDIRECT=http://localhost:5173/auth/callback
```

### Production (Render — `aidl-backend.onrender.com`)

Set in **Render Dashboard → Environment** (`.env` is gitignored and is NOT pushed):

```env
MS_REDIRECT_URI=https://aidl-backend.onrender.com/api/auth/teams/callback/
MICROSOFT_REDIRECT_URI=https://aidl-backend.onrender.com/api/auth/teams/callback/
AUTH_SUCCESS_REDIRECT=https://aidl-frontend-8owk.vercel.app/auth/callback
ALLOWED_HOSTS=aidl-backend.onrender.com
DEBUG=False
```

Azure App Registration → Redirect URIs must include the same production callback URL.

Confirm after deploy: `GET https://aidl-backend.onrender.com/api/` → check `microsoft_redirect_uri` is the Render URL, not localhost.

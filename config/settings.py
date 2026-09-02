"""
Django settings for Backend-AIDL-Project.
Python 3.13 + Django 6 + MongoDB
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-change-me-in-production",
)

DEBUG = os.getenv("DEBUG", "True").lower() in ("1", "true", "yes")

# Production host: aidl-backend.onrender.com
_DEFAULT_ALLOWED_HOSTS = (
    "127.0.0.1,localhost,aidl-backend.onrender.com,vaptbackend.secureitlab.com"
)
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS).split(",")
    if host.strip()
]

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://aidl-frontend-8owk.vercel.app")
# After Teams login, browser lands on frontend with tokens
AUTH_SUCCESS_REDIRECT = os.getenv(
    "AUTH_SUCCESS_REDIRECT",
    "https://aidl-frontend-8owk.vercel.app/auth/callback",
)

# Microsoft / Teams OAuth (Azure Entra ID)
# Supports both MS_* and MICROSOFT_* env names.
# Default = production Render callback (override with localhost in local .env).
_DEFAULT_MS_REDIRECT_URI = (
    "https://aidl-backend.onrender.com/api/auth/teams/callback/"
)
MS_TENANT_ID = os.getenv("MS_TENANT_ID") or os.getenv("MICROSOFT_TENANT_ID") or "common"
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID") or os.getenv("MICROSOFT_CLIENT_ID") or ""
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET") or os.getenv("MICROSOFT_CLIENT_SECRET") or ""
MS_REDIRECT_URI = (
    os.getenv("MS_REDIRECT_URI")
    or os.getenv("MICROSOFT_REDIRECT_URI")
    or _DEFAULT_MS_REDIRECT_URI
)
_DEFAULT_MS_SCOPES = (
    "User.Read,"
    "Team.ReadBasic.All,"
    "Team.Create,"
    "Channel.ReadBasic.All,"
    "Channel.Create,"
    "ChannelMessage.Send,"
    "TeamsTab.Create,"
    "Group.ReadWrite.All"
)
MS_SCOPES = [
    s.strip()
    for s in os.getenv("MS_SCOPES", _DEFAULT_MS_SCOPES).split(",")
    if s.strip()
]

# AIDL Microsoft Team + channel (auto-created on login when possible)
# Optional override: set MS_AIDL_TEAM_ID to use an existing Team instead of creating "AIDL"
MS_AIDL_TEAM_ID = (
    os.getenv("MS_AIDL_TEAM_ID") or os.getenv("MICROSOFT_AIDL_TEAM_ID") or ""
).strip()
MS_AIDL_TEAM_NAME = (
    os.getenv("MS_AIDL_TEAM_NAME") or os.getenv("MICROSOFT_AIDL_TEAM_NAME") or "AIDL"
).strip() or "AIDL"
MS_AIDL_CHANNEL_NAME = (
    os.getenv("MS_AIDL_CHANNEL_NAME")
    or os.getenv("MICROSOFT_AIDL_CHANNEL_NAME")
    or "aidl dashboard"
).strip() or "aidl dashboard"
MS_AIDL_AUTO_CREATE_TEAM = os.getenv("MS_AIDL_AUTO_CREATE_TEAM", "True").lower() in (
    "1",
    "true",
    "yes",
)

# AIDL Teams app content (Adaptive Cards + tab pages)
MS_TEAMS_APP_BASE_URL = (
    os.getenv("MS_TEAMS_APP_BASE_URL") or "https://aidl-backend.onrender.com/api/teams"
).strip().rstrip("/")
MS_SEND_WELCOME_CARD = os.getenv("MS_SEND_WELCOME_CARD", "True").lower() in (
    "1",
    "true",
    "yes",
)
MS_AIDL_INSTALL_CHANNEL_TABS = os.getenv("MS_AIDL_INSTALL_CHANNEL_TABS", "True").lower() in (
    "1",
    "true",
    "yes",
)
AIDL_ORG_DISPLAY_NAME = (
    os.getenv("AIDL_ORG_DISPLAY_NAME") or "Northwind Logistics"
).strip() or "Northwind Logistics"
AIDL_POLICY_URL = (
    os.getenv("AIDL_POLICY_URL") or "https://www.spinifexit.com/acceptable-use-policy"
).strip()
AIDL_LOGO_URL = (
    os.getenv("AIDL_LOGO_URL")
    or "https://aidl-backend.onrender.com/static/aidl/logo.svg"
).strip()
_default_policy_entities = (
    "SpinifexIT Global Pty Ltd,"
    "SpinifexIT North America Inc.,"
    "SpinifexIT Solutions UK Limited,"
    "SpinifexIT Philippines Inc.,"
    "SpinifexIT Singapore Pte. Ltd.,"
    "SpinifexIT Deutschland GmbH"
)
AIDL_POLICY_ENTITIES = [
    item.strip()
    for item in os.getenv("AIDL_POLICY_ENTITIES", _default_policy_entities).split(",")
    if item.strip()
]

# JWT for API auth
JWT_SECRET = os.getenv("JWT_SECRET", SECRET_KEY)
JWT_ACCESS_MINUTES = int(os.getenv("JWT_ACCESS_MINUTES", "60"))
JWT_REFRESH_DAYS = int(os.getenv("JWT_REFRESH_DAYS", "7"))

INSTALLED_APPS = [
    "django_mongodb_backend",
    "config.apps.MongoAdminConfig",
    "config.apps.MongoAuthConfig",
    "config.apps.MongoContentTypesConfig",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "api",
]

MIGRATION_MODULES = {
    "admin": "mongo_migrations.admin",
    "auth": "mongo_migrations.auth",
    "contenttypes": "mongo_migrations.contenttypes",
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# MongoDB (official Django MongoDB Backend)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/?directConnection=true")
DB_NAME = os.getenv("DB_NAME", os.getenv("MONGO_DB_NAME", "aidl"))
MONGO_DB_NAME = DB_NAME

DATABASES = {
    "default": {
        "ENGINE": "django_mongodb_backend",
        "HOST": MONGO_URI,
        "NAME": DB_NAME,
    }
}

DATABASE_ROUTERS = ["django_mongodb_backend.routers.MongoRouter"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django_mongodb_backend.fields.ObjectIdAutoField"

CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        (
            "http://127.0.0.1:8000,"
            "http://localhost:8000,"
            "http://localhost:3000,"
            "http://localhost:5173,"
            "http://localhost:5184,"
            "https://aidl-frontend-8owk.vercel.app"
        ),
    ).split(",")
    if origin.strip()
]

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
}

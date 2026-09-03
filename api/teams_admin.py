"""AIDL Admin Center tab navigation + dynamic payloads (DB-backed)."""

from .org_service import (
    build_admin_dashboard_from_db,
    build_admin_tab_payload,
)
from .teams_cards import first_name, logo_url, org_display_name


# In-page Admin Center tabs (Option C — clickable pills inside tab HTML).
ADMIN_TABS = (
    ("home", "Home", "🏠"),
    ("add-admin", "Add Admin", "👤"),
    ("policy", "Policy", "📄"),
    ("cards", "Cards", "💳"),
    ("ai-apps", "AI Apps", "🤖"),
    ("it-apps", "IT Apps", "💻"),
)


def build_admin_dashboard(
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
) -> dict:
    return build_admin_dashboard_from_db(
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )


def build_admin_placeholder(
    tab: str,
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
) -> dict:
    payload = build_admin_tab_payload(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    # Keep template-compatible keys
    return {
        "full_name": payload.get("full_name", ""),
        "first_name": payload.get("first_name") or first_name(full_name),
        "org_name": payload.get("org_name") or org_display_name(org_name),
        "logo_url": payload.get("logo_url") or logo_url(),
        "title": payload.get("title") or tab,
        "body": payload.get("body") or "",
        "heading": payload.get("heading") or payload.get("title") or tab,
        "items": payload.get("items") or [],
        "policy_url": payload.get("policy_url") or "",
        "signed_count": payload.get("signed_count"),
        "unsigned_count": payload.get("unsigned_count"),
        "admin_count": payload.get("admin_count"),
        "admin_seat_limit": payload.get("admin_seat_limit"),
        "can_add": payload.get("can_add"),
        "email": payload.get("email") or email,
    }

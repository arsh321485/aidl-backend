"""Microsoft Teams app tabs + Adaptive Card JSON APIs."""

import json

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .auth_jwt import create_access_token
from .auth_views import JWTAuthentication
from .models import AIDLUser
from .teams_admin import (
    ADMIN_TABS,
    build_admin_dashboard,
    build_admin_placeholder,
)
from .teams_cards import (
    CARD_BUILDERS,
    TEAMS_TABS,
    _licence_facts,
    build_card,
    build_tab_content,
    org_display_name,
)
from .teams_channel_tabs import ensure_aidl_channel_tabs, is_aidl_dashboard_channel, target_channel_name
from .teams_messaging import send_channel_adaptive_card
from .teams_invites import send_user_invite
from .org_service import build_user_dashboard_payload, replace_welcome_card


def _teams_base_url(request) -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    return request.build_absolute_uri("/api/teams").rstrip("/")


def _request_user_bits(request) -> dict:
    """Resolve signed-in identity from query, JWT, or Mongo AIDLUser."""
    get = getattr(request, "query_params", None) or request.GET
    full_name = (get.get("full_name") or "").strip()
    org_name = (get.get("org_name") or "").strip()
    email = (get.get("email") or "").strip()

    user = getattr(request, "user", None)
    db_user = None
    if user is not None and getattr(user, "is_authenticated", False) and isinstance(user, AIDLUser):
        db_user = user
        full_name = full_name or (user.full_name or "")
        email = email or (user.email or "")
        org_name = org_name or (user.organization_name or "")

    if email and db_user is None:
        try:
            db_user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
            if db_user:
                full_name = full_name or db_user.full_name
                email = email or db_user.email
                org_name = org_name or db_user.organization_name
        except Exception:  # noqa: BLE001
            pass

    return {
        "full_name": full_name,
        "org_name": org_display_name(org_name),
        "email": email,
        "user": db_user,
    }


def _admin_tab_ids() -> set[str]:
    return {tab_id for tab_id, _label, _icon in ADMIN_TABS}


def _admin_tab_context(request, active_tab: str, user_bits: dict) -> dict:
    base = _teams_base_url(request)
    q_parts = []
    if user_bits.get("full_name"):
        from urllib.parse import urlencode

        q = urlencode(
            {
                k: v
                for k, v in {
                    "full_name": user_bits.get("full_name"),
                    "org_name": user_bits.get("org_name"),
                    "email": user_bits.get("email"),
                }.items()
                if v
            }
        )
        q_parts = [q] if q else []

    suffix = f"?{q_parts[0]}" if q_parts else ""
    tabs = []
    for tab_id, label, icon in ADMIN_TABS:
        tabs.append(
            {
                "id": tab_id,
                "label": label,
                "icon": icon,
                "url": f"{base}/tabs/{tab_id}/{suffix}",
                "active": tab_id == active_tab,
            }
        )

    dashboard = build_admin_dashboard(
        full_name=user_bits.get("full_name", ""),
        org_name=user_bits.get("org_name", ""),
        email=user_bits.get("email", ""),
        user=user_bits.get("user"),
    )
    placeholder = build_admin_placeholder(
        active_tab,
        full_name=user_bits.get("full_name", ""),
        org_name=user_bits.get("org_name", ""),
        email=user_bits.get("email", ""),
        user=user_bits.get("user"),
    )
    return {
        "tabs": tabs,
        "active_tab": active_tab,
        "org_name": user_bits.get("org_name") or org_display_name(),
        "teams_base_url": base,
        "dashboard": dashboard,
        "placeholder": placeholder,
        "dashboard_json": json.dumps(dashboard),
        "placeholder_json": json.dumps(placeholder),
        "next_tab_url": f"{base}/tabs/add-admin/{suffix}",
    }


def _render_learner_tab(request, tab: str, user_bits: dict):
    """The Learner's 4-tab dashboard (Home / Learner's Permit / Highway Code /
    Traffic Light Check) — same nav-pill design as Admin Center, personalized
    plain HTML rendered client-side from JSON fetched via teams_tab_content_json
    (no Adaptive Cards / Action.OpenUrl here, so nothing pops out of the tab)."""
    from urllib.parse import urlencode

    from .teams_cards import TEAMS_TABS as LEARNER_TABS

    base = _teams_base_url(request)
    q = urlencode(
        {
            k: v
            for k, v in {
                "full_name": user_bits.get("full_name"),
                "org_name": user_bits.get("org_name"),
                "email": user_bits.get("email"),
            }.items()
            if v
        }
    )
    suffix = f"?{q}" if q else ""
    tabs = [
        {
            "id": tab_id,
            "label": label,
            "icon": icon,
            "url": f"{base}/tabs/{tab_id}/{suffix}",
            "active": tab_id == tab,
        }
        for tab_id, label, icon in LEARNER_TABS
    ]
    context = {
        "tabs": tabs,
        "active_tab": tab,
        "org_name": user_bits.get("org_name") or org_display_name(),
        "teams_base_url": base,
        "content_json": json.dumps(
            build_tab_content(
                tab,
                full_name=user_bits.get("full_name", ""),
                org_name=user_bits.get("org_name", ""),
                user=user_bits.get("user"),
            )
            or {}
        ),
    }
    return render(request, "teams/tab.html", context)


@xframe_options_exempt
@never_cache
def teams_tab_page(request, tab: str):
    """
    Option C: Admin Center in-page UI (Home + clickable pills).
    Channel login/redirect flow unchanged — only the Home tab content.
    """
    user_bits = _request_user_bits(request)
    db_user = user_bits.get("user")
    is_learner = db_user is not None and db_user.role == AIDLUser.Role.LEARNER

    if tab in _admin_tab_ids():
        # "home" is the one slug both the Admin Center and the Learner
        # dashboard use — a Learner must get their own Home, not Admin
        # Center's, so this is the one case that doesn't just fall through.
        if not (tab == "home" and is_learner):
            context = _admin_tab_context(request, tab, user_bits)
            return render(request, "teams/admin.html", context)

    if tab == "user-dashboard":
        if db_user is None:
            context = {
                "org_name": user_bits.get("org_name") or org_display_name(),
                "empty": True,
            }
        else:
            context = build_user_dashboard_payload(db_user)
        return render(request, "teams/user_dashboard.html", context)

    # Learner's 4-tab dashboard (Home / Learner's Permit / Highway Code /
    # Traffic Light Check) — still reachable even for a non-learner viewer
    # (e.g. previewing without ?email=), just without live personalization.
    if tab not in CARD_BUILDERS:
        raise Http404("Unknown tab")
    return _render_learner_tab(request, tab, user_bits)


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_admin_json(request, tab: str):
    """Dynamic Admin Center JSON — user name from query / Teams / DB."""
    if tab not in _admin_tab_ids():
        return Response({"error": "unknown_tab"}, status=status.HTTP_404_NOT_FOUND)
    user_bits = _request_user_bits(request)
    if tab == "home":
        return Response(
            {
                "tab": tab,
                "dashboard": build_admin_dashboard(
                    full_name=user_bits["full_name"],
                    org_name=user_bits["org_name"],
                    email=user_bits["email"],
                    user=user_bits.get("user"),
                ),
            }
        )
    return Response(
        {
            "tab": tab,
            "placeholder": build_admin_placeholder(
                tab,
                full_name=user_bits["full_name"],
                org_name=user_bits["org_name"],
                email=user_bits["email"],
                user=user_bits.get("user"),
            ),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_card_json(request, tab: str):
    """Return Adaptive Card JSON for a tab (used by Teams tabs / integrations)."""
    user_bits = _request_user_bits(request)
    card = build_card(
        tab,
        full_name=user_bits.get("full_name", ""),
        org_name=user_bits.get("org_name", ""),
        user=user_bits.get("user"),
    )
    if card is None:
        return Response({"error": "unknown_tab"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"tab": tab, "card": card})


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_tab_content_json(request, tab: str):
    """Plain JSON content for a Learner tab (used by the tab's own HTML page
    via fetch — no Adaptive Card parsing, so buttons/links stay in-tab)."""
    user_bits = _request_user_bits(request)
    content = build_tab_content(
        tab,
        full_name=user_bits.get("full_name", ""),
        org_name=user_bits.get("org_name", ""),
        user=user_bits.get("user"),
    )
    if content is None:
        return Response({"error": "unknown_tab"}, status=status.HTTP_404_NOT_FOUND)
    return Response(content)


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_licence_download(request):
    """Downloadable image of the learner's licence card — an SVG (needs no
    image/PDF library) with the same real licence_number/issued/expires
    fields the Learner's Permit tab shows on screen."""
    from django.utils.html import escape

    user_bits = _request_user_bits(request)
    org_name = org_display_name(user_bits.get("org_name", ""))
    facts = _licence_facts(
        full_name=user_bits.get("full_name", ""),
        org_name=org_name,
        user=user_bits.get("user"),
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="380" viewBox="0 0 640 380">
  <rect width="640" height="380" rx="20" fill="#ffd335"/>
  <text x="32" y="48" font-family="Segoe UI, sans-serif" font-size="16" font-weight="700" fill="#201f36">AI DRIVING LICENSE</text>
  <text x="32" y="70" font-family="Segoe UI, sans-serif" font-size="12" fill="#5c4a00">ISSUED FOR {escape(org_name.upper())}</text>
  <text x="32" y="130" font-family="Segoe UI, sans-serif" font-size="34" font-weight="800" fill="#201f36">{escape(facts["name"])}</text>
  <text x="32" y="200" font-family="Segoe UI, sans-serif" font-size="11" fill="#5c4a00">CLASS</text>
  <text x="32" y="222" font-family="Segoe UI, sans-serif" font-size="16" font-weight="700" fill="#201f36">Learner's Permit</text>
  <text x="32" y="256" font-family="Segoe UI, sans-serif" font-size="11" fill="#5c4a00">EXPIRES</text>
  <text x="32" y="278" font-family="Segoe UI, sans-serif" font-size="16" font-weight="700" fill="#201f36">{escape(facts["expires"])}</text>
  <text x="330" y="200" font-family="Segoe UI, sans-serif" font-size="11" fill="#5c4a00">ISSUED</text>
  <text x="330" y="222" font-family="Segoe UI, sans-serif" font-size="16" font-weight="700" fill="#201f36">{escape(facts["issued"])}</text>
  <text x="330" y="256" font-family="Segoe UI, sans-serif" font-size="11" fill="#5c4a00">STATUS</text>
  <text x="330" y="278" font-family="Segoe UI, sans-serif" font-size="16" font-weight="700" fill="#201f36">{escape(facts["status"])}</text>
  <text x="32" y="340" font-family="Segoe UI, sans-serif" font-size="12" fill="#5c4a00">{escape(facts["licence_number"])}</text>
</svg>"""
    response = HttpResponse(svg, content_type="image/svg+xml")
    filename = f"{facts['licence_number'] or 'aidl-licence'}.svg"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def teams_traffic_light_rate(request):
    """Record a 👍/👎 vote on the Traffic Light Check card and return the new
    totals — a single global counter, same one the card's feedback line shows."""
    from .models import TrafficLightRating

    vote = (request.data.get("vote") or "").strip().lower()
    if vote not in ("like", "dislike"):
        return Response({"error": "invalid_vote"}, status=status.HTTP_400_BAD_REQUEST)

    row = TrafficLightRating.objects.first()
    if row is None:
        row = TrafficLightRating.objects.create()
    if vote == "like":
        row.likes += 1
        row.save(update_fields=["likes", "updated_at"])
    else:
        row.dislikes += 1
        row.save(update_fields=["dislikes", "updated_at"])
    return Response({"likes": row.likes, "dislikes": row.dislikes})


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_app_index(request):
    """Teams app metadata + Admin Center tab endpoints."""
    base = _teams_base_url(request)
    return Response(
        {
            "app": "AIDL Teams Admin Center",
            "org_display_name": org_display_name(),
            "admin_tabs": [
                {
                    "id": tab_id,
                    "name": label,
                    "tab_url": request.build_absolute_uri(f"/api/teams/tabs/{tab_id}/"),
                    "admin_json": request.build_absolute_uri(
                        f"/api/teams/admin/{tab_id}/"
                    ),
                }
                for tab_id, label, _icon in ADMIN_TABS
            ],
            "legacy_learner_tabs": [
                {
                    "id": tab_id,
                    "name": label,
                    "tab_url": request.build_absolute_uri(f"/api/teams/tabs/{tab_id}/"),
                    "card_url": request.build_absolute_uri(
                        f"/api/teams/cards/{tab_id}/"
                    ),
                }
                for tab_id, label, _icon in TEAMS_TABS
            ],
            "manifest_path": "/teams/manifest.json",
            "send_welcome_on_signup": bool(getattr(settings, "MS_SEND_WELCOME_CARD", True)),
            "teams_base_url": base,
            "note": (
                "Option C: Home tab shows Admin Center UI with in-page pills. "
                "Login/channel create flow unchanged."
            ),
        }
    )


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_install_channel_tabs(request):
    """
    Install Home / Learner's Permit / Highway Code / Traffic Light Check tabs
    on the aidl dashboard channel only (never General).
    """
    user = request.user
    team_id = getattr(user, "teams_team_id", "") or ""
    channel_id = getattr(user, "teams_channel_id", "") or ""
    channel_name = getattr(user, "teams_channel_name", "") or target_channel_name()

    if not team_id or not channel_id:
        return Response(
            {
                "error": "channel_not_configured",
                "message": "Log in via Teams first so aidl dashboard ids are saved.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not is_aidl_dashboard_channel(channel_name):
        return Response(
            {
                "error": "wrong_channel",
                "message": (
                    f"Tabs only install on '{target_channel_name()}' channel, "
                    f"not '{channel_name}'."
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    ms_token = (request.data.get("ms_access_token") or "").strip()
    if not ms_token:
        return Response(
            {"error": "ms_access_token_required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    result = ensure_aidl_channel_tabs(
        ms_token,
        team_id=team_id,
        channel_id=channel_id,
        channel_name=channel_name,
    )
    if not result or result.get("skipped"):
        return Response(
            {"error": "tab_install_skipped", "details": result},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not result.get("ok"):
        return Response(
            {"error": "tab_install_partial", "details": result},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response({"ok": True, "details": result})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_send_welcome(request):
    """
    Manually post the Home welcome Adaptive Card to the user's AIDL channel.
    Requires a valid AIDL JWT and stored team/channel ids from login.
    """
    user = request.user
    team_id = getattr(user, "teams_team_id", "") or ""
    channel_id = getattr(user, "teams_channel_id", "") or ""
    if not team_id or not channel_id:
        return Response(
            {
                "error": "channel_not_configured",
                "message": "Log in via Teams first so AIDL channel ids are saved.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    ms_token = (request.data.get("ms_access_token") or "").strip()
    if not ms_token:
        return Response(
            {
                "error": "ms_access_token_required",
                "message": "Pass Microsoft Graph access token in POST body.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    result = replace_welcome_card(
        ms_token,
        team_id=team_id,
        channel_id=channel_id,
        full_name=user.full_name,
        org_name=user.organization_name or org_display_name(),
        email=user.email,
        user=user,
    )
    if not result or not result.get("ok"):
        return Response(
            {
                "error": "send_failed",
                "message": "Could not post Admin Center card to channel Posts.",
                "details": result,
                "hint": (
                    "Ensure Azure delegated permission ChannelMessage.Send "
                    "has admin consent, then re-login."
                ),
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(
        {
            "ok": True,
            "message_id": result.get("message_id")
            or (result.get("message") or {}).get("id"),
            "tab": "home",
            "card_variant": result.get("card_variant"),
            "channel": {
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": getattr(user, "teams_channel_name", "") or "aidl dashboard",
            },
        }
    )


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_send_card(request, tab: str):
    """Post any tab Adaptive Card to the user's AIDL channel."""
    card = build_card(tab, full_name=request.user.full_name, org_name=org_display_name(), user=request.user)
    if card is None:
        return Response({"error": "unknown_tab"}, status=status.HTTP_404_NOT_FOUND)

    team_id = getattr(request.user, "teams_team_id", "") or ""
    channel_id = getattr(request.user, "teams_channel_id", "") or ""
    ms_token = (request.data.get("ms_access_token") or "").strip()
    if not team_id or not channel_id or not ms_token:
        return Response(
            {"error": "missing_channel_or_token"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    result = send_channel_adaptive_card(
        ms_token,
        team_id=team_id,
        channel_id=channel_id,
        card=card,
    )
    if not result or not result.get("ok"):
        return Response(
            {"error": "send_failed", "details": result},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(
        {
            "ok": True,
            "tab": tab,
            "message_id": result.get("message_id")
            or (result.get("message") or {}).get("id"),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_admin_export_csv(request):
    """Export coverage CSV for the signed-in user's organisation."""
    import csv
    from io import StringIO

    from django.http import HttpResponse

    from .org_service import coverage_csv_rows

    bits = _request_user_bits(request)
    rows = coverage_csv_rows(email=bits.get("email", ""), user=bits.get("user"))
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "full_name",
            "email",
            "role",
            "licence_issued",
            "aup_signed",
            "organization",
            "teams_channel",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="aidl-coverage.csv"'
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def teams_admin_session(request):
    """
    Mint a short-lived AIDL session token for an org admin who is already
    inside the Teams tab (the "Send Admin Invite" flow has no browser-based
    SPA login to hand it a token from). The Teams tab resolves the caller's
    email from the Teams SSO context client-side and posts it here.

    This does not grant admin — it only authenticates a request for someone
    the DB already has on record as role=admin (set the normal way, via a
    prior invite or org setup), the same requirement teams_admin_invite
    enforces. An unrecognised or non-admin email gets nothing.
    """
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response({"error": "email_required"}, status=status.HTTP_400_BAD_REQUEST)
    caller = AIDLUser.objects.filter(
        email__iexact=email, is_active=True, role=AIDLUser.Role.ADMIN
    ).first()
    if caller is None:
        return Response({"error": "not_admin"}, status=status.HTTP_403_FORBIDDEN)
    return Response({"ok": True, "access_token": create_access_token(caller)})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_invite(request):
    """Promote / add an admin by email within the caller's organisation."""
    from .admin_ops import AdminOpsError, promote_to_admin

    caller = request.user
    permissions = request.data.get("permissions") or {}
    try:
        target = promote_to_admin(caller, email=request.data.get("email") or "", permissions=permissions)
    except AdminOpsError as exc:
        code = {
            "email_required": status.HTTP_400_BAD_REQUEST,
            "no_organization": status.HTTP_400_BAD_REQUEST,
            "not_admin": status.HTTP_403_FORBIDDEN,
            "user_not_found": status.HTTP_404_NOT_FOUND,
            "admin_seats_full": status.HTTP_400_BAD_REQUEST,
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return Response({"error": exc.code, "message": exc.message}, status=code)

    return Response(
        {
            "ok": True,
            "admin": {
                "email": target.email,
                "full_name": target.full_name,
                "role": target.role,
                "permissions": {
                    "approve_apps": target.perm_approve_apps,
                    "access_cards": target.perm_access_cards,
                    "create_card": target.perm_create_card,
                },
            },
        }
    )


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_invite_user(request):
    """
    Invite a brand-new (same-tenant) user by email: add them to the caller's
    AIDL Team via Graph + send them a sign-in email. They land in the
    caller's organisation as a Learner the first time they sign in.
    """
    caller = request.user
    email = (request.data.get("email") or "").strip()
    full_name = (request.data.get("full_name") or "").strip()
    if not email:
        return Response({"error": "email_required"}, status=status.HTTP_400_BAD_REQUEST)

    result = send_user_invite(caller=caller, email=email, full_name=full_name)
    if not result.get("ok"):
        error = result.get("error") or "invite_failed"
        code = {
            "not_admin": status.HTTP_403_FORBIDDEN,
            "no_organization": status.HTTP_400_BAD_REQUEST,
            "user_already_exists": status.HTTP_409_CONFLICT,
            "org_not_found": status.HTTP_400_BAD_REQUEST,
            "email_required": status.HTTP_400_BAD_REQUEST,
        }.get(error, status.HTTP_400_BAD_REQUEST)
        return Response(result, status=code)
    return Response(result)


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_team_members(request):
    """
    List members already in the caller's AIDL Teams team, for the Admin
    Center's "pick someone already in this Teams team" picker on Add User /
    Add Admin. Lets an admin fetch a known member's name + email instead of
    typing them by hand, so the whole flow stays inside Teams with no
    external link. Returns [] (not an error) if Graph is unreachable or the
    caller has no usable token — the UI falls back to manual entry.
    """
    from .microsoft_auth import get_access_token_for_user, list_team_members

    caller = request.user
    access_token = get_access_token_for_user(caller)
    members = list_team_members(access_token, caller.teams_team_id) if access_token else []
    return Response({"ok": True, "members": members})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_sign_aup(request):
    """Mark Acceptable Use Policy signed for the current user."""
    from django.utils import timezone

    user = request.user
    user.aup_signed = True
    user.aup_signed_at = timezone.now()
    user.save(update_fields=["aup_signed", "aup_signed_at", "updated_at"])
    return Response({"ok": True, "aup_signed": True, "aup_signed_at": user.aup_signed_at})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_issue_licence(request):
    """Issue licence flag for a member in the same organisation."""
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response({"error": "email_required"}, status=status.HTTP_400_BAD_REQUEST)
    caller = request.user
    target = AIDLUser.objects.filter(
        email__iexact=email,
        organization_id=caller.organization_id,
        is_active=True,
    ).first()
    if target is None:
        return Response({"error": "user_not_found"}, status=status.HTTP_404_NOT_FOUND)

    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    target.licence_issued = True
    if not target.licence_number:
        target.licence_number = _generate_licence_number()
    if not target.licence_issued_at:
        target.licence_issued_at = now
        target.licence_expires_at = now + timedelta(days=365)
    target.save(
        update_fields=[
            "licence_issued",
            "licence_number",
            "licence_issued_at",
            "licence_expires_at",
            "updated_at",
        ]
    )
    return Response(
        {
            "ok": True,
            "email": target.email,
            "licence_issued": True,
            "licence_number": target.licence_number,
            "licence_issued_at": target.licence_issued_at,
            "licence_expires_at": target.licence_expires_at,
        }
    )


def _generate_licence_number() -> str:
    import secrets

    for _ in range(5):
        candidate = f"AIDL-L-{secrets.randbelow(10**8):08d}"
        if not AIDLUser.objects.filter(licence_number=candidate).exists():
            return candidate
    return f"AIDL-L-{secrets.randbelow(10**8):08d}"

"""Adaptive Cards for AIDL Admin Center — rendered inside Teams Posts (VaptFix-style)."""

from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings

from .org_service import build_admin_dashboard_from_db, build_admin_tab_payload
from .teams_admin import ADMIN_TABS
from .teams_cards import logo_url


def _teams_app_base_url() -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    return configured.rstrip("/") if configured else "https://aidl-backend.onrender.com/api/teams"


def _admin_home_tab_url(*, email: str = "", org_name: str = "") -> str:
    """The actual HTML page with the Home/Add Admin/Policy/Cards/AI Apps/IT
    Apps pill navigation — this is what "Refresh Admin Center" must open,
    not the raw JSON API index."""
    base = _teams_app_base_url()
    params = {k: v for k, v in {"email": email, "org_name": org_name}.items() if v}
    query = f"?{urlencode(params)}" if params else ""
    return f"{base}/tabs/home/{query}"


def _header(org_name: str) -> dict:
    """Brand row with AIDL PNG logo (Teams does not reliably load SVG)."""
    return {
        "type": "ColumnSet",
        "spacing": "None",
        "columns": [
            {
                "type": "Column",
                "width": "auto",
                "items": [
                    {
                        "type": "Image",
                        "url": logo_url(),
                        "size": "Medium",
                        "width": "48px",
                        "height": "48px",
                    }
                ],
                "verticalContentAlignment": "Center",
            },
            {
                "type": "Column",
                "width": "stretch",
                "spacing": "Small",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "AIDL",
                        "weight": "Bolder",
                        "size": "Medium",
                        "spacing": "None",
                    },
                    {
                        "type": "TextBlock",
                        "text": f"for {org_name}",
                        "isSubtle": True,
                        "size": "Small",
                        "spacing": "None",
                        "wrap": True,
                    },
                ],
                "verticalContentAlignment": "Center",
            },
        ],
    }


def _nav_actions(active_tab: str, *, email: str = "", org_id: str = "") -> list[dict]:
    """Pill-style nav — Action.Execute needs the AIDL bot (Phase 2)."""
    actions = []
    for tab_id, label, _icon in ADMIN_TABS:
        action = {
            "type": "Action.Execute",
            "title": label,
            "verb": "aidl.nav",
            "data": {
                "action": "nav",
                "tab": tab_id,
                "email": email,
                "organization_id": org_id,
            },
        }
        if tab_id == active_tab:
            action["style"] = "positive"
        actions.append(action)
    return actions


def _nav_text(active_tab: str) -> dict:
    """Graph-safe nav row (no bot required) — active tab highlighted in text."""
    parts = []
    for tab_id, label, _icon in ADMIN_TABS:
        if tab_id == active_tab:
            parts.append(f"**{label}**")
        else:
            parts.append(label)
    return {
        "type": "TextBlock",
        "text": " · ".join(parts),
        "wrap": True,
        "spacing": "Small",
    }


def _nav_container(
    active_tab: str,
    *,
    email: str = "",
    org_id: str = "",
    interactive: bool = False,
) -> dict:
    if interactive:
        return {
            "type": "ActionSet",
            "actions": _nav_actions(active_tab, email=email, org_id=org_id),
        }
    return _nav_text(active_tab)


def _stat_tile(label: str, value: str, sub: str, *, alert: bool = False) -> dict:
    return {
        "type": "Container",
        "style": "emphasis",
        "items": [
            {
                "type": "TextBlock",
                "text": label,
                "size": "Small",
                "weight": "Bolder",
                "isSubtle": True,
                "spacing": "None",
            },
            {
                "type": "TextBlock",
                "text": value,
                "size": "ExtraLarge",
                "weight": "Bolder",
                "color": "attention" if alert else "default",
                "spacing": "Small",
            },
            {
                "type": "TextBlock",
                "text": sub,
                "size": "Small",
                "isSubtle": True,
                "spacing": "None",
                "wrap": True,
            },
        ],
    }


def build_admin_adaptive_card(
    tab: str = "home",
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
    interactive: bool = False,
) -> dict:
    """
    Build Admin Center Adaptive Card (dynamic from DB).

    interactive=False (default for Graph channel posts): no Action.Execute —
    Graph user posts reject / ignore bot verbs, which can leave Posts empty.
    interactive=True: bot-driven in-place nav (Phase 2).
    """
    tab = (tab or "home").strip().lower()
    if tab == "home":
        return _home_card(
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
            interactive=interactive,
        )
    return _section_card(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
        interactive=interactive,
    )


def _home_card(
    *,
    full_name: str,
    org_name: str,
    email: str,
    user,
    interactive: bool = False,
) -> dict:
    dash = build_admin_dashboard_from_db(
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = dash.get("organization_id") or ""
    org = dash.get("org_name") or org_name or "AIDL"

    stats = dash.get("stats") or []
    stats_cols = []
    for s in stats[:3]:
        stats_cols.append(
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    _stat_tile(
                        s.get("label", ""),
                        s.get("value", "0"),
                        s.get("sub", ""),
                        alert=bool(s.get("alert")),
                    )
                ],
            }
        )

    gov = dash.get("governance") or []
    gov_row_1 = []
    gov_row_2 = []
    for idx, g in enumerate(gov[:4]):
        col = {
            "type": "Column",
            "width": "stretch",
            "items": [
                _stat_tile(
                    g.get("label", ""),
                    g.get("value", "0"),
                    g.get("sub", ""),
                )
            ],
        }
        if idx < 2:
            gov_row_1.append(col)
        else:
            gov_row_2.append(col)

    body = [
        _header(org),
        {
            "type": "TextBlock",
            "text": "Admin Center",
            "size": "Medium",
            "weight": "Bolder",
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": "Depot Overview",
            "isSubtle": True,
            "size": "Small",
            "spacing": "None",
        },
        _nav_container(
            "home",
            email=email,
            org_id=org_id,
            interactive=interactive,
        ),
        {
            "type": "Container",
            "spacing": "Medium",
            "items": [
                {
                    "type": "TextBlock",
                    "text": dash["welcome_title"],
                    "size": "Large",
                    "weight": "Bolder",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": dash["welcome_body"],
                    "wrap": True,
                    "spacing": "Small",
                },
            ],
        },
    ]

    if stats_cols:
        body.append(
            {
                "type": "ColumnSet",
                "spacing": "Medium",
                "columns": stats_cols,
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": "GOVERNANCE SNAPSHOT",
            "size": "Small",
            "weight": "Bolder",
            "isSubtle": True,
            "spacing": "Large",
        }
    )
    if gov_row_1:
        body.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_1})
    if gov_row_2:
        body.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_2})

    body.append(
        {
            "type": "TextBlock",
            "text": dash["progress_text"],
            "size": "Small",
            "isSubtle": True,
            "spacing": "Medium",
            "wrap": True,
        }
    )

    export_base = f"{_teams_app_base_url()}/admin/export/"
    export_url = f"{export_base}?{urlencode({'email': email})}" if email else export_base

    actions: list[dict] = [
        {
            "type": "Action.OpenUrl",
            "title": "Export Coverage CSV",
            "url": export_url,
        },
    ]
    if interactive:
        actions.append(
            {
                "type": "Action.Execute",
                "title": "Next: add another admin",
                "verb": "aidl.nav",
                "style": "positive",
                "data": {
                    "action": "nav",
                    "tab": "add-admin",
                    "email": email,
                    "organization_id": org_id,
                },
            }
        )
    else:
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "Open Admin Center",
                "url": _admin_home_tab_url(email=email, org_name=org),
                "style": "positive",
            }
        )

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": actions,
        "msteams": {"width": "Full"},
    }


def _section_card(
    tab: str,
    *,
    full_name: str,
    org_name: str,
    email: str,
    user,
    interactive: bool = False,
) -> dict:
    payload = build_admin_tab_payload(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = payload.get("organization_id") or ""
    org = payload.get("org_name") or org_name or "AIDL"
    items = payload.get("items") or []
    item_blocks = []
    for it in items[:12]:
        title = it.get("name") or it.get("email") or "Item"
        meta = " · ".join(
            str(x) for x in [it.get("email"), it.get("role"), it.get("status")] if x
        )
        desc = it.get("description") or ""
        block_items = [
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "wrap": True,
                "spacing": "None",
            }
        ]
        if meta:
            block_items.append(
                {
                    "type": "TextBlock",
                    "text": meta,
                    "size": "Small",
                    "isSubtle": True,
                    "wrap": True,
                    "spacing": "None",
                }
            )
        if desc:
            block_items.append(
                {
                    "type": "TextBlock",
                    "text": desc,
                    "size": "Small",
                    "wrap": True,
                    "spacing": "None",
                }
            )
        item_blocks.append(
            {
                "type": "Container",
                "style": "emphasis",
                "items": block_items,
                "spacing": "Small",
            }
        )
    if not item_blocks:
        item_blocks.append(
            {
                "type": "TextBlock",
                "text": "No records yet for this organisation.",
                "isSubtle": True,
                "wrap": True,
            }
        )

    actions = []
    if tab == "policy" and payload.get("policy_url"):
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "Read full policy",
                "url": payload["policy_url"],
                "style": "positive",
            }
        )
    if interactive and tab == "add-admin":
        actions.append(
            {
                "type": "Action.Execute",
                "title": "Refresh admins",
                "verb": "aidl.nav",
                "data": {
                    "action": "nav",
                    "tab": "add-admin",
                    "email": email,
                    "organization_id": org_id,
                },
            }
        )

    body = [
        _header(org),
        {
            "type": "TextBlock",
            "text": "Admin Center",
            "size": "Small",
            "weight": "Bolder",
            "spacing": "Medium",
        },
        _nav_container(
            tab,
            email=email,
            org_id=org_id,
            interactive=interactive,
        ),
        {
            "type": "TextBlock",
            "text": payload.get("heading") or payload.get("title") or tab,
            "size": "Large",
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": payload.get("body") or "",
            "wrap": True,
        },
        *item_blocks,
    ]
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "msteams": {"width": "Full"},
    }
    if actions:
        card["actions"] = actions
    return card

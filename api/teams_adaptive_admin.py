"""Adaptive Cards for AIDL Admin Center — rendered inside Teams Posts (VaptFix-style)."""

from __future__ import annotations

from .org_service import build_admin_dashboard_from_db, build_admin_tab_payload
from .teams_admin import ADMIN_TABS
from .teams_cards import logo_url


def _nav_actions(active_tab: str, *, email: str = "", org_id: str = "") -> list[dict]:
    """Pill-style nav buttons — Action.Execute updates the card in-place via bot."""
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


def _nav_container(active_tab: str, *, email: str = "", org_id: str = "") -> dict:
    return {
        "type": "ActionSet",
        "actions": _nav_actions(active_tab, email=email, org_id=org_id),
    }


def build_admin_adaptive_card(
    tab: str = "home",
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
) -> dict:
    """Build the Adaptive Card shown in aidl dashboard Posts (dynamic from DB)."""
    tab = (tab or "home").strip().lower()
    if tab == "home":
        return _home_card(
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
        )
    return _section_card(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )


def _home_card(*, full_name: str, org_name: str, email: str, user) -> dict:
    dash = build_admin_dashboard_from_db(
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = dash.get("organization_id") or ""
    stats_cols = []
    for s in dash.get("stats") or []:
        color = "attention" if s.get("alert") else "default"
        stats_cols.append(
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": s["label"],
                        "size": "Small",
                        "weight": "Bolder",
                        "isSubtle": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": s["value"],
                        "size": "ExtraLarge",
                        "weight": "Bolder",
                        "color": color,
                        "spacing": "None",
                    },
                    {
                        "type": "TextBlock",
                        "text": s["sub"],
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "None",
                    },
                ],
                "style": "emphasis",
            }
        )

    gov_cols = []
    for g in dash.get("governance") or []:
        gov_cols.append(
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": g["label"],
                        "size": "Small",
                        "weight": "Bolder",
                        "isSubtle": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": g["value"],
                        "size": "Large",
                        "weight": "Bolder",
                        "spacing": "None",
                    },
                    {
                        "type": "TextBlock",
                        "text": g["sub"],
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "None",
                    },
                ],
                "style": "emphasis",
            }
        )

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "Image",
                                "url": dash.get("logo_url") or logo_url(),
                                "size": "Small",
                                "style": "Person",
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": f"AIDL for {dash.get('org_name')}",
                                "isSubtle": True,
                                "size": "Small",
                            }
                        ],
                        "verticalContentAlignment": "Center",
                    },
                ],
            },
            {
                "type": "TextBlock",
                "text": "Admin Center",
                "size": "Small",
                "weight": "Bolder",
                "spacing": "Medium",
            },
            _nav_container("home", email=email, org_id=org_id),
            {
                "type": "TextBlock",
                "text": dash["welcome_title"],
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": dash["welcome_body"],
                "wrap": True,
            },
            {
                "type": "ColumnSet",
                "spacing": "Medium",
                "columns": stats_cols,
            },
            {
                "type": "TextBlock",
                "text": "GOVERNANCE SNAPSHOT",
                "size": "Small",
                "weight": "Bolder",
                "isSubtle": True,
                "spacing": "Medium",
            },
            {
                "type": "ColumnSet",
                "columns": gov_cols,
            },
            {
                "type": "TextBlock",
                "text": dash["progress_text"],
                "size": "Small",
                "isSubtle": True,
                "spacing": "Medium",
                "wrap": True,
            },
        ],
        "actions": [
            {
                "type": "Action.Execute",
                "title": "⬇ Export Coverage CSV",
                "verb": "aidl.export",
                "data": {
                    "action": "export",
                    "email": email,
                    "organization_id": org_id,
                },
            },
            {
                "type": "Action.Execute",
                "title": "Next: add another admin",
                "verb": "aidl.nav",
                "data": {
                    "action": "nav",
                    "tab": "add-admin",
                    "email": email,
                    "organization_id": org_id,
                },
            },
        ],
        "msteams": {"width": "Full"},
    }


def _section_card(
    tab: str,
    *,
    full_name: str,
    org_name: str,
    email: str,
    user,
) -> dict:
    payload = build_admin_tab_payload(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = payload.get("organization_id") or ""
    items = payload.get("items") or []
    item_blocks = []
    for it in items[:12]:
        title = it.get("name") or it.get("email") or "Item"
        meta = " · ".join(
            str(x) for x in [it.get("email"), it.get("role"), it.get("status")] if x
        )
        desc = it.get("description") or ""
        line = f"**{title}**"
        if meta:
            line += f"\n{meta}"
        if desc:
            line += f"\n_{desc}_"
        item_blocks.append(
            {
                "type": "TextBlock",
                "text": line,
                "wrap": True,
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
            }
        )
    if tab == "add-admin":
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
        {
            "type": "TextBlock",
            "text": f"AIDL for {payload.get('org_name')}",
            "isSubtle": True,
            "size": "Small",
        },
        {
            "type": "TextBlock",
            "text": "Admin Center",
            "size": "Small",
            "weight": "Bolder",
        },
        _nav_container(tab, email=email, org_id=org_id),
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
        {
            "type": "Container",
            "style": "emphasis",
            "items": item_blocks,
            "spacing": "Medium",
        },
    ]
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "msteams": {"width": "Full"},
    }
    if actions:
        card["actions"] = actions
    return card

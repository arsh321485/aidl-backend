"""Adaptive Cards for AIDL Admin Center — rendered inside Teams Posts (VaptFix-style)."""

from __future__ import annotations

from .org_service import build_admin_dashboard_from_db, build_admin_tab_payload
from .teams_admin import ADMIN_TABS
from .teams_cards import logo_url


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


def _section_body_blocks(
    tab: str,
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
) -> list[dict]:
    """Heading + body + item list + tab-specific extras for one section, with
    no header/nav — fills a toggle-visibility Container in the combined card
    (see _combined_card) so a pill click swaps this content in below the nav
    without a bot round-trip or a new browser tab."""
    payload = build_admin_tab_payload(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
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

    blocks = [
        {
            "type": "TextBlock",
            "text": payload.get("heading") or payload.get("title") or tab,
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": payload.get("body") or "",
            "wrap": True,
            "spacing": "Small",
        },
        *item_blocks,
    ]

    if tab == "policy" and payload.get("policy_url"):
        blocks.append(
            {
                "type": "ActionSet",
                "spacing": "Small",
                "actions": [
                    {
                        "type": "Action.OpenUrl",
                        "title": "Read full policy",
                        "url": payload["policy_url"],
                        "style": "positive",
                    }
                ],
            }
        )
    if tab == "add-admin":
        blocks.append(
            {
                "type": "TextBlock",
                "text": "Open the AIDL Admin Center tab in Teams to add a new admin by email.",
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        )

    return blocks


def _section_element_id(tab_id: str) -> str:
    return f"sec-{tab_id}"


def _nav_toggle_actions(active_tab: str) -> list[dict]:
    """
    Graph-safe clickable nav — each pill is an Action.ToggleVisibility that
    force-shows its own section Container and force-hides every other one, so
    clicking a pill swaps the card content in place (Home's card disappears
    the moment another tab is opened) instead of leaving Home visible
    underneath or popping a new browser tab/window.
    """
    all_ids = [_section_element_id(t) for t, _l, _i in ADMIN_TABS]
    actions = []
    for tab_id, label, icon in ADMIN_TABS:
        target_id = _section_element_id(tab_id)
        action = {
            "type": "Action.ToggleVisibility",
            "title": f"{icon} {label}" if icon else label,
            "targetElements": [
                {"elementId": eid, "isVisible": eid == target_id} for eid in all_ids
            ],
        }
        if tab_id == active_tab:
            action["style"] = "positive"
        actions.append(action)
    return actions


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
    return {
        "type": "ActionSet",
        "actions": _nav_toggle_actions(active_tab),
    }


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

    interactive=False (default for Graph channel posts): a single combined
    card with every tab's section pre-built and Action.ToggleVisibility nav —
    no Action.Execute, since Graph user posts reject / ignore bot verbs
    (which can leave Posts empty), but still a real tab switch (old section
    hides the moment another one is opened) with no bot and no browser tab.
    interactive=True: bot-driven in-place nav (Phase 2) — one section per
    message, replaced wholesale by the bot on each click.
    """
    tab = (tab or "home").strip().lower()
    if not interactive:
        return _combined_card(
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
            active_tab=tab,
        )
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


def _home_body_items(dash: dict) -> list[dict]:
    """Depot Overview + welcome + stats + governance + progress — Home's own
    section content, with no header/nav (those are shared, above the tabs)."""
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

    items: list[dict] = [
        {
            "type": "TextBlock",
            "text": "Depot Overview",
            "isSubtle": True,
            "size": "Small",
            "spacing": "None",
        },
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
        items.append({"type": "ColumnSet", "spacing": "Medium", "columns": stats_cols})

    items.append(
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
        items.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_1})
    if gov_row_2:
        items.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_2})

    items.append(
        {
            "type": "TextBlock",
            "text": dash["progress_text"],
            "size": "Small",
            "isSubtle": True,
            "spacing": "Medium",
            "wrap": True,
        }
    )
    return items


def _combined_card(
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
    active_tab: str = "home",
) -> dict:
    """
    One AdaptiveCard holding every tab's section as a Container keyed
    "sec-<tab>", with only the active one visible at a time. Nav pills use
    Action.ToggleVisibility to force-show the clicked section and
    force-hide every other one, so switching tabs behaves like a real tab
    strip — Home's card actually disappears once another tab is opened.
    """
    active_tab = (active_tab or "home").strip().lower()
    dash = build_admin_dashboard_from_db(
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = dash.get("organization_id") or ""
    org = dash.get("org_name") or org_name or "AIDL"

    sections = [
        {
            "type": "Container",
            "id": _section_element_id("home"),
            "isVisible": active_tab == "home",
            "items": _home_body_items(dash),
        }
    ]
    for tab_id, _label, _icon in ADMIN_TABS:
        if tab_id == "home":
            continue
        sections.append(
            {
                "type": "Container",
                "id": _section_element_id(tab_id),
                "isVisible": active_tab == tab_id,
                "items": _section_body_blocks(
                    tab_id,
                    full_name=full_name,
                    org_name=org_name,
                    email=email,
                    user=user,
                ),
            }
        )

    body = [
        _header(org),
        {
            "type": "TextBlock",
            "text": "Admin Center",
            "size": "Medium",
            "weight": "Bolder",
            "spacing": "Medium",
        },
        _nav_container(active_tab, email=email, org_id=org_id, interactive=False),
        *sections,
    ]

    export_url = (
        "https://aidl-backend.onrender.com/api/teams/admin/export/"
        f"?email={email}"
        if email
        else "https://aidl-backend.onrender.com/api/teams/admin/export/"
    )

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Export Coverage CSV",
                "url": export_url,
            },
            {
                "type": "Action.OpenUrl",
                "title": "Refresh Admin Center",
                "url": "https://aidl-backend.onrender.com/api/teams/",
                "style": "positive",
            },
        ],
        "msteams": {"width": "Full"},
    }


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

    export_url = (
        "https://aidl-backend.onrender.com/api/teams/admin/export/"
        f"?email={email}"
        if email
        else "https://aidl-backend.onrender.com/api/teams/admin/export/"
    )

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
                "title": "Refresh Admin Center",
                "url": "https://aidl-backend.onrender.com/api/teams/",
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

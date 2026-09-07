"""Adaptive Card JSON builders for AIDL Microsoft Teams tabs."""

from django.conf import settings

TEAMS_TABS = (
    ("home", "Home", "🏠"),
    ("learners-permit", "Learner's Permit", "🪪"),
    ("highway-code", "Highway Code", "📖"),
    ("traffic-light-check", "Traffic Light Check", "🚦"),
)

DEFAULT_POLICY_ENTITIES = (
    "SpinifexIT Global Pty Ltd",
    "SpinifexIT North America Inc.",
    "SpinifexIT Solutions UK Limited",
    "SpinifexIT Philippines Inc.",
    "SpinifexIT Singapore Pte. Ltd.",
    "SpinifexIT Deutschland GmbH",
)


def first_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else "there"


def org_display_name(org_name: str = "") -> str:
    value = (org_name or "").strip()
    if value:
        return value
    return (
        getattr(settings, "AIDL_ORG_DISPLAY_NAME", None) or "Northwind Logistics"
    ).strip() or "Northwind Logistics"


def policy_entities() -> tuple[str, ...]:
    configured = getattr(settings, "AIDL_POLICY_ENTITIES", None)
    if configured:
        return tuple(str(item).strip() for item in configured if str(item).strip())
    return DEFAULT_POLICY_ENTITIES


def policy_url() -> str:
    return (
        getattr(settings, "AIDL_POLICY_URL", None)
        or "https://www.spinifexit.com/acceptable-use-policy"
    ).strip()


def logo_url() -> str:
    # Prefer PNG — Teams Adaptive Cards often fail to load SVG images.
    return (
        getattr(settings, "AIDL_LOGO_URL", None)
        or "https://aidl-backend.onrender.com/static/aidl/logo.png"
    ).strip()


def _header_block(org_name: str) -> dict:
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


def _policy_checklist_block() -> dict:
    items = []
    for entity in policy_entities():
        items.append(
            {
                "type": "ColumnSet",
                "spacing": "Small",
                "columns": [
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "☐",
                                "size": "Default",
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": entity,
                                "wrap": True,
                            }
                        ],
                    },
                ],
            }
        )

    return {
        "type": "Container",
        "style": "emphasis",
        "bleed": True,
        "backgroundImage": {
            "fillMode": "Cover",
            "horizontalAlignment": "Center",
            "verticalAlignment": "Center",
        },
        "items": [
            {
                "type": "TextBlock",
                "text": "Acceptable Use of Technology Policy — the short version",
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "APPLIES TO",
                "size": "Small",
                "weight": "Bolder",
                "isSubtle": True,
                "spacing": "Medium",
            },
            *items,
        ],
        "spacing": "Medium",
    }


def build_home_card(*, full_name: str = "", org_name: str = "") -> dict:
    """Welcome card shown on Home tab and after Teams signup."""
    org = org_display_name(org_name)
    name = first_name(full_name)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _header_block(org),
            {
                "type": "TextBlock",
                "text": f"Welcome to AIDL, {name}! 👋",
                "size": "ExtraLarge",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": (
                    f"You've been added to **{org}'s** AI Driving Licence programme. "
                    "Before we issue your permit, take a second to agree to our "
                    "Acceptable Use Policy."
                ),
                "wrap": True,
                "spacing": "Small",
            },
            _policy_checklist_block(),
            {
                "type": "TextBlock",
                "text": f"Sent automatically on signup · {org}",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Medium",
                "wrap": True,
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Read full policy",
                "url": policy_url(),
                "style": "positive",
            }
        ],
        "msteams": {
            "width": "Full",
        },
    }


def build_learners_permit_card(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    name = first_name(full_name)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _header_block(org),
            {
                "type": "TextBlock",
                "text": f"Learner's Permit — {name}",
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": (
                    "Complete the Acceptable Use Policy on **Home**, then start your "
                    "AIDL modules here. Your digital permit will appear once you pass "
                    "the required checks."
                ),
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Status", "value": "Not issued yet"},
                    {"title": "Organisation", "value": org},
                ],
                "spacing": "Medium",
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Start learning",
                "url": getattr(settings, "FRONTEND_URL", ""),
                "style": "positive",
            }
        ],
        "msteams": {"width": "Full"},
    }


def build_highway_code_card(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _header_block(org),
            {
                "type": "TextBlock",
                "text": "Highway Code",
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": (
                    "Review core AI safety principles, responsible use guidelines, "
                    "and organisation-specific rules before taking your licence quiz."
                ),
                "wrap": True,
            },
            {
                "type": "Container",
                "style": "emphasis",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "Coming up",
                        "weight": "Bolder",
                    },
                    {
                        "type": "TextBlock",
                        "text": "• Responsible AI basics\n• Data handling\n• Prompt safety",
                        "wrap": True,
                    },
                ],
                "spacing": "Medium",
            },
        ],
        "msteams": {"width": "Full"},
    }


def build_traffic_light_check_card(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    name = first_name(full_name)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _header_block(org),
            {
                "type": "TextBlock",
                "text": "Traffic Light Check",
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": (
                    f"Hi {name}, this quick readiness check shows whether you're "
                    "green to proceed, amber for review, or red for mandatory training."
                ),
                "wrap": True,
            },
            {
                "type": "ColumnSet",
                "spacing": "Medium",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "🟢 Ready",
                                "horizontalAlignment": "Center",
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "🟡 Review",
                                "horizontalAlignment": "Center",
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "🔴 Training",
                                "horizontalAlignment": "Center",
                            }
                        ],
                    },
                ],
            },
            {
                "type": "TextBlock",
                "text": "Your check will unlock after policy acceptance.",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Medium",
            },
        ],
        "msteams": {"width": "Full"},
    }


CARD_BUILDERS = {
    "home": build_home_card,
    "learners-permit": build_learners_permit_card,
    "highway-code": build_highway_code_card,
    "traffic-light-check": build_traffic_light_check_card,
}


def build_card(tab: str, *, full_name: str = "", org_name: str = "") -> dict | None:
    builder = CARD_BUILDERS.get(tab)
    if not builder:
        return None
    return builder(full_name=full_name, org_name=org_name)

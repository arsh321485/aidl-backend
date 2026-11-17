"""Admin Center home card. Tab buttons and dialogs are a later step."""

from __future__ import annotations

from api.slack_cards import build_admin_cards_context


def admin_home_message(user) -> tuple[dict, str]:
    """Google Chat card plus the plain-text notification, from the shared admin context."""
    data = build_admin_cards_context(user)
    plan = data.get("plan") or {}
    text = f"AIDL Admin Center for {data['org_name']}"
    card = {
        "header": {
            "title": "AIDL Admin Center",
            "subtitle": data["org_name"],
        },
        "sections": [
            {
                "header": "Home",
                "widgets": [
                    {"textParagraph": {"text": f"Signed in as <b>{data['admin_name']}</b>"}},
                    {"decoratedText": {"topLabel": "Plan", "text": str(plan.get("label") or "Trial")}},
                    {
                        "decoratedText": {
                            "topLabel": "People",
                            "text": f"{data['enrolled']} enrolled · {data['licences_issued']} licensed",
                        }
                    },
                    {
                        "decoratedText": {
                            "topLabel": "Apps",
                            "text": f"{data['approved_apps']} approved of {data['total_apps']}",
                        }
                    },
                ],
            }
        ],
    }
    return card, text

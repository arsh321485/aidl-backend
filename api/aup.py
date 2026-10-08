"""Generated Acceptable Use Policy (AUP) — platform-neutral.

The organization never uploads its own policy. AIDL generates each user's
AUP from:
  1. the 8 policy answers given at organization signup (org_policy.py), and
  2. the organization's AI / IT app registry (Approved / Prohibited, with the
     class of data each app may touch).

The result is a plain dict that Slack (slack_blocks.aup_blocks) — and later
Teams — render as a card. Its `version` changes whenever the answers or the
app list change, so users are asked to accept the new version.
"""

from __future__ import annotations

import hashlib
import json

from django.utils import timezone

from .models import Organization, RegisteredApp
from .org_policy import get_answers, policy_effects

# Data the AUP always forbids in AI prompts (AIDL baseline).
BASE_RED_LIST = (
    "Passwords, PINs and verification codes",
    "Bank / card numbers and national ID numbers",
    "Home addresses and other people's personal data",
)

_CONFIDENTIAL_RULE = {
    "Never": "Never put confidential or customer data into any AI tool.",
    "Only in approved enterprise tools": "Confidential data only goes into apps marked 🔴 *Internal + Confidential* below.",
    "Yes, if the data is anonymised": "Anonymise confidential and customer data before it goes into any AI tool.",
    "No rule in place": "There's no company rule yet — treat confidential data as off-limits for AI.",
}
_REVIEW_RULE = {
    "Always": "A person must review every AI output before it's shared outside the company.",
    "Only for high-risk content": "Get a second person to review high-risk AI output (legal, financial, customer-facing).",
    "No, it is optional": "Review is optional — but you're responsible for anything you send.",
}
_DISCLOSURE_RULE = {
    "Yes, always": "Always say when content was created with AI.",
    "Only for client-facing content": "Say when client-facing content was created with AI.",
}
_TOOLS_RULE = {
    "Only company-approved tools": "Use only the apps listed under *You can use*.",
    "Any tool, with manager approval": "Any app not listed below needs your manager's approval first.",
    "Any public AI tool": "Public AI tools are fine — but only for *public* data.",
    "Not decided yet": "Stick to the apps listed under *You can use* until your company decides.",
}

_DATA_LABEL = dict(RegisteredApp.DataAllowed.choices)
_DATA_DOT = {"Public only": "🟢", "Internal": "🟡", "Internal + Confidential": "🔴", "None": "⚫"}


def _apps(org: Organization) -> tuple[list[dict], list[dict]]:
    """(AI apps, IT apps) after the policy answers are applied — the same
    lists the Admin Center's AI Apps / IT Apps tabs show."""
    from .org_service import compute_org_metrics
    from .slack_cards import _apply_app_effects, _registry_rows

    metrics = compute_org_metrics(org)
    fx = policy_effects(get_answers(org))
    return _apply_app_effects(_registry_rows(metrics["ai_app_list"]), fx), _registry_rows(metrics["it_app_list"])


def _entry(app: dict, kind: str) -> dict:
    return {
        "name": app["name"],
        "kind": kind,
        "category": app.get("category", ""),
        "data": app["data_allowed"],
        "dot": _DATA_DOT.get(app["data_allowed"], "🟡"),
    }


def generate_aup(org: Organization) -> dict:
    answers = get_answers(org)
    fx = policy_effects(answers)
    ai, it = _apps(org)

    allowed = [_entry(a, "ai") for a in ai if a["status"] == "Approved"] + \
              [_entry(a, "it") for a in it if a["status"] == "Approved"]
    prohibited = [_entry(a, "ai") for a in ai if a["status"] != "Approved"] + \
                 [_entry(a, "it") for a in it if a["status"] != "Approved"]

    red_list = list(BASE_RED_LIST)
    if fx["red_includes_confidential"]:
        red_list.insert(0, "Any confidential or customer data")

    rules = []
    for key, table in (("approved_tools", _TOOLS_RULE), ("confidential_data", _CONFIDENTIAL_RULE),
                       ("human_review", _REVIEW_RULE), ("disclosure", _DISCLOSURE_RULE)):
        if answers.get(key) in table:
            rules.append(table[answers[key]])
    if fx["regulation_law"]:
        rules.append(f"Personal data is handled under *{fx['regulation_law']}*.")
    if fx["report_to"]:
        rules.append(f"If something goes wrong with AI, report it to {fx['report_to']}.")
    if fx["training_note"] and fx["training_note"] != "No refresher reminders":
        rules.append(f"Training: {fx['training_note'].lower()}.")

    source = json.dumps({"answers": answers, "allowed": allowed, "prohibited": prohibited}, sort_keys=True)
    return {
        "org_name": org.name,
        "version": "AUP-" + hashlib.sha256(source.encode()).hexdigest()[:6].upper(),
        "generated": timezone.localdate().strftime("%d %b %Y"),
        "status": fx["aup_status"],  # live / draft / not_available
        "has_answers": bool(answers),
        "allowed_ai": [a for a in allowed if a["kind"] == "ai"],
        "allowed_it": [a for a in allowed if a["kind"] == "it"],
        "prohibited": prohibited,
        "red_list": red_list,
        "rules": rules,
    }

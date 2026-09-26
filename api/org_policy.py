"""The 8 organization policy questions (AIDL Slack guide 4.3) and how the
answers change the Admin and User cards (guide 7.3).

The question ids and option texts must match the frontend's
src/lib/orgPolicyQuestions.ts — answers are stored as the chosen option text.
"""

from __future__ import annotations

import json

from .models import Organization

POLICY_QUESTIONS: dict[str, tuple[str, ...]] = {
    "ai_policy": ("Yes, published and enforced", "Yes, but still a draft", "No, we are planning one", "No, not yet"),
    "approved_tools": ("Only company-approved tools", "Any tool, with manager approval", "Any public AI tool", "Not decided yet"),
    "confidential_data": ("Never", "Only in approved enterprise tools", "Yes, if the data is anonymised", "No rule in place"),
    "human_review": ("Always", "Only for high-risk content", "No, it is optional"),
    "disclosure": ("Yes, always", "Only for client-facing content", "No"),
    "regulation": ("GDPR (EU / UK)", "DPDP Act (India)", "HIPAA / CCPA (US)", "Other / Not sure"),
    "incident_reporting": ("Dedicated compliance / security channel", "Directly to their manager", "Through HR", "No process defined"),
    "training_frequency": ("Only when they join", "Every year", "Every quarter", "Not required"),
}


class PolicyAnswersError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("invalid policy answers")
        self.errors = errors


def clean_answers(raw) -> dict[str, str]:
    """All 8 questions answered with one of their options, else raises."""
    if not isinstance(raw, dict):
        raise PolicyAnswersError({"answers": "Send an object of question id → chosen option."})
    errors = {}
    answers = {}
    for qid, options in POLICY_QUESTIONS.items():
        value = raw.get(qid)
        if value not in options:
            errors[qid] = "Answer this question with one of its options."
        else:
            answers[qid] = value
    if errors:
        raise PolicyAnswersError(errors)
    return answers


def get_answers(org: Organization | None) -> dict[str, str]:
    if org is None or not org.policy_answers:
        return {}
    try:
        return clean_answers(json.loads(org.policy_answers))
    except (ValueError, PolicyAnswersError):
        return {}


def save_answers(org: Organization, answers: dict[str, str]) -> None:
    org.policy_answers = json.dumps(clean_answers(answers))
    org.save(update_fields=["policy_answers", "updated_at"])


def policy_completed(org: Organization | None) -> bool:
    return bool(get_answers(org))


# ---------- guide 7.3: answers → card data ----------

_REGULATION_LAW = {
    "GDPR (EU / UK)": "GDPR",
    "DPDP Act (India)": "the DPDP Act",
    "HIPAA / CCPA (US)": "HIPAA / CCPA",
}
_REPORT_TO = {
    "Dedicated compliance / security channel": "your compliance / security channel",
    "Directly to their manager": "your manager",
    "Through HR": "HR",
}


def policy_effects(answers: dict[str, str]) -> dict:
    """What the cards change, derived from the answers. Empty answers → the
    defaults (the cards look exactly like the guide's prototype)."""
    a = answers or {}
    ai_policy = a.get("ai_policy", "")
    training = a.get("training_frequency", "")

    recommended = []
    if a.get("confidential_data") == "Never":
        recommended.append("c7")  # Customer Data in Prompts
    if a.get("human_review") == "Always":
        recommended.append("c8")  # The Verification Pass

    if training == "Every quarter":
        training_note = "Refresher reminder every 3 months"
    elif training == "Every year":
        training_note = "License valid 12 months · yearly refresher"
    elif training in ("Only when they join", "Not required"):
        training_note = "No refresher reminders"
    else:
        training_note = ""

    return {
        "has_answers": bool(a),
        # Acceptable Use reference card + AUP Unsigned stat
        "aup_status": (
            "not_available" if ai_policy in ("No, we are planning one", "No, not yet")
            else "draft" if ai_policy == "Yes, but still a draft"
            else "live"
        ),
        "apps_mode": {
            "Only company-approved tools": "company_only",
            "Any public AI tool": "any_public",
        }.get(a.get("approved_tools", ""), ""),
        "enterprise_only": a.get("confidential_data") == "Only in approved enterprise tools",
        "red_includes_confidential": a.get("confidential_data") == "Never",
        "recommended_cards": recommended,
        # With answers, the gate lists unreviewed output only when review is mandatory.
        "gate_blocks_unreviewed": (not a) or a.get("human_review") == "Always",
        "disclose_ai": a.get("disclosure") == "Yes, always",
        "regulation_law": _REGULATION_LAW.get(a.get("regulation", ""), ""),
        "report_to": _REPORT_TO.get(a.get("incident_reporting", ""), "your AIDL admin" if a else ""),
        "training_note": training_note,
    }

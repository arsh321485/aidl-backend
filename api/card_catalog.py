"""
Fixed "Send Cards" catalog — ported from teams-admin-cards.html (#card-send).

This is reference-card metadata (title, pricing, description, guidance body),
not organisation data, so it lives as a Python constant rather than in Mongo.
Per-organisation state (sent / scheduled / requested) is tracked separately
in the CardDelivery model — see api/card_service.py.
"""

from __future__ import annotations

QUOTA_MAX = 10

CARD_CATALOG = {
    "aup": {
        "icon": "📄",
        "kicker": "Policy · Signature Required",
        "title": "Acceptable Use v3.1",
        "price": "$29",
        "rating": 4.1,
        "votes": 39,
        "desc": "Your live AUP with the summary your team actually reads.",
        "body": (
            "Your live AUP (northwind-aup-v3.1.pdf, effective 01 Aug 2026) with the "
            "plain-language summary your team actually reads. Signing this is required "
            "before the Data Ethics Gate unlocks a licence upgrade."
        ),
    },
    "traffic": {
        "icon": "🚦",
        "kicker": "Decision Aid",
        "title": "Traffic Light Check",
        "price": "$49",
        "rating": 4.8,
        "votes": 52,
        "desc": "Green, amber or red before you hand a task to AI. The three-second gut check.",
        "body": (
            "Green, amber or red before you hand a task to AI — the three-second gut "
            "check every team member runs before pasting anything into a tool. Green is "
            "public, non-personal data. Amber needs a placeholder or a strip-down first. "
            "Red never goes in, no exceptions."
        ),
        "has_lights": True,
    },
    "apps": {
        "icon": "🧩",
        "kicker": "Registry · 12 Entries",
        "title": "Approved Apps Registry",
        "price": "$59",
        "rating": 4.4,
        "votes": 27,
        "desc": "Which AI and IT tools are approved or prohibited, and for what data.",
        "body": (
            "The full list of AI and IT tools your org has marked Approved or "
            "Prohibited, and what class of data each one is allowed to touch. Pulled "
            "live from the Approved Apps tab."
        ),
    },
    "ethics": {
        "icon": "⚖️",
        "kicker": "Gate Briefing",
        "title": "Data Ethics Gate",
        "price": "$39",
        "rating": 4.6,
        "votes": 31,
        "desc": "What blocks a licence: unsigned policy, unlogged agents, unreviewed output.",
        "body": (
            "What blocks a licence upgrade: an unsigned Acceptable Use Policy, an "
            "unlogged AI agent run, or AI output that went out without a human review. "
            "Clearing all three re-opens the gate."
        ),
    },
    "museum": {
        "icon": "🏛️",
        "kicker": "Worked Examples",
        "title": "Mistake Museum",
        "price": "$79",
        "rating": 4.9,
        "votes": 44,
        "desc": "Real failures from the team, de-identified, with what should have happened.",
        "body": (
            "Real, de-identified failures from across the fleet — what went wrong, and "
            "what should have happened instead. Updated as new incidents are reviewed "
            "and cleared for sharing."
        ),
    },
    "classification": {
        "icon": "📋",
        "kicker": "Reference Card",
        "title": "Data Classification Cheat Sheet",
        "price": "$25",
        "rating": 4.5,
        "votes": 19,
        "desc": "Four data classes, four different answers about what you may submit.",
        "body": (
            "Four data classes, four different answers about what you may submit. "
            "Public: anything goes. Internal: approved tools only. Confidential: "
            "approved enterprise tools, no training, logged. Restricted: not in a "
            "prompt at all, in any tool, for any reason.\n\n"
            "Do this: classify the data before you classify the task. Keep the card visible."
        ),
    },
    "redaction": {
        "icon": "✅",
        "kicker": "Checklist",
        "title": "Redaction Before Submission",
        "price": "$35",
        "rating": 4.3,
        "votes": 15,
        "desc": "Before a document goes into a chat window, strip what the model doesn't need.",
        "body": (
            "Before a document goes into a chat window, strip what the model doesn't "
            "need. Names, account numbers, addresses, internal system identifiers, "
            "pricing, and anything in the headers, footers and comments you forgot "
            "were there. The tracked changes usually contain more than the document.\n\n"
            "Do this: save a redacted copy and upload that. Never the original."
        ),
    },
    "customerdata": {
        "icon": "🚫",
        "kicker": "Gate Briefing",
        "title": "Customer Data in Prompts",
        "price": "$69",
        "rating": 4.7,
        "votes": 22,
        "desc": "Customer data in a prompt is where the conversation stops.",
        "body": (
            "Customer data in a prompt is where the conversation stops. It doesn't "
            "matter that the tool is approved, the task is small, or you'll delete the "
            "chat afterwards. Identifiable customer information does not go into a "
            "general-purpose AI tool without a documented, approved pathway.\n\n"
            "Do this: if you need to work with customer data, request the approved "
            "route. Don't improvise one."
        ),
    },
    "verification": {
        "icon": "✅",
        "kicker": "Checklist",
        "title": "The Verification Pass",
        "price": "$45",
        "rating": 4.4,
        "votes": 17,
        "desc": "Three checks before AI output leaves your desk. Every time.",
        "body": (
            "Three checks before AI output leaves your desk. Every time. One: is every "
            "factual claim and number verified against a source you opened? Two: is "
            "anything here confidential that shouldn't be? Three: would you defend this "
            "if challenged in detail?\n\n"
            "Do this: run all three. If you can't do the first, you can't send it."
        ),
    },
    "reporting": {
        "icon": "🆘",
        "kicker": "Template",
        "title": "Reporting an AI Mistake",
        "price": "$19",
        "rating": 4.2,
        "votes": 11,
        "desc": "When something goes wrong, the first hour matters more than the blame.",
        "body": (
            "When something goes wrong, the first hour matters more than the blame. "
            "Tell us what you asked, what came back, what you did with it, and who "
            "might be affected. We're looking for the pattern, not the culprit. Fast "
            "reporting has never cost anyone here.\n\n"
            "Do this: use the report template. Send it before you've worked out the "
            "whole story."
        ),
    },
}

CARD_ORDER = [
    "aup",
    "traffic",
    "apps",
    "ethics",
    "museum",
    "classification",
    "redaction",
    "customerdata",
    "verification",
    "reporting",
]

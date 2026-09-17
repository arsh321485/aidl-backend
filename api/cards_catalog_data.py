"""Static content for the 10 reference cards in the Admin Center Cards
catalogue — same for every organisation (like Highway Code / Traffic Light
Check), so it lives in code, not the database. Only per-org request/send
state (CardRequest) is dynamic."""

from __future__ import annotations

CARD_CATALOG = (
    {
        "id": "c0",
        "icon": "📄",
        "title": "Acceptable Use v3.1",
        "category": "POLICY · SIGNATURE REQUIRED",
        "desc": "Your live AUP with the summary your team actually reads.",
        "body": (
            "This card mirrors your organisation's current Acceptable Use Policy: "
            "what data can go into an AI tool, what can't, and what happens if "
            "someone gets it wrong. Send it into any channel as a quick refresher."
        ),
        "price": "",
        "rating": "4.1 (39)",
    },
    {
        "id": "c1",
        "icon": "🚦",
        "title": "Traffic Light Check",
        "category": "DECISION AID",
        "desc": "Green, amber or red before you hand a task to AI. The three-second gut check.",
        "body": (
            "Green: public, harmless, go ahead. Amber: check before you paste it in. "
            "Red: customer data, credentials, anything regulated — never goes into a prompt. "
            "A one-glance card for anyone unsure before they hit send."
        ),
        "price": "$49 · one-time",
        "rating": "4.8 (52)",
    },
    {
        "id": "c2",
        "icon": "🗂️",
        "title": "Approved Apps Registry",
        "category": "REGISTRY · 12 ENTRIES",
        "desc": "Which AI and IT tools are approved or prohibited, and for what data.",
        "body": (
            "A living list of every AI and IT tool your organisation has reviewed, "
            "what data class each one is cleared for, and who to ask if a tool "
            "you need isn't on the list yet."
        ),
        "price": "$59 · one-time",
        "rating": "4.4 (27)",
    },
    {
        "id": "c3",
        "icon": "⚖️",
        "title": "Data Ethics Gate",
        "category": "GATE BRIEFING",
        "desc": "What blocks a licence: unsigned policy, unlogged agents, unreviewed output.",
        "body": (
            "Three things stand between a new hire and a working AI licence: a signed "
            "policy, a logged use case, and a first piece of output someone else has "
            "checked. This card explains why each gate exists."
        ),
        "price": "$39 · one-time",
        "rating": "4.6 (31)",
    },
    {
        "id": "c4",
        "icon": "🏛️",
        "title": "Mistake Museum",
        "category": "WORKED EXAMPLES",
        "desc": "Real failures from the team, de-identified, with what should have happened.",
        "body": (
            "Every mistake on this card actually happened somewhere on the team, "
            "stripped of names. Next to each one: what the right call would have "
            "looked like, in plain language."
        ),
        "price": "$79 · one-time",
        "rating": "4.9 (44)",
    },
    {
        "id": "c5",
        "icon": "📋",
        "title": "Data Classification Cheat Sheet",
        "category": "REFERENCE CARD",
        "desc": "Four data classes, four different answers about what you may submit.",
        "body": (
            "Public, Internal, Confidential, Restricted — a one-page reminder of what "
            "belongs in each bucket and which ones are ever allowed near an AI prompt."
        ),
        "price": "$25 · one-time",
        "rating": "4.5 (19)",
    },
    {
        "id": "c6",
        "icon": "✅",
        "title": "Redaction Before Submission",
        "category": "CHECKLIST",
        "desc": "Before a document goes into a chat window, strip what the model doesn't need.",
        "body": (
            "Names, ids, account numbers, anything the model doesn't actually need to "
            "do the task — this checklist walks through what to strip before a document "
            "goes into any AI tool."
        ),
        "price": "$35 · one-time",
        "rating": "4.3 (15)",
    },
    {
        "id": "c7",
        "icon": "💬",
        "title": "Customer Data in Prompts",
        "category": "GATE BRIEFING",
        "desc": "Customer data in a prompt is where the conversation stops.",
        "body": (
            "If a prompt would contain a real customer's name, contact details, or "
            "account history, this card explains why that's a hard stop — and what "
            "to do instead (anonymise, summarise, or ask first)."
        ),
        "price": "$69 · one-time",
        "rating": "4.7 (22)",
    },
    {
        "id": "c8",
        "icon": "✅",
        "title": "The Verification Pass",
        "category": "CHECKLIST",
        "desc": "Three checks before AI output leaves your desk. Every time.",
        "body": (
            "Is it factually right? Did it miss anything important? Would you put "
            "your name on it? Three questions worth thirty seconds before AI output "
            "goes anywhere outside your own screen."
        ),
        "price": "$45 · one-time",
        "rating": "4.4 (17)",
    },
    {
        "id": "c9",
        "icon": "📝",
        "title": "Reporting an AI Mistake",
        "category": "TEMPLATE",
        "desc": "When something goes wrong, the first hour matters more than the blame.",
        "body": (
            "A short template for the first hour after an AI mistake reaches a "
            "customer or a decision: what to capture, who to tell, and what not "
            "to do while it's still being sorted out."
        ),
        "price": "$19 · one-time",
        "rating": "4.2 (11)",
    },
)

CARD_BY_ID = {c["id"]: c for c in CARD_CATALOG}

MONTHLY_CARD_QUOTA = 10

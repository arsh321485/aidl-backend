"""Data for the Slack Admin cards and User cards (AIDL Slack Handover Guide,
sections 8 and 10).

Everything here is read-only: rendering a card never creates or changes a
row. When no signed-in user / organisation can be resolved, the cards show
the guide's demo data ("Northwind Logistics", admin "Priya", user "Jordan
Ellis") so the design can be previewed without Slack.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .cards_catalog_data import MONTHLY_CARD_QUOTA
from .models import AIDLUser, CardRequest, RegisteredApp
from .org_policy import get_answers, policy_effects
from .org_service import compute_org_metrics, get_organization_for_user
from .teams_cards import first_name

CURRICULUM_VERSION = "v2.4"
DEFAULT_POLICY_VERSION = "v3.1"

# Admin Center tabs, in the order the guide lists them (section 8). There is
# intentionally no Policy tab.
ADMIN_TABS = (
    ("home", "Home", "🏠"),
    ("add-admin", "Add Admin", "🧑‍💼"),
    ("add-user", "Add User", "👤"),
    ("cards", "Cards", "📬"),
    ("ai-apps", "AI Apps", "🤖"),
    ("it-apps", "IT Apps", "💻"),
)

USER_TABS = (
    ("home", "Home", "🏠"),
    ("license", "License", "🪪"),
    ("highway-code", "Highway Code", "📖"),
    ("traffic-light", "Traffic Light Check", "🚦"),
)

# Guide 8.4 — the 10 reference cards. Ids match CARD_CATALOG so per-org
# request/send state (CardRequest) lines up.
REFERENCE_CARDS = (
    {
        "id": "c0", "icon": "📄", "title": "Acceptable Use v3.1",
        "kicker": "Policy · Signature Required", "price": "$29", "rating": "4.1", "votes": 39,
        "desc": "Your live AUP with the summary your team actually reads.",
        "body": [
            "Your organization's live Acceptable Use Policy with the plain-language summary your "
            "team actually reads. Signing it is required before the Data Ethics Gate lets a "
            "license be upgraded.",
        ],
    },
    {
        "id": "c1", "icon": "🚦", "title": "Traffic Light Check",
        "kicker": "Decision Aid", "price": "$49", "rating": "4.8", "votes": 52,
        "desc": "Green, amber or red before you hand a task to AI. The three-second gut check.",
        "body": [
            "Green, amber or red before you hand a task to AI — the three-second gut check every "
            "team member runs before pasting anything into a tool. Choose which lights your team "
            "receives; they only see the ones you send.",
        ],
        "lights": True,
    },
    {
        "id": "c2", "icon": "🧩", "title": "Approved Apps Registry",
        "kicker": "Registry · {app_count} Entries", "price": "$59", "rating": "4.4", "votes": 27,
        "desc": "Which AI and IT tools are approved or prohibited, and for what data.",
        "body": [
            "Every AI and IT tool your organization has marked Approved or Prohibited, and what "
            "class of data each one may touch. Pulled live from the AI Apps and IT Apps tabs.",
        ],
    },
    {
        "id": "c3", "icon": "⚖️", "title": "Data Ethics Gate",
        "kicker": "Gate Briefing", "price": "$39", "rating": "4.6", "votes": 31,
        "desc": "What blocks a license: unsigned policy, unlogged agents, unreviewed output.",
        "body": [
            "What blocks a license upgrade: an unsigned Acceptable Use Policy, an unlogged AI "
            "agent run, or AI output sent without a human review. Clearing all three re-opens "
            "the gate.",
        ],
    },
    {
        "id": "c4", "icon": "🏛️", "title": "Mistake Museum",
        "kicker": "Worked Examples", "price": "$79", "rating": "4.9", "votes": 44,
        "desc": "Real failures from the team, de-identified, with what should have happened.",
        "body": [
            "Real, de-identified failures from your organization — what went wrong, and what "
            "should have happened instead.",
        ],
    },
    {
        "id": "c5", "icon": "🗂️", "title": "Data Classification Cheat Sheet",
        "kicker": "Reference Card", "price": "$25", "rating": "4.5", "votes": 19,
        "desc": "Four data classes, four different answers about what you may submit.",
        "body": [
            "Four data classes, four different answers about what you may submit. Public: "
            "anything goes. Internal: approved tools only. Confidential: approved enterprise "
            "tools, no training, logged. Restricted: never in a prompt, in any tool.",
            "Do this: classify the data before you classify the task.",
        ],
    },
    {
        "id": "c6", "icon": "✂️", "title": "Redaction Before Submission",
        "kicker": "Checklist", "price": "$35", "rating": "4.3", "votes": 15,
        "desc": "Before a document goes into a chat window, strip what the model doesn't need.",
        "body": [
            "Strip names, account numbers, addresses, IDs, pricing, comments and tracked changes "
            "before uploading a document. The tracked changes usually contain more than the "
            "document.",
            "Do this: save a redacted copy and upload that. Never the original.",
        ],
    },
    {
        "id": "c7", "icon": "🚧", "title": "Customer Data in Prompts",
        "kicker": "Gate Briefing", "price": "$69", "rating": "4.7", "votes": 22,
        "desc": "Customer data in a prompt is where the conversation stops.",
        "body": [
            "Identifiable customer data never goes into a general-purpose AI tool without a "
            "documented, approved pathway — however small the task.",
            "Do this: if you need to work with customer data, request the approved route.",
        ],
    },
    {
        "id": "c8", "icon": "✅", "title": "The Verification Pass",
        "kicker": "Checklist", "price": "$45", "rating": "4.4", "votes": 17,
        "desc": "Three checks before AI output leaves your desk. Every time.",
        "body": [
            "One: every fact and number verified against a source you opened. Two: nothing "
            "confidential that shouldn't be there. Three: you can defend it if challenged.",
            "Do this: run all three. If you can't do the first, you can't send it.",
        ],
    },
    {
        "id": "c9", "icon": "📝", "title": "Reporting an AI Mistake",
        "kicker": "Template", "price": "$19", "rating": "4.2", "votes": 11,
        "desc": "When something goes wrong, the first hour matters more than the blame.",
        "body": [
            "Report what you asked, what came back, what you did with it, and who might be "
            "affected. We're looking for the pattern, not the culprit.",
            "Do this: use the report template. Send it before you've worked out the whole story.",
        ],
    },
)

AI_CATEGORIES = (
    "Assistant / Chatbot", "Coding Assistant", "Writing & Content", "Image Generation",
    "Video Generation", "Audio / Voice", "Meeting Transcription", "Data Analysis",
    "Research & Search", "Automation / Agents", "Translation", "Customer Support", "Other",
)
IT_CATEGORIES = (
    "Identity & Access (SSO)", "Productivity Suite", "Messaging & Collaboration",
    "Project & Delivery", "Data & Storage", "CRM", "HR & Payroll", "Finance & Billing",
    "Security & Compliance", "Cloud Infrastructure", "Other",
)
# Label + traffic-light colour for each data class (guide 8.5).
DATA_ALLOWED = (
    ("Public only", "g"),
    ("Internal", "a"),
    ("Internal + Confidential", "r"),
    ("None", "r"),
)
_DATA_ALLOWED_LABEL = dict(RegisteredApp.DataAllowed.choices)

# Guide 10.3 — Section A, Learner Rules.
HIGHWAY_CODE = (
    {"sign": "STOP", "shape": "stop", "title": "Keep Private Things Private",
     "text": "Never paste passwords, ID numbers, bank details, or your home address into a consumer AI tool. Once it's in, you've lost control of it.",
     "full": "Once it's in a prompt, you've lost control of where it goes — some tools log and train on what you type. If a task needs private data, use an approved internal tool instead, or strip the sensitive fields out first."},
    {"sign": "CHECK", "shape": "check", "title": "Check Before You Trust",
     "text": "AI can state wrong things confidently. Verify facts, dates, and numbers against a real source before you rely on them.",
     "full": "AI predicts plausible text; it doesn't look facts up. Treat every date, number, name, or claim as a draft until you've verified it against a document, a database, or a person who'd know."},
    {"sign": "YOU", "shape": "yield", "title": "You're Still the Driver",
     "text": "AI drafts; you decide. Read and edit every output and make it your own before you use or send it.",
     "full": "Don't forward anything you haven't actually read. You're accountable for what goes out under your name, not the tool."},
    {"sign": "ASK", "shape": "ask", "title": "Better Prompt, Better Answer",
     "text": "Vague questions get vague answers. Say who the AI should be, what you want, and how it should look — that's the PREP habit.",
     "full": "Say who the AI should act as, what you actually want, and how the result should look (length, tone, format). Specific prompts save you the second and third round of edits."},
    {"sign": "ONE WAY", "shape": "oneway", "title": "Mind What You Share",
     "text": "Free tools may learn from what you type. Treat every prompt like a postcard — assume it could be read.",
     "full": "Assume anything you type could be read by someone else, and prefer your organization's approved, contracted tools for anything sensitive."},
)

# Guide 10.4 — the three lights. Users only see the ones an admin sent.
TRAFFIC_LIGHTS = (
    {"id": "green", "dot": "g", "label": "GREEN · GO", "tagline": "Public, non-personal.",
     "items": ["General questions & explanations", "Public articles to summarise", "Story, recipe, and idea prompts"],
     "action": "Any tool you like — then check facts."},
    {"id": "amber", "dot": "a", "label": "AMBER · CAUTION", "tagline": "A little personal.",
     "items": ["Your first name or city", "Your rough plans or preferences", "Non-sensitive everyday details"],
     "action": "Use a placeholder or remove it first."},
    {"id": "red", "dot": "r", "label": "RED · STOP", "tagline": "Private, keep it out.",
     "items": ["Passwords, PINs, verification codes", "Bank/card numbers, national ID", "Home address, other people's data"],
     "action": "Never paste. Anonymise, then retry."},
)

_DEMO_AI_APPS = (
    ("Claude Enterprise", "Assistant / Chatbot", "Internal + Confidential", "Approved", "Platform Eng"),
    ("GitHub Copilot", "Coding Assistant", "Internal", "Approved", "Platform Eng"),
    ("Notion AI", "Writing & Content", "Internal", "Approved", "Ops"),
    ("Otter.ai", "Meeting Transcription", "Public only", "Prohibited", "Ops"),
    ("Midjourney", "Image Generation", "Public only", "Approved", "Brand"),
    ("Consumer free-tier chatbots", "Assistant / Chatbot", "None", "Prohibited", "InfoSec"),
)
_DEMO_IT_APPS = (
    ("Okta SSO", "Identity & Access (SSO)", "Internal + Confidential", "Approved", "IT"),
    ("Google Workspace", "Productivity Suite", "Internal + Confidential", "Approved", "IT"),
    ("Slack (Enterprise Grid)", "Messaging & Collaboration", "Internal", "Approved", "IT"),
    ("Jira + Confluence", "Project & Delivery", "Internal", "Approved", "PMO"),
    ("Snowflake", "Data & Storage", "Internal + Confidential", "Approved", "Data"),
    ("Personal cloud drives", "Data & Storage", "None", "Prohibited", "InfoSec"),
)


def org_initials(name: str) -> str:
    words = [w for w in (name or "").replace("-", " ").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "AI"


def _verify_url(licence_number: str) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/home#verify?lic={licence_number}"


def _app_row(name, category, data_allowed, status, owner="") -> dict:
    colour = dict(DATA_ALLOWED).get(data_allowed, "a")
    return {
        "name": name,
        "category": category,
        "data_allowed": data_allowed,
        "data_dot": colour,
        "status": status,
        "owner": owner,
    }


def _registry_rows(apps) -> list[dict]:
    rows = []
    for app in apps:
        if app.status == RegisteredApp.Status.PENDING:
            continue
        rows.append(
            _app_row(
                app.name,
                app.category or "Other",
                _DATA_ALLOWED_LABEL.get(app.data_allowed, "Internal"),
                "Approved" if app.status == RegisteredApp.Status.APPROVED else "Prohibited",
            )
        )
    return rows


def _visible_admin_tabs(user: AIDLUser | None, is_primary: bool) -> list[str]:
    """Guide 8.2 — Home always; Add Admin/Add User only for the admin who
    signed the organization up; the rest by permission chip."""
    if user is None or is_primary:
        return [tab_id for tab_id, _l, _i in ADMIN_TABS]
    tabs = ["home"]
    if user.perm_access_cards or user.perm_create_card:
        tabs.append("cards")
    if user.perm_approve_apps:
        tabs += ["ai-apps", "it-apps"]
    return tabs


def _card_rows(org=None, app_count: int = 12) -> list[dict]:
    latest: dict[str, CardRequest] = {}
    if org is not None:
        for row in CardRequest.objects.filter(organization_id=str(org.pk)).order_by("-requested_at"):
            latest.setdefault(row.card_id, row)
    rows = []
    for card in REFERENCE_CARDS:
        req = latest.get(card["id"])
        state = "not_requested"
        when = ""
        if req is not None:
            state = {
                CardRequest.Status.SENT: "sent",
                CardRequest.Status.SCHEDULED: "scheduled",
            }.get(req.status, "requested")
            if req.scheduled_at:
                when = timezone.localtime(req.scheduled_at).strftime("%d %b, %H:%M")
        rows.append(
            {
                **card,
                "kicker": card["kicker"].format(app_count=app_count),
                "state": state,
                "scheduled_for": when,
            }
        )
    return rows


# ---------- guide 7.3: policy answers → card data ----------

_PUBLIC_AI_TOOLS = (("ChatGPT", "Assistant / Chatbot"), ("Claude", "Assistant / Chatbot"), ("Gemini", "Assistant / Chatbot"))


def _apply_card_effects(rows: list[dict], fx: dict) -> list[dict]:
    by_id = {r["id"]: r for r in rows}
    aup = by_id["c0"]
    if fx["aup_status"] == "not_available":
        aup.update(state="unavailable", kicker="Policy · Not available yet",
                   desc="Your organization has no written AI policy yet — this card unlocks once it's published.")
    elif fx["aup_status"] == "draft":
        aup.update(kicker="Policy · Draft · Signature Required",
                   desc="Your draft AUP with the summary your team actually reads — marked Draft until it's published.")

    if not fx["gate_blocks_unreviewed"]:
        gate = by_id["c3"]
        gate["desc"] = "What blocks a license: unsigned policy and unlogged agents."
        gate["body"] = ["What blocks a license upgrade: an unsigned Acceptable Use Policy or an unlogged AI "
                        "agent run. Clearing both re-opens the gate."]

    if fx["red_includes_confidential"]:
        by_id["c1"]["body"] = by_id["c1"]["body"] + [
            "Your policy: confidential and customer data never goes into an AI tool — it's always Red."]

    if fx["regulation_law"]:
        law = fx["regulation_law"]
        cheat = by_id["c5"]
        cheat["desc"] = f"Four data classes under {law}, four different answers about what you may submit."
        cheat["body"] = [f"Your organization works under {law}."] + cheat["body"]

    if fx["report_to"]:
        rep = by_id["c9"]
        rep["desc"] = f"When something goes wrong, report it to {fx['report_to']} — the first hour matters more than the blame."
        rep["body"] = rep["body"] + [f"Where to report: {fx['report_to']}."]

    for cid in fx["recommended_cards"]:
        by_id[cid]["recommended"] = True
    # Recommended cards first, in the order the answers flagged them.
    first = [by_id[cid] for cid in fx["recommended_cards"]]
    return first + [r for r in rows if r["id"] not in fx["recommended_cards"]]


def _apply_app_effects(ai_apps: list[dict], fx: dict) -> list[dict]:
    apps = [dict(a) for a in ai_apps]
    if fx["apps_mode"] == "company_only":
        consumer = next((a for a in apps if "consumer" in a["name"].lower()), None)
        if consumer:
            apps.remove(consumer)
        else:
            consumer = _app_row("Consumer free-tier chatbots", "Assistant / Chatbot", "None", "Prohibited", "InfoSec")
        consumer.update(status="Prohibited", data_allowed="None", data_dot="r")
        apps.insert(0, consumer)
    elif fx["apps_mode"] == "any_public":
        top = []
        for name, category in _PUBLIC_AI_TOOLS:
            existing = next((a for a in apps if a["name"].lower() == name.lower()), None)
            if existing:
                apps.remove(existing)
            row = existing or _app_row(name, category, "Public only", "Approved")
            row.update(status="Approved", data_allowed="Public only", data_dot="g")
            top.append(row)
        apps = top + apps
    if fx["enterprise_only"]:
        for a in apps:
            if a["status"] != "Approved":
                continue
            enterprise = "enterprise" in a["name"].lower()
            a.update(data_allowed="Internal + Confidential" if enterprise else "Internal",
                     data_dot="r" if enterprise else "a")
    return apps


def build_admin_cards_context(user: AIDLUser | None) -> dict:
    org = get_organization_for_user(user) if user is not None else None
    today = timezone.localdate()
    available_from = today.replace(day=1)
    available_to = (available_from + timedelta(days=62)).replace(day=1) - timedelta(days=1)

    fx = policy_effects(get_answers(org))
    if org is None:
        ai_apps = [_app_row(*a) for a in _DEMO_AI_APPS]
        it_apps = [_app_row(*a) for a in _DEMO_IT_APPS]
        approved = sum(1 for a in ai_apps + it_apps if a["status"] == "Approved")
        data = {
            "demo": True,
            "org_name": "Northwind Logistics",
            "admin_name": "Priya",
            "admin_email": "priya@northwind.co",
            "seats_purchased": 50, "seats_renew": "01 Mar",
            "licences_issued": 18, "enrolled": 32, "aup_unsigned": 6,
            "admin_count": 2, "admin_seat_limit": 3,
            "approved_apps": approved, "total_apps": len(ai_apps) + len(it_apps),
            "rollout_done": 3, "rollout_total": 4,
            "quota_used": 0,
            "ai_apps": ai_apps, "it_apps": it_apps,
            "cards": _card_rows(None, len(ai_apps) + len(it_apps)),
            "tabs": [t[0] for t in ADMIN_TABS],
            "can_manage_cards": True, "can_create_card": True,
        }
    else:
        metrics = compute_org_metrics(org)
        admins = sorted(metrics["admins"], key=lambda m: m.created_at or timezone.now())
        is_primary = bool(admins) and admins[0].pk == user.pk
        ai_apps = _registry_rows(metrics["ai_app_list"])
        it_apps = _registry_rows(metrics["it_app_list"])
        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        data = {
            "demo": False,
            "org_name": org.name,
            "admin_name": first_name(user.full_name),
            "admin_email": user.email,
            "seats_purchased": org.seats_purchased,
            "seats_renew": metrics["seats_renews_on"],
            "licences_issued": metrics["licences_issued"],
            "enrolled": metrics["enrolled"],
            "aup_unsigned": metrics["aup_unsigned"],
            "admin_count": metrics["admin_count"],
            "admin_seat_limit": metrics["admin_seat_limit"],
            "approved_apps": metrics["approved_apps"],
            "total_apps": metrics["total_apps"],
            "rollout_done": metrics["rollout_steps_done"],
            "rollout_total": metrics["rollout_steps_total"],
            "quota_used": CardRequest.objects.filter(
                organization_id=str(org.pk), requested_at__gte=month_start
            ).count(),
            "ai_apps": ai_apps,
            "it_apps": it_apps,
            "cards": _card_rows(org, metrics["total_apps"]),
            "tabs": _visible_admin_tabs(user, is_primary),
            "can_manage_cards": is_primary or user.perm_access_cards,
            "can_create_card": is_primary or user.perm_create_card,
        }

    data["policy"] = fx
    data["cards"] = _apply_card_effects(data["cards"], fx)
    data["ai_apps"] = _apply_app_effects(data["ai_apps"], fx)
    shown = data["ai_apps"] + data["it_apps"]
    data["approved_apps"] = sum(1 for a in shown if a["status"] == "Approved")
    data["total_apps"] = len(shown)
    # No written policy yet → nothing to sign, so the stat shows "—".
    data["aup_display"] = "—" if fx["aup_status"] == "not_available" else str(data["aup_unsigned"])
    data["aup_warn"] = fx["aup_status"] != "not_available" and data["aup_unsigned"] > 0

    enrolled = data["enrolled"]
    data.update(
        {
            "licence_pct": round(data["licences_issued"] / enrolled * 100) if enrolled else 0,
            "quota_max": MONTHLY_CARD_QUOTA,
            "available_from": available_from.strftime("%d %b %Y"),
            "available_to": available_to.strftime("%d %b %Y"),
            "ai_categories": list(AI_CATEGORIES),
            "it_categories": list(IT_CATEGORIES),
            "data_allowed": [{"label": l, "dot": d} for l, d in DATA_ALLOWED],
        }
    )
    data["tab_defs"] = [
        {"id": t, "label": l, "icon": i} for t, l, i in ADMIN_TABS if t in data["tabs"]
    ]
    return data


def build_user_cards_context(user: AIDLUser | None, lights: list[str] | None = None) -> dict:
    lights = [l for l in (lights or []) if l in {"green", "amber", "red"}] or ["green", "amber", "red"]
    fx = policy_effects(get_answers(get_organization_for_user(user) if user else None))
    if user is None:
        org_name = "Northwind Logistics"
        data = {
            "demo": True,
            "full_name": "Jordan Ellis",
            "licence_number": "AIDL-L-0455-2210",
            "licence_class": "L",
            "issued": "02/14/2026",
            "expires": "02/14/2027",
            "status": "ACTIVE",
            "avatar_url": "",
            "policy_version": DEFAULT_POLICY_VERSION,
        }
    else:
        org = get_organization_for_user(user)
        org_name = (org.name if org else "") or user.organization_name or "your organization"
        issued = user.licence_issued_at
        expires = user.licence_expires_at
        data = {
            "demo": False,
            "full_name": user.full_name or user.email,
            "licence_number": user.licence_number or "Pending",
            # Class F is not modelled yet — every issued license is a Learner's Permit.
            "licence_class": "L",
            "issued": issued.strftime("%m/%d/%Y") if issued else "—",
            "expires": expires.strftime("%m/%d/%Y") if expires else "—",
            "status": "ACTIVE" if user.licence_issued else "PENDING",
            "avatar_url": user.avatar_url,
            "policy_version": DEFAULT_POLICY_VERSION,
        }

    data.update(
        {
            "org_name": org_name,
            "org_initials": org_initials(org_name),
            "first_name": first_name(data["full_name"]),
            "curriculum_version": CURRICULUM_VERSION,
            "verify_url": _verify_url(data["licence_number"]),
            "highway_code": _highway_code(fx),
            "lights": _traffic_lights(fx, lights),
            "training_note": fx["training_note"],
            "tab_defs": [{"id": t, "label": l, "icon": i} for t, l, i in USER_TABS],
        }
    )
    return data


def _highway_code(fx: dict) -> list[dict]:
    rules = [dict(r) for r in HIGHWAY_CODE]
    if fx["disclose_ai"]:
        driver = next(r for r in rules if r["shape"] == "yield")
        driver["text"] += " Say when AI helped."
    return rules


def _traffic_lights(fx: dict, chosen: list[str]) -> list[dict]:
    lights = []
    for light in TRAFFIC_LIGHTS:
        if light["id"] not in chosen:
            continue
        light = dict(light, items=list(light["items"]))
        if light["id"] == "red" and fx["red_includes_confidential"]:
            light["items"].append("All confidential and customer data")
        lights.append(light)
    return lights

"""The school's setup after sign-up: kept short, so teachers have nothing to run.

    1. School details   school name + roughly how many students (and teachers). An
                        estimate only: it suggests a plan, it never limits anyone.
    2. AI policy        AIDL's school policy, already filled in, on one screen. One
                        click uses it; editing topic by topic stays optional.
    3. Done             automatic: the two starter cards (Traffic Light Check,
                        Highway Code) are approved, the programme starts, and on
                        Enterprise the students of the admin's other classes are
                        invited to the AIDL class.

No admins step, no card approvals, no "Start the programme" button: the admin
who signs the school up does these two screens and AIDL does the rest.

Pages: setup_views.py. Uses only api/ functions (the same rules as Slack / Teams).
"""

from __future__ import annotations

from django.utils import timezone

from api import admin_setup, aup_store, card_approval
from api import policy_cards as pc
from api.models import AIDLUser, Organization

from .models import SchoolSetup

# (key, label, upper bound or None). The key is saved; the bound is for plan suggestions.
STUDENT_RANGES = (
    ("50", "Up to 50", 50),
    ("200", "50–200", 200),
    ("500", "200–500", 500),
    ("1000", "500–1,000", 1000),
    ("1000+", "More than 1,000", None),
)
TEACHER_RANGES = (
    ("10", "Up to 10", 10),
    ("50", "10–50", 50),
    ("50+", "More than 50", None),
)
_STUDENT_LABELS = {k: label for k, label, _ in STUDENT_RANGES}
_TEACHER_LABELS = {k: label for k, label, _ in TEACHER_RANGES}
_STUDENT_BOUND = {k: bound for k, _l, bound in STUDENT_RANGES}

# The answers a school's policy needs that AIDL's question bank leaves open, pre-filled
# for a school on Google Workspace. The admin can change each one on the policy screen.
SCHOOL_DEFAULTS = {
    "speaker": "the Principal",  # 8.5: who may speak publicly for the school
    "storage": "Google Drive",  # 6.2.b: the approved cloud storage
    "tools": "Google Gemini",  # 7: the approved AI tools (comma separated)
}
NO_PASSWORD_STANDARD = "5.4.c"  # 5.4: "No standard password manager yet"
STORAGE_OPTION, REPORT_OPTION = "6.2.b", "9.2.a"


# --- state ---

def get(org: Organization) -> SchoolSetup | None:
    return SchoolSetup.objects.filter(organization_id=str(org.pk)).first()


def details_done(org: Organization) -> bool:
    row = get(org)
    return bool(row and row.approx_students)


def policy_done(org: Organization) -> bool:
    return aup_store.is_complete(org)


def next_url(org: Organization) -> str:
    """Where an admin must go before anything else: "" once the school details are in.
    (The policy is asked right after, but the Admin Center also reminds them of it.)"""
    from django.urls import reverse

    return "" if details_done(org) else reverse("google-classroom-setup")


# --- 1. school details ---

def student_label(key: str) -> str:
    return _STUDENT_LABELS.get(key, "")


def teacher_label(key: str) -> str:
    return _TEACHER_LABELS.get(key, "")


def save_details(org: Organization, by: AIDLUser, name: str, students: str, teachers: str) -> str:
    """Returns "" when saved, else what's missing."""
    name = " ".join((name or "").split())[:120]
    if not name:
        return "Enter your school's name."
    if students not in _STUDENT_LABELS:
        return "Choose roughly how many students your school has."
    if teachers and teachers not in _TEACHER_LABELS:
        teachers = ""
    if name != org.name:
        org.name = name
        org.save(update_fields=["name", "updated_at"])
    row, _ = SchoolSetup.objects.get_or_create(organization_id=str(org.pk))
    row.approx_students, row.approx_teachers = students, teachers
    row.details_by, row.details_at = str(by.pk), timezone.now()
    row.save()
    return ""


def plan_hint(org: Organization) -> dict | None:
    """When the plan has fewer student seats than the school's estimate: what to tell the
    admin. None when the plan already covers it (Enterprise has no limit)."""
    from api.plans import PLANS, plan_key

    from .plan_limits import limits

    row = get(org)
    if row is None or not row.approx_students:
        return None
    limit = limits(org)["students"]
    if limit is None:
        return None
    bound = _STUDENT_BOUND.get(row.approx_students)
    if bound is not None and bound <= limit:
        return None
    return {"plan": PLANS[plan_key(org)]["label"], "limit": limit, "estimate": student_label(row.approx_students)}


# --- 2. the school's AI policy, on one screen ---

def topics(org: Organization) -> list[int]:
    """The policy topics this school's plan includes (the Trial: 3 of 14)."""
    return [n for n in pc.ordered(org) if not pc.is_locked(org, n)]


def school_answers(org: Organization, admin: AIDLUser) -> dict:
    """{answers, fills, tools}: what the admin chose before, AIDL's recommended answers,
    and school defaults for the few questions the question bank leaves open."""
    a = aup_store.answers(org, with_tools=False)
    answers, fills = dict(a["answers"]), dict(a["fills"])
    answers.setdefault("5.4", NO_PASSWORD_STANDARD)
    answers.setdefault("8.5", SCHOOL_DEFAULTS["speaker"])
    if STORAGE_OPTION in (answers.get("6.2") or []):
        fills.setdefault(STORAGE_OPTION, SCHOOL_DEFAULTS["storage"])
    if REPORT_OPTION in (answers.get("9.2") or []):
        fills.setdefault(REPORT_OPTION, admin.email or "")
    tools = a["tools"] or _split(SCHOOL_DEFAULTS["tools"])
    return {"answers": answers, "fills": fills, "tools": aup_store.allowed_tools(org, tools)}


def fields(a: dict) -> dict:
    """The few things the admin may change on the policy screen."""
    return {"speaker": a["answers"].get("8.5", ""), "storage": a["fills"].get(STORAGE_OPTION, ""),
            "report": a["fills"].get(REPORT_OPTION, ""), "tools": ", ".join(a["tools"])}


def with_fields(org: Organization, a: dict, post) -> dict:
    """The answers with the policy screen's fields applied."""
    answers, fills = dict(a["answers"]), dict(a["fills"])
    answers["8.5"] = (post.get("speaker") or "").strip()[:200]
    if STORAGE_OPTION in (answers.get("6.2") or []):
        fills[STORAGE_OPTION] = (post.get("storage") or "").strip()[:200]
    if REPORT_OPTION in (answers.get("9.2") or []):
        fills[REPORT_OPTION] = (post.get("report") or "").strip()[:200]
    return {"answers": answers, "fills": fills, "tools": aup_store.allowed_tools(org, _split(post.get("tools", "")))}


def _split(text: str) -> list[str]:
    out = []
    for t in (text or "").replace("\n", ",").split(","):
        t = " ".join(t.split())[:80]
        if t and t not in out:
            out.append(t)
    return out[:20]


def preview(org: Organization, a: dict) -> list[dict]:
    """The policy as learners will see it: one card per topic."""
    out = []
    for n in topics(org):
        out.append({"n": n, "title": pc.SHORT[n], "icon": pc.ICONS[n], "lines": pc.lines(n, a),
                    "problems": list(pc.problems(n, a).values()), "approved": aup_store.card(org, n) is not None})
    return out


def use_policy(org: Organization, admin: AIDLUser, a: dict) -> tuple[int, list[str]]:
    """Approve every topic of the plan with these answers. Returns (cards approved, problems);
    nothing is approved while any topic has a problem."""
    ns = topics(org)
    problems = [f"{pc.SHORT[n]}: {msg}" for n in ns for msg in pc.problems(n, a).values()]
    if problems:
        aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
        return 0, problems
    for n in ns:
        pc.approve(org, admin, n, a)
    return len(ns), []


# --- 3. done: everything else is automatic ---

def auto_start(org: Organization, admin: AIDLUser) -> bool:
    """Once the details and the policy are in: approve the two starter cards and start the
    programme (the steps teachers used to do by hand). True when it started just now."""
    from integrations.moodle import flow

    row = get(org)
    if row is None or not row.approx_students or not policy_done(org):
        return False
    if row.finished_at is None:
        row.finished_at = timezone.now()
        row.save(update_fields=["finished_at", "updated_at"])
    if admin_setup.started(org):
        return False
    for item in card_approval.ONBOARDING:
        if not card_approval.is_approved(org, item):
            flow.teacher_action(admin, org, {"action": "approve", "item": item})
    flow.teacher_action(admin, org, {"action": "start"})
    return admin_setup.started(org)


def invite_everyone(org: Organization, admin: AIDLUser) -> dict:
    """On finishing: invite the students of all the admin's other Google Classroom classes
    to the AIDL class (the "Select all students" button, done for them). Enterprise only;
    otherwise the reason is saved and shown. Never raises."""
    from . import classes
    from .google_oauth import GoogleError

    row = get(org)
    if not classes.bulk_invite_allowed(org):
        result = {"skipped": "plan"}
    else:
        ids = [c["id"] for c in classes.other_classes(admin) if c["new"]]
        if not ids:
            result = {"skipped": "nobody"}
        else:
            try:
                result = classes.invite_from_classes(admin, ids)
            except GoogleError as exc:
                result = {"skipped": "error", "message": str(exc)}
    if row is not None:
        row.invited = result
        row.save(update_fields=["invited", "updated_at"])
    return result


def summary(org: Organization) -> dict:
    """For the Admin Center and the done screen."""
    row = get(org)
    return {
        "details": bool(row and row.approx_students),
        "policy": policy_done(org),
        "started": admin_setup.started(org),
        "students": student_label(row.approx_students) if row else "",
        "teachers": teacher_label(row.approx_teachers) if row else "",
        "invited": (row.invited if row else {}) or {},
        "hint": plan_hint(org),
    }

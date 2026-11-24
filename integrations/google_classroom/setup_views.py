"""The school's short setup after sign-up (onboarding.py): pages.

GET/POST /api/google-classroom/admin/setup/          1. school details (roughly how many students)
GET/POST /api/google-classroom/admin/setup/policy/   2. the school's AI policy, one screen
GET      /api/google-classroom/admin/setup/done/     done: what AIDL did automatically (no AIDL class only)

With an AIDL class, each step is its own page, opened from that class's Classwork in a
new tab: when it's done (?saved=1) it says so and goes back to Google Classroom, where the
next step is. Without one (Classroom permissions not granted), the steps run one after another.
"""

from __future__ import annotations

from django.shortcuts import redirect
from django.urls import reverse

from . import onboarding
from .admin_views import _admin_page, _message, _render


def _may_edit_policy(org, user) -> bool:
    from .policy_views import _can

    return _can(org, user)["policy"]


def _classroom_link(org) -> str:
    from .aidl_class import info

    return info(org)["link"]


def _saved(request, user, org, step: int, heading: str, lines: list[str], hint=None, delay_ms: int = 2500):
    """A step is done: say so, then back to Google Classroom (where the next step is)."""
    return _render(request, user, org, "setup/saved.html", step=step, heading=heading, lines=lines, hint=hint,
                   delay_ms=delay_ms)


@_admin_page
def details(request, user, org):
    row = onboarding.get(org)
    in_classroom = bool(_classroom_link(org))
    if request.GET.get("saved") and in_classroom and row and row.approx_students:
        return _saved(request, user, org, 1, "✅ School details saved", [
            f"{org.name} · about {onboarding.student_label(row.approx_students).lower()} students.",
            "Next: “Step 2 · Your AI policy” in Google Classroom's Classwork."])
    error, form = "", {"name": org.name, "students": row.approx_students if row else "",
                       "teachers": row.approx_teachers if row else ""}
    if request.method == "POST":
        form = {k: request.POST.get(k, "") for k in ("name", "students", "teachers")}
        error = onboarding.save_details(org, user, form["name"], form["students"], form["teachers"])
        if not error:
            # Opened from Classwork: back to Classroom, where Step 2 is. Otherwise straight on.
            return redirect(f"{reverse('google-classroom-setup')}?saved=1" if in_classroom
                            else reverse("google-classroom-setup-policy"))
    return _render(request, user, org, "setup/details.html", form=form, error=error, step=1,
                   student_ranges=onboarding.STUDENT_RANGES, teacher_ranges=onboarding.TEACHER_RANGES)


@_admin_page
def policy(request, user, org):
    if not onboarding.details_done(org):
        return redirect("google-classroom-setup")
    if not _may_edit_policy(org, user):
        return _message(request, "Your AI policy", "Your admin permissions don't include the policy. "
                                                   "Ask your master admin.", 403)
    in_classroom = bool(_classroom_link(org))
    if request.GET.get("saved") and in_classroom and onboarding.policy_done(org):
        return _policy_saved(request, user, org)
    a, problems = onboarding.school_answers(org, user), []
    if request.method == "POST":
        a = onboarding.with_fields(org, a, request.POST)
        if request.POST.get("action") == "use":
            count, problems = onboarding.use_policy(org, user, a)
            if not problems:
                onboarding.auto_start(org, user)
                onboarding.invite_everyone(org, user)
                return redirect(f"{reverse('google-classroom-setup-policy')}?saved=1" if in_classroom
                                else reverse("google-classroom-setup-done"))
        else:  # "Update preview": keep what they typed
            from api import aup_store

            aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
    return _render(request, user, org, "setup/policy.html", step=2, fields=onboarding.fields(a),
                   cards=onboarding.preview(org, a), problems=problems, done=onboarding.policy_done(org),
                   asks_storage=onboarding.STORAGE_OPTION in (a["answers"].get("6.2") or []),
                   asks_report=onboarding.REPORT_OPTION in (a["answers"].get("9.2") or []))


def _policy_saved(request, user, org):
    from api import policy_cards as pc

    s = onboarding.summary(org)
    n = len(pc.statuses(org))
    lines = [f"Your students agree to it ({n} policy card{'s' if n != 1 else ''}) before their AI Driving Licence."]
    if s["started"]:
        lines.append("🚀 The programme has started: students get their cards as soon as they join the AIDL class.")
    invited = s["invited"]
    if invited.get("invited"):
        lines.append(f"📨 {invited['invited']} student{'s' if invited['invited'] != 1 else ''} from your other "
                     "classes invited. Each one presses Join in Google Classroom.")
    lines.append("👩‍🏫 Teachers have nothing else to do: just tell your students about AIDL.")
    return _saved(request, user, org, 2, "✅ Your AI policy is in use", lines, hint=s["hint"], delay_ms=6000)


@_admin_page
def done(request, user, org):
    if not onboarding.details_done(org):
        return redirect("google-classroom-setup")
    if not onboarding.policy_done(org):
        return redirect("google-classroom-setup-policy")
    onboarding.auto_start(org, user)  # e.g. the policy was approved topic by topic instead
    from .aidl_class import info

    return _render(request, user, org, "setup/done.html", step=3, s=onboarding.summary(org),
                   aidl_class=info(org))

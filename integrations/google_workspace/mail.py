"""AIDL for Google Workspace lives in Gmail: the admin's Admin Center and every
employee's steps arrive as emails. Each button opens just that step in AIDL,
already signed in (links.py), and the next email follows.

    admin_center()   the admin, right after sign-up: the setup steps
    started()        the admin, when the programme starts: invite the team
    invite()         a colleague the admin invited: join with Google
    learner_step()   an employee: Welcome, the AI policy, Traffic Light Check,
                     Highway Code, avatar, licence (one email per step)

Sent with Django's email settings (SendGrid / SMTP, api/sendgrid_backend.py).
Without them (local), emails are printed in the server console instead.
Sending never breaks a page: failures are logged and the function returns False.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from api.models import AIDLUser, Organization

from . import links

logger = logging.getLogger(__name__)

TEMPLATE = "google_workspace/email/message.html"


def gmail_url(email: str = "") -> str:
    """Gmail, in the right Google account when someone is signed in to several."""
    return f"https://mail.google.com/mail/u/?authuser={email}" if email else "https://mail.google.com/mail/"


PANEL_HINT = "👉 Click the AIDL icon in the side panel on the right of this email."
JOIN_HINT = "👉 Click the AIDL icon in the side panel on the right of this email to join."


def _open(user: AIDLUser, label: str, path: str = "") -> dict:
    """What the email asks for. With the Gmail side panel switched on (addon_auth), no button:
    the reader is already in Gmail, so AIDL is one click away in the side panel. A small web link
    stays for anyone who doesn't see the icon yet. Without the panel: a button to the AIDL page."""
    from .addon_auth import enabled

    web = links.link(user, path)
    if not enabled():
        return {"button": (label, web)}
    return {"hint": PANEL_HINT, "fallback": ("Don't see the AIDL icon? Open it in your browser", web)}


def _name(org: Organization) -> str:
    """The company name for an email's text. Workspace companies are often named after their domain
    (e.g. "acme.com"), which Gmail turns into a link to a website that may not exist; a zero-width
    space after each dot keeps it plain text without changing how it looks."""
    return (org.name or "").replace(".", ".​")


def _first(user: AIDLUser) -> str:
    return (user.first_name or user.full_name or "there").split()[0]


def _text(subject: str, ctx: dict) -> str:
    """The plain-text version (some mail apps show only this), with the button's link written out."""
    lines = [subject, ""]
    if ctx.get("first"):
        lines += [f"Hi {ctx['first']},", ""]
    lines += [ctx.get("intro", ""), ""]
    for i, step in enumerate(ctx.get("steps") or [], 1):
        lines.append(f"{'[done]' if step['done'] else f'{i}.'} {step['title']}: {step['text']}")
    if ctx.get("steps"):
        lines.append("")
    if ctx.get("button"):
        lines += [f"{ctx['button'][0]}: {ctx['button'][1]}", ""]
    if ctx.get("hint"):
        lines += [ctx["hint"], ""]
    if ctx.get("fallback"):
        lines += [f"{ctx['fallback'][0]}: {ctx['fallback'][1]}", ""]
    if ctx.get("small"):
        lines.append(ctx["small"])
    return "\n".join(lines).strip() + "\n"


def _send(to: str, subject: str, ctx: dict) -> bool:
    if not to:
        return False
    org = ctx.get("org")
    heading = subject.replace(org.name, _name(org)) if org is not None and org.name else subject
    html = render_to_string(TEMPLATE, {"subject": heading, **ctx})
    msg = EmailMultiAlternatives(subject, _text(subject, ctx), settings.DEFAULT_FROM_EMAIL, [to])
    msg.attach_alternative(html, "text/html")
    try:
        msg.send()
    except Exception as exc:  # noqa: BLE001  (a page must never fail over an email)
        logger.warning("google workspace email %r to %s not sent: %s", subject, to, exc)
        return False
    return True


# ---------- admins ----------

def admin_center(user: AIDLUser, org: Organization) -> bool:
    from integrations.moodle import flow

    t = flow.teacher_context(org)
    steps = [{"done": c["status"] == "approved", "title": f"Approve the {c['label']}",
              "text": "A starter card every employee gets. Read it, then approve it."} for c in t["onboarding"]]
    steps.append({"done": t["started"], "title": "Start the programme",
                  "text": "Everyone who joins gets their Welcome email straight away."})
    steps.append({"done": False, "title": "Invite your team",
                  "text": f"AIDL emails each colleague a link to join {_name(org)} with their Google account."})
    return _send(user.email, f"AIDL Admin Center · {org.name}", {
        "first": _first(user), "org": org,
        "intro": f"You're the AIDL admin for {_name(org)}. Everything runs from your Gmail: finish these steps "
                 "and your team gets their AI Driving Licence cards by email.",
        "steps": steps,
        **_open(user, "Open the Admin Center"),
    })


def started(user: AIDLUser, org: Organization) -> bool:
    return _send(user.email, f"🚀 AIDL is running at {org.name}", {
        "first": _first(user), "org": org,
        "intro": "The programme has started. Invite your team: each person gets a Welcome email, then their "
                 "cards one step at a time, and their licence when they finish.",
        **_open(user, "Invite your team", reverse("google-workspace-home") + "#invite"),
    })


def invite(email: str, admin: AIDLUser, org: Organization) -> bool:
    """With the Gmail side panel on, colleagues join by opening AIDL in the panel (it knows their work
    account, addon._person), so the email doesn't send them to a sign-in page."""
    from .addon_auth import enabled

    join = f"{settings.BACKEND_URL.rstrip('/')}{reverse('google-workspace-join')}"
    how = ({"hint": JOIN_HINT, "fallback": ("Don't see the AIDL icon? Join in your browser", join)} if enabled()
           else {"button": ("Join with Google", join), "small": "Sign in with your work Google account."})
    return _send(email, f"{admin.full_name or 'Your admin'} invited you to AIDL at {org.name}", {
        "first": "", "org": org,
        "intro": f"{_name(org)} uses AIDL, the AI Driving Licence: agree to the company's AI policy point by point, "
                 "take two short checks and get your licence. It takes about ten minutes, all from your Gmail.",
        **how,
    })


# ---------- employees: one email per step ----------

STEPS = {
    "welcome": ("👋 Welcome to your AI Driving Licence",
                "{org} has started its AI Driving Licence programme. Three short steps, about ten minutes, "
                "and your licence arrives here in Gmail.", "Let's go"),
    "policy": ("📜 Your AI policy cards",
               "Read {org}'s AI policy, one short card at a time, and agree to each point.", "Read the policy"),
    "traffic_light": ("🚦 Your Traffic Light Check",
                      "Which AI uses are green, amber or red at {org}? One card and a quick check.",
                      "Open the Traffic Light Check"),
    "highway_code": ("📖 Your Highway Code", "The last card before your licence: AIDL's Highway Code for AI.",
                     "Open the Highway Code"),
    "avatar": ("🎨 One last step: your licence avatar",
               "You've finished every card. Pick your avatar and your licence is ready.", "Create my avatar"),
    "licence": ("🎉 Your AI Driving Licence", "You're road-ready! Your licence is valid for one year.",
                "See my licence"),
}


def step_of(user: AIDLUser, org: Organization) -> str:
    """Which email the employee needs now (a STEPS key), or "" for none (waiting for the admin)."""
    from api import admin_setup, aup_store, card_approval
    from api.employee_flow import stage

    if user.role == AIDLUser.Role.ADMIN or not admin_setup.started(org):
        return ""
    if not user.slack_onboarded_at:
        return "welcome"
    if user.licence_issued:
        return "licence" if user.avatar_data else "avatar"
    if aup_store.pending_points(user, org):  # the page shows the policy cards first
        return "policy"
    now = stage(user, org)
    if now in ("traffic_light", "highway_code") and not card_approval.is_approved(org, now):
        return ""
    if now == "licence":
        return "" if not user.licence_issued else "licence"
    return now


def learner_step(user: AIDLUser, org: Organization, step: str) -> bool:
    if step not in STEPS:
        return False
    from .addon_auth import enabled

    if step == "licence" and enabled():
        return False  # the licence is shown in the Gmail side panel itself: no email
    subject, text, button = STEPS[step]
    ctx = {"first": _first(user), "org": org, "intro": text.format(org=_name(org)), **_open(user, button)}
    if step == "licence" and user.licence_number:
        from api import licence_image

        ctx["image"] = licence_image.image_url(user.licence_number)
        ctx["small"] = f"Licence number {user.licence_number}"
    return _send(user.email, subject, ctx)


def send_next(user: AIDLUser, org: Organization) -> bool:
    """Email the employee's current step if they haven't had that email yet. True = sent now."""
    from django.utils import timezone

    from .models import WorkspaceMember

    step = step_of(user, org)
    if not step:
        return False
    row, _ = WorkspaceMember.objects.get_or_create(user_id=str(user.pk), defaults={"organization_id": str(org.pk)})
    if row.last_step == step:
        return False
    if not learner_step(user, org, step):
        return False
    row.last_step, row.last_sent_at, row.organization_id = step, timezone.now(), str(org.pk)
    row.save(update_fields=["last_step", "last_sent_at", "organization_id", "updated_at"])
    return True


def has_email(user: AIDLUser) -> bool:
    """Has the employee had any step email (so Gmail has something to open)?"""
    from .models import WorkspaceMember

    return WorkspaceMember.objects.filter(user_id=str(user.pk)).exclude(last_step="").exists()


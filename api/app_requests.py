"""Employees ask for an app to be approved ("🛣 Request an app" on the
licence card and every awareness card).

    Employee → popup: app, website, what for, what data (🟢 / 🟠 / 🔴)
      already approved  → told at once, nothing to approve
      already requested → added to that request ("2 people asked")
      otherwise         → DM to every admin who can approve apps (+ the master)
                          with ✅ Approve · ❌ Reject (reason) · 💬 Ask for more info
    Approve → the app joins the organization's approved AI apps
              (RegisteredApp, shown in the AUP / Highway Code app lists)
    Every requester gets the answer by DM; the admin messages show who decided.
"""

from __future__ import annotations

import json

from django.utils import timezone

from . import slack_client
from .models import AIDLUser, AppRequest, Organization, RegisteredApp

DATA = {
    "green": ("🟢 Public information only", RegisteredApp.DataAllowed.PUBLIC_ONLY),
    "amber": ("🟠 Internal, or a little personal", RegisteredApp.DataAllowed.INTERNAL),
    "red": ("🔴 Confidential or personal data", RegisteredApp.DataAllowed.INTERNAL_CONFIDENTIAL),
}
CALLBACK, REJECT, INFO = "aidl_app_request", "aidl_app_reject", "aidl_app_info"


def _t(text: str) -> dict:
    return {"type": "plain_text", "text": text, "emoji": True}


def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _input(block_id, label, element, optional=False):
    return {"type": "input", "block_id": block_id, "label": _t(label), "element": element, "optional": optional}


def _slack_id(user: AIDLUser) -> str:
    return user.microsoft_id.split(":")[-1] if user.microsoft_id.startswith("slack:") else ""


# ---------- employee ----------

def request_view() -> dict:
    return {"type": "modal", "callback_id": CALLBACK, "title": _t("Request an app"), "submit": _t("Send request"),
            "close": _t("Cancel"), "blocks": [
        {"type": "section", "text": _md("Ask your AIDL admin to approve an AI tool or app. You'll get the answer here "
                                        "in Slack.")},
        _input("name", "App name", {"type": "plain_text_input", "action_id": "v", "max_length": 100,
                                    "placeholder": _t("e.g. Perplexity")}),
        _input("website", "Website", {"type": "plain_text_input", "action_id": "v", "max_length": 300,
                                      "placeholder": _t("https://…")}, optional=True),
        _input("purpose", "What would you use it for?", {"type": "plain_text_input", "action_id": "v",
                                                         "multiline": True, "max_length": 1000}),
        _input("data", "What data would you put into it?", {"type": "static_select", "action_id": "v", "options": [
            {"text": _t(label), "value": key} for key, (label, _a) in DATA.items()]}),
    ]}


def _values(view: dict) -> dict:
    out = {}
    for block_id, actions in ((view.get("state") or {}).get("values") or {}).items():
        v = actions.get("v") or {}
        out[block_id] = (v.get("value") or (v.get("selected_option") or {}).get("value") or "").strip()
    return out


def _approvers(org: Organization) -> list[AIDLUser]:
    from .admin_perms import has

    admins = AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True,
                                     microsoft_id__startswith="slack:")
    return [a for a in admins if has(org, a, "approve_apps")]


def submit(member: AIDLUser, org: Organization, view: dict) -> dict:
    """The popup was sent. Returns Slack's response (errors, or clear)."""
    v = _values(view)
    name = " ".join(v.get("name", "").split())
    if not name:
        return {"response_action": "errors", "errors": {"name": "Tell us the app's name."}}
    if v.get("data") not in DATA:
        return {"response_action": "errors", "errors": {"data": "Pick what data you'd put into it."}}
    key = name.lower()
    from .slack_modals import in_background

    approved = RegisteredApp.objects.filter(organization_id=str(org.pk), name__iexact=name, is_active=True,
                                            status=RegisteredApp.Status.APPROVED).first()
    if approved is not None:
        in_background(_dm, org, member, f"✅ *{approved.name}* is already approved at {org.name} — go ahead. "
                                              "Remember the Traffic Light Check before you paste anything.")
        return {"response_action": "clear"}
    req = AppRequest.objects.filter(organization_id=str(org.pk), name_key=key, status=AppRequest.Status.PENDING).first()
    if req is not None:
        ids = json.loads(req.requester_ids or "[]")
        if str(member.pk) not in ids:
            ids.append(str(member.pk))
            req.requester_ids = json.dumps(ids)
            req.save(update_fields=["requester_ids"])
        in_background(_dm, org, member, f"📨 *{req.name}* has already been requested — we've added you to "
                                              "that request. You'll hear back here.")
        in_background(refresh_admin_messages, org, req)
        return {"response_action": "clear"}
    req = AppRequest.objects.create(organization_id=str(org.pk), name=name, name_key=key, website=v.get("website", ""),
                                    purpose=v.get("purpose", ""), data_class=v["data"],
                                    requester_ids=json.dumps([str(member.pk)]))
    in_background(_after_request, org, member, req)
    return {"response_action": "clear"}


def _after_request(org: Organization, member: AIDLUser, req: AppRequest) -> None:
    _dm(org, member, f"📨 Your request for *{req.name}* was sent to your AIDL admin. You'll get the answer here.")
    notify_approvers(org, req)


def _dm(org: Organization, user: AIDLUser, text: str) -> dict:
    uid = _slack_id(user)
    if not uid:
        return {}
    return slack_client.send_dm(slack_client.bot_token(org), org, uid, text=text,
                                blocks=[{"type": "section", "text": _md(text)}])


# ---------- admins ----------

def _requesters(req: AppRequest) -> list[AIDLUser]:
    return list(AIDLUser.objects.filter(pk__in=json.loads(req.requester_ids or "[]")))


def admin_blocks(req: AppRequest) -> list:
    people = _requesters(req)
    names = ", ".join(p.full_name or p.email for p in people) or "an employee"
    label = DATA.get(req.data_class, ("", ""))[0]
    blocks = [
        {"type": "header", "text": _t("🛣 App request")},
        {"type": "section", "text": _md(f"*{req.name}*" + (f"  ·  {req.website}" if req.website else "")
                                        + f"\nRequested by *{names}*"
                                        + (f"  ·  {len(people)} people asked" if len(people) > 1 else ""))},
        {"type": "section", "fields": [_md(f"*Use*\n{req.purpose or '—'}"[:1990]), _md(f"*Data*\n{label}")]},
    ]
    if req.status == AppRequest.Status.PENDING:
        blocks.append({"type": "actions", "block_id": "aidl_appreq", "elements": [
            {"type": "button", "text": _t("✅ Approve"), "style": "primary", "action_id": "aidl_appreq_approve",
             "value": str(req.pk)},
            {"type": "button", "text": _t("❌ Reject"), "style": "danger", "action_id": "aidl_appreq_reject",
             "value": str(req.pk)},
            {"type": "button", "text": _t("💬 Ask for more info"), "action_id": "aidl_appreq_info",
             "value": str(req.pk)},
        ]})
    else:
        verb = "✅ Approved" if req.status == AppRequest.Status.APPROVED else "❌ Rejected"
        when = req.decided_at.strftime("%d %b %Y") if req.decided_at else ""
        blocks.append({"type": "context", "elements": [_md(f"{verb} by *{req.decided_by_name}* · {when}"
                                                           + (f" · _{req.reason}_" if req.reason else ""))]})
    return blocks


def notify_approvers(org: Organization, req: AppRequest) -> None:
    token = slack_client.bot_token(org)
    refs = []
    for admin in _approvers(org):
        res = slack_client.send_dm(token, org, _slack_id(admin), text=f"App request: {req.name}",
                                   blocks=admin_blocks(req))
        if res.get("ok"):
            refs.append({"channel": res.get("channel", ""), "ts": res.get("ts", "")})
    req.admin_messages = json.dumps(refs)
    req.save(update_fields=["admin_messages"])


def refresh_admin_messages(org: Organization, req: AppRequest) -> None:
    token = slack_client.bot_token(org)
    for ref in json.loads(req.admin_messages or "[]"):
        if ref.get("channel") and ref.get("ts"):
            slack_client.slack_api("chat.update", token, json={"channel": ref["channel"], "ts": ref["ts"],
                                                               "text": f"App request: {req.name}",
                                                               "blocks": admin_blocks(req)})


def get(org: Organization, request_id: str) -> AppRequest | None:
    return AppRequest.objects.filter(organization_id=str(org.pk), pk=request_id).first()


def _decide(req: AppRequest, status: str, admin: AIDLUser, reason: str = "") -> None:
    req.status = status
    req.decided_by_name = admin.full_name or admin.email
    req.decided_at = timezone.now()
    req.reason = reason
    req.save(update_fields=["status", "decided_by_name", "decided_at", "reason"])


def approve(org: Organization, req: AppRequest, admin: AIDLUser) -> None:
    from .admin_perms import notify_master

    if req.status != AppRequest.Status.PENDING:
        return
    if not RegisteredApp.objects.filter(organization_id=str(org.pk), name__iexact=req.name, is_active=True).exists():
        RegisteredApp.objects.create(organization_id=str(org.pk), name=req.name, app_type=RegisteredApp.AppType.AI,
                                     status=RegisteredApp.Status.APPROVED, category="Requested by employees",
                                     data_allowed=DATA.get(req.data_class, DATA["green"])[1],
                                     description=req.purpose[:2000])
    else:
        RegisteredApp.objects.filter(organization_id=str(org.pk), name__iexact=req.name).update(
            status=RegisteredApp.Status.APPROVED, is_active=True)
    _decide(req, AppRequest.Status.APPROVED, admin)
    label = DATA.get(req.data_class, DATA["green"])[0]
    for person in _requesters(req):
        _dm(org, person, f"✅ *{req.name}* is approved at {org.name} — you can use it. Data allowed: {label}. "
                         "Keep checking the Traffic Light before you paste anything.")
    refresh_admin_messages(org, req)
    notify_master(org, admin, f"🛣 *{admin.full_name or admin.email}* approved the app *{req.name}*.")


def reject(org: Organization, req: AppRequest, admin: AIDLUser, reason: str) -> None:
    from .admin_perms import notify_master

    if req.status != AppRequest.Status.PENDING:
        return
    _decide(req, AppRequest.Status.REJECTED, admin, reason)
    for person in _requesters(req):
        _dm(org, person, f"❌ *{req.name}* wasn't approved at {org.name}." + (f"\n> {reason}" if reason else "")
            + "\nPlease don't use it for work.")
    refresh_admin_messages(org, req)
    notify_master(org, admin, f"🛣 *{admin.full_name or admin.email}* rejected the app *{req.name}*.")


def ask_info(org: Organization, req: AppRequest, admin: AIDLUser, question: str) -> None:
    for person in _requesters(req):
        _dm(org, person, f"💬 *{admin.full_name or admin.email}* has a question about your request for "
                         f"*{req.name}*:\n> {question}\nReply to them directly in Slack.")


def reason_view(req: AppRequest, kind: str) -> dict:
    """Reject (reason) / Ask for more info (question) popup."""
    reject_ = kind == REJECT
    return {"type": "modal", "callback_id": kind, "private_metadata": json.dumps({"request": str(req.pk)}),
            "title": _t("Reject app" if reject_ else "Ask for more info"),
            "submit": _t("Reject" if reject_ else "Send question"), "close": _t("Cancel"), "blocks": [
        {"type": "section", "text": _md(f"*{req.name}*")},
        _input("text", "Reason (the employee sees it)" if reject_ else "Your question",
               {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 1000},
               optional=reject_),
    ]}


def submit_reason(admin: AIDLUser, org: Organization, view: dict) -> dict:
    from .slack_modals import in_background

    meta = json.loads(view.get("private_metadata") or "{}")
    req = get(org, meta.get("request", ""))
    if req is None:
        return {"response_action": "errors", "errors": {"text": "This request no longer exists."}}
    text = _values(view).get("text", "")
    if view.get("callback_id") == REJECT:
        in_background(reject, org, req, admin, text)
    else:
        if not text:
            return {"response_action": "errors", "errors": {"text": "Type your question."}}
        in_background(ask_info, org, req, admin, text)
    return {"response_action": "clear"}

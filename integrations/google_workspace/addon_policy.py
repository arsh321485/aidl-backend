"""The admin's AI policy, in the Gmail side panel. Same rules and the same api/ modules as the
Slack, Teams and Classroom Admin Centers; only the cards are ours.

    section(user, org)         "Your AI policy" in the Admin Center: the 14 topics, which ones the
                               team agrees to, and the uploaded policy's progress
    topic_card(org, n, a)      one topic: its questions (AIDL's recommended answers already picked),
                               a preview of the card, "Add to our policy"
    upload_offer(event, ...)   on an email with a policy file attached: "Use this file"

The admin chooses which topics go in (Trial: 3 topics, paid plans: all 14). Paid plans can also
use their own policy file: Gmail cards can't pick a file, so the admin opens the email that has it
attached and AIDL reads it from there (google_api.attachment) and turns it into the 14 cards
(api/aup_upload.py, the same as the other platforms).
"""

from __future__ import annotations

from django.http import QueryDict

from api import aup_store, aup_upload
from api import policy_cards as pc
from api.models import AIDLUser, AupUpload, Organization

from . import addon as ui
from . import google_api

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ---------- the Admin Center section ----------

def section(user: AIDLUser, org: Organization) -> list:
    states = pc.statuses(org)
    chosen = [n for n in pc.TOPICS if n in states]
    rows = [ui._text(
        f"Choose the topics your team agrees to: <b>{len(chosen)}</b> chosen so far. Each person agrees to every "
        "chosen card in their Gmail before the checks. AIDL's recommended answers are already picked; "
        "open a topic to check them.")]
    rows += _upload_rows(org)
    for n in pc.ordered(org):
        st = states.get(n)
        title = f"{pc.ICONS[n]} <b>{ui._esc(pc.SHORT[n])}</b>"
        if pc.is_locked(org, n):
            rows.append(ui._row(title, "🔒 Basic and Enterprise plans"))
        elif st is None:
            rows.append(ui._row(title, "Not in your policy", button=ui._button("Add", "policy_open", n=n)))
        elif st["state"] == "scheduled":
            rows.append(ui._row(title, f"✅ In your policy · from {st['send_on']}",
                                button=ui._button("Edit", "policy_open", n=n)))
        else:
            rows.append(ui._row(title, f"✅ In your policy · {st['agreed']} agreed",
                                button=ui._button("Edit", "policy_open", n=n)))
    if aup_upload.can_upload(org):
        rows.append(ui._text("📎 <b>Have your own AI policy?</b> Open the email that has it attached (PDF or Word), "
                             "then click the AIDL icon: AIDL turns it into your policy cards."))
    elif any(pc.is_locked(org, n) for n in pc.TOPICS):
        rows.append(ui._text('<font color="#5E5843">The Trial includes 3 topics. Basic and Enterprise include all '
                             "14 and your own policy file (see Your plan).</font>"))
    return rows


def _upload_rows(org: Organization) -> list:
    """The latest uploaded policy file, while it's being read or waiting for the admin's OK."""
    up = aup_upload.latest(org) if aup_upload.can_upload(org) else None
    if up is None or up.status == AupUpload.Status.PUBLISHED:
        return []
    name = ui._esc(up.file_name)
    if up.status == AupUpload.Status.PROCESSING:
        return [ui._text(f"⏳ <b>Reading {name}</b>. AIDL is turning it into your policy cards (about a minute)."),
                ui._buttons(ui._refresh())]
    if up.status == AupUpload.Status.FAILED:
        return [ui._text(f"⚠️ <b>{name}</b> couldn't be turned into cards: {ui._esc(up.error)}")]
    cards = aup_upload.cards_of(up)
    warn = aup_upload.warnings(cards)
    rows = [ui._text(f"📎 <b>Your policy file is ready: {name}</b><br>{len(cards) - len(warn['not_covered'])} of "
                     f"{len(cards)} topics come from your file"
                     + (f"; {len(warn['not_covered'])} aren't in it, so those cards say to ask your AIDL admin"
                        if warn["not_covered"] else "") + ".")]
    for c in sorted(cards, key=lambda c: c["no"]):
        rows.append(ui._row(f"{pc.ICONS.get(c['no'], '📜')} <b>{ui._esc(c['title'])}</b>",
                            ui._esc(c["headline"]) if c["covered"] else "Not in your file"))
    rows.append(ui._buttons(ui._button("✅ Use these cards as our policy", "policy_publish", upload=up.pk,
                                       filled=True)))
    rows.append(ui._text('<font color="#5E5843">They replace the topics above, for everyone.</font>'))
    return rows


# ---------- one topic ----------

def answers(org: Organization) -> dict:
    return aup_store.answers(org, with_tools=False)


def _selection(name: str, label: str, kind: str, items: list[tuple[str, str, bool]]) -> dict:
    return {"selectionInput": {"name": name, "label": label, "type": kind,
                               "items": [{"text": t, "value": v, "selected": s} for t, v, s in items]}}


def _text_input(name: str, label: str, value: str = "", hint: str = "") -> dict:
    field = {"name": name, "label": label, "value": value or ""}
    if hint:
        field["hintText"] = hint
    return {"textInput": field}


def topic_card(org: Organization, n: int, a: dict, notice: str = "") -> dict:
    """The topic's questions as Gmail inputs (the same field names as the Teams / Classroom forms, so
    api.teams_portal._read_policy reads them), the card's words, and the buttons."""
    from api.teams_portal import _needs_fill

    widgets = []
    for q in pc.questions(n, a["answers"]):
        value = a["answers"].get(q["id"])
        chosen = value if isinstance(value, list) else [value]
        label = q["text"] + (" (pick any)" if q["type"] == "multi" else "")
        if q["type"] == "text":
            widgets.append(_text_input(f"q|{q['id']}", q["text"], value or "", q.get("example") or ""))
            continue
        widgets.append(_selection(f"q|{q['id']}", label, "CHECK_BOX" if q["type"] == "multi" else "RADIO_BUTTON",
                                  [(o["text"] + (" ★" if o.get("recommended") else ""), o["id"], o["id"] in chosen)
                                   for o in q["options"]]))
        for o in q["options"]:
            if o["id"] in chosen and _needs_fill(o):
                widgets.append(_text_input(f"f|{o['id']}", f"{o['text']}: {q.get('fill_label') or 'details'}",
                                           a["fills"].get(o["id"], "")))
        if q["id"] == "7.1" and value == pc.APPROVED_AI:
            tools = aup_store.allowed_tools(org, list(dict.fromkeys(pc.POPULAR_AI_TOOLS + a["tools"])))
            widgets.append(_selection("tools", "Approved AI tools", "CHECK_BOX",
                                      [(t, t, t in a["tools"]) for t in tools]))
            widgets.append(_text_input("paste", "Other approved tools", "", "Comma separated"))
    preview = pc.lines(n, a)
    existing = aup_store.card(org, n)
    done = ui._button("💾 Save changes" if existing else "✅ Add to our policy", "policy_approve", n=n,
                      filled=True, tools_shown=1)
    sections = [
        ui._section(widgets + [ui._buttons(ui._button("↻ Update preview", "policy_preview", n=n, tools_shown=1))],
                    "Your answers (★ = AIDL recommends)"),
        ui._section([ui._text("<br>".join(f"• {ui._esc(t)}" for t in preview) or "Pick your answers above.")],
                    "What your team will read"),
        ui._section([ui._buttons(done, ui._button("← Back", "home"))]),
    ]
    status = "In your policy" if existing else "Not in your policy yet"
    return ui._card(f"{pc.ICONS[n]} {pc.SHORT[n]}", status, sections, notice)


def read_form(org: Organization, n: int, form: dict, params: dict) -> dict:
    """The topic card's inputs as {answers, fills, tools} (via the shared form reader)."""
    from api.teams_portal import _read_policy

    post = QueryDict(mutable=True)
    for name, values in form.items():
        post.setlist(name, values)
    for name in ("tools_shown",):
        if params.get(name):
            post[name] = params[name]
    post["n"] = str(n)
    a, _when, _day = _read_policy(org, post)
    return a


def approve(user: AIDLUser, org: Organization, n: int, a: dict) -> str:
    """"" = approved, else what's missing. Learners see the card the next time they open AIDL (no Slack/Teams
    delivery: deliver=False)."""
    problems = pc.problems(n, a)
    if problems:
        return " ".join(problems.values())
    pc.approve(org, user, n, a, deliver=False)
    return ""


# ---------- your own policy file ----------

def upload_offer(event: dict, user: AIDLUser, org: Organization) -> list:
    """On an email with a policy file attached (admins on a paid plan): "Use this file"."""
    if user.role != AIDLUser.Role.ADMIN or not aup_upload.can_upload(org):
        return []
    try:
        files = google_api.attachments(event)
    except google_api.GoogleApiError:
        return []
    return [ui._section([
        ui._text(f"📎 <b>{ui._esc(f['name'])}</b> is attached to this email. Use it as {ui._esc(org.name)}'s "
                 "AI policy? AIDL turns it into your policy cards, then you check them before anyone sees them."),
        ui._buttons(ui._button("Use this file as our AI policy", "policy_upload", att=f["id"], name=f["name"],
                               filled=True)),
    ], "Your own AI policy") for f in files[:3]]


def upload(event: dict, user: AIDLUser, org: Organization, attachment_id: str, name: str) -> str:
    if user.role != AIDLUser.Role.ADMIN:
        return "Only your AIDL admins can change the AI policy."
    try:
        data = google_api.attachment(event, attachment_id)
    except google_api.GoogleApiError as exc:
        return "Couldn't open that attachment." if str(exc) == "refused" else str(exc)
    if len(data) > MAX_UPLOAD_BYTES:
        return "That file is too big (10 MB at most)."
    try:
        aup_upload.start(org, user, name, data)
    except aup_upload.UploadError as exc:
        return str(exc.message if hasattr(exc, "message") else exc)
    return "⏳ AIDL is reading your policy. In about a minute, press Refresh to check the cards."


def publish(user: AIDLUser, org: Organization, upload_id: str) -> str:
    if user.role != AIDLUser.Role.ADMIN:
        return "Only your AIDL admins can change the AI policy."
    up = AupUpload.objects.filter(pk=upload_id, organization_id=str(org.pk)).first()
    if up is None:
        return "That file isn't there any more. Please use it again."
    try:
        aup_upload.publish(org, up, user)
    except aup_upload.UploadError as exc:
        return str(exc.message if hasattr(exc, "message") else exc)
    return f"✅ Your policy cards now come from {up.file_name}."

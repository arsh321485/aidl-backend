"""Slack screens for an uploaded AUP (aup_upload.py).

    Admin Center step 1 → "Upload your own AUP" (Basic and Enterprise) → popup with a
    file field (callback aidl_aup_upload) → "AIDL is writing your 14 cards"
    → when the model is done, that popup turns into the review (if it's still
    open) and the admin gets a DM with "Review cards" either way.
    Review (callback aidl_aup_upload_review): the 14 cards with ⚠ for points
    the policy doesn't cover and quotes AIDL couldn't find in the file, and
    when they should go out → "Use these cards" approves them as the 14
    policy cards (policy_cards.py) and sends them today or on that day.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings

from . import aup_upload, slack_client
from .models import AIDLUser, AupUpload, Organization

logger = logging.getLogger(__name__)
UPLOAD = "aidl_aup_upload"
REVIEW = "aidl_aup_upload_review"
_LEGAL = ("AI writes these cards from your file. Check each one before you approve it — AIDL doesn't give legal "
          "advice.")


def _text(t: str) -> dict:
    return {"type": "plain_text", "text": t[:150], "emoji": True}


def _md(t: str) -> dict:
    return {"type": "mrkdwn", "text": t[:3000]}


def _modal(callback: str, title: str, blocks: list, *, submit: str = "", close: str = "Close", meta=None) -> dict:
    view = {"type": "modal", "callback_id": callback, "title": _text(title), "close": _text(close),
            "blocks": blocks[:100], "private_metadata": json.dumps(meta or {})}
    if submit:
        view["submit"] = _text(submit)
    return view


def upload_view(org: Organization, meta: dict, notice: str = "") -> dict:
    blocks = [
        {"type": "section", "text": _md(
            f"Already have an AI policy? Upload it and AIDL turns it into the *14 policy cards* your team agrees to, "
            f"one at a time, with *{org.name}'s* logo on every card.")},
        {"type": "context", "elements": [_md(
            "AI writes the cards *only from your file* and quotes the sentence each card is based on. Nothing "
            "reaches your team until you review and approve the cards.")]},
    ]
    if not org.logo_data:
        blocks.append({"type": "context", "elements": [_md(
            "Tip: add your company logo in the Admin Center first — it goes on every card as a faint watermark.")]})
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    blocks.append({"type": "input", "block_id": "file", "label": _text("Your AI policy (PDF, Word or text file)"),
                   "element": {"type": "file_input", "action_id": "v", "filetypes": ["pdf", "docx", "txt", "md"],
                               "max_files": 1}})
    blocks.append({"type": "context", "elements": [_md(_LEGAL)]})
    return _modal(UPLOAD, "Upload your AUP", blocks, submit="Write my cards", meta=meta)


def working_view(file_name: str) -> dict:
    return _modal(REVIEW, "Your AUP cards", [
        {"type": "section", "text": _md(f"⏳ *AIDL is writing your 14 cards* from _{file_name}_.")},
        {"type": "context", "elements": [_md("This usually takes a minute or two. You can close this window — "
                                             "AIDL will message you when the cards are ready to review.")]},
    ])


def _card_block(c: dict) -> list:
    if not c["covered"]:
        return [{"type": "section", "text": _md(f"⚠  *{c['no']} · {c['title']}*\n_Your policy doesn't cover this "
                                                "point. Employees will see that it isn't covered yet._")}]
    lines = [f"*{c['no']} · {c['title']}*" + (f"  —  {c['headline']}" if c["headline"] else "")]
    if c["summary"]:
        lines.append(c["summary"])
    lines += [f"✓ {t}" for t in c["do"]] + [f"✗ {t}" for t in c["dont"]]
    quote = c["source_quote"]
    source = (f"From your policy: “{quote}”" if c["quote_verified"]
              else f"⚠ AIDL couldn't find this quote in your file — check this card: “{quote or 'no quote given'}”")
    return [{"type": "section", "text": _md("\n".join(lines))}, {"type": "context", "elements": [_md(source)]}]


def review_view(org: Organization, upload: AupUpload | None, notice: str = "") -> dict:
    if upload is None:
        return _modal(REVIEW, "Your AUP cards", [{"type": "section", "text": _md(notice or "No uploaded policy yet.")}])
    if upload.status == AupUpload.Status.PROCESSING:
        return working_view(upload.file_name)
    if upload.status == AupUpload.Status.FAILED:
        return _modal(REVIEW, "Your AUP cards", [
            {"type": "section", "text": _md(f"⚠  *AIDL couldn't write the cards* from _{upload.file_name}_.\n"
                                            f"{upload.error or 'Please try again.'}")},
            {"type": "actions", "block_id": "aidl_aup_upload_again", "elements": [
                {"type": "button", "text": _text("Upload a file"), "action_id": "aidl_aup_upload_open",
                 "value": "again", "style": "primary"}]},
        ])
    cards = aup_upload.cards_of(upload)
    warn = aup_upload.warnings(cards)
    published = upload.status == AupUpload.Status.PUBLISHED
    blocks = [
        {"type": "header", "text": _text("Your policy as 14 cards")},
        {"type": "context", "elements": [_md(f"From _{upload.file_name}_ · written by AI · "
                                             + ("in use as your AUP" if published else "not sent to anyone yet"))]},
    ]
    if upload.text_truncated:
        blocks.append({"type": "context", "elements": [_md("⚠ Your file is very long, so only its first part was "
                                                           "used. Check that every card is right.")]})
    if warn["not_covered"]:
        blocks.append({"type": "section", "text": _md(
            f"⚠ *Not in your policy:* points {', '.join(map(str, warn['not_covered']))}. Add them to your policy "
            "and upload it again, or use these cards as they are.")})
    if warn["unverified"]:
        blocks.append({"type": "section", "text": _md(
            f"⚠ *Check these cards:* {', '.join(map(str, warn['unverified']))} — AIDL couldn't match their quote "
            "to your file.")})
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    for c in cards:
        blocks += [{"type": "divider"}, *_card_block(c)]
    blocks += [
        {"type": "actions", "block_id": "aidl_aup_upload_again", "elements": [
            {"type": "button", "text": _text("Upload another file"), "action_id": "aidl_aup_upload_open",
             "value": "again"}]},
    ]
    if not published:
        blocks += [{"type": "divider"},
                   {"type": "input", "block_id": "when", "label": _text("When should the cards go out?"),
                    "element": {"type": "radio_buttons", "action_id": "v",
                                "initial_option": {"text": _text("Today, now"), "value": "today"},
                                "options": [{"text": _text("Today, now"), "value": "today"},
                                            {"text": _text("On a day"), "value": "day",
                                             "description": _text("Pick it below · 10:00 in each person's time zone")}]}},
                   {"type": "input", "block_id": "day", "label": _text("Day (if not today)"), "optional": True,
                    "element": {"type": "datepicker", "action_id": "v"}}]
    blocks.append({"type": "context", "elements": [_md(_LEGAL)]})
    return _modal(REVIEW, "Your AUP cards", blocks, submit="" if published else "Use these cards",
                  meta={"upload_id": str(upload.pk)})


# ---------- submissions ----------

def submit_upload(admin: AIDLUser, org: Organization, view: dict) -> dict:
    from .slack_modals import in_background

    values = (view.get("state") or {}).get("values") or {}
    files = ((values.get("file") or {}).get("v") or {}).get("files") or []
    if not files:
        return {"response_action": "errors", "errors": {"file": "Choose your policy file."}}
    if not aup_upload.can_upload(org):
        return {"response_action": "errors", "errors": {"file": "Uploading your own AUP is part of the "
                                                                "Basic and Enterprise plans."}}
    name = files[0].get("name") or files[0].get("title") or "policy"
    if aup_upload.file_type(name) not in aup_upload.TYPES:
        return {"response_action": "errors", "errors": {"file": "Upload a PDF, Word (.docx) or text file."}}
    if aup_upload.busy(org):
        return {"response_action": "errors", "errors": {"file": "AIDL is still writing cards from your last file."}}
    in_background(_from_slack, org.pk, admin.pk, files[0], view.get("id", ""))
    return {"response_action": "update", "view": working_view(name)}


def _from_slack(org_pk, admin_pk, file_obj: dict, view_id: str) -> None:
    """Download the file from Slack (files:read) and start the generation."""
    import requests

    org = Organization.objects.get(pk=org_pk)
    admin = AIDLUser.objects.get(pk=admin_pk)
    token = slack_client.bot_token(org)
    name = file_obj.get("name") or file_obj.get("title") or "policy"
    try:
        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        if not url:
            info = slack_client.slack_api("files.info", token, params={"file": file_obj.get("id", "")})
            if info.get("error") == "missing_scope":
                raise aup_upload.UploadError("AIDL needs the Slack *files:read* permission. Sign in with Slack "
                                             "again on the AIDL website to grant it, then upload the file again.")
            url = (info.get("file") or {}).get("url_private_download", "")
        if (file_obj.get("size") or 0) > settings.AUP_UPLOAD_MAX_BYTES:
            raise aup_upload.UploadError("That file is too big.")
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        resp.raise_for_status()
        aup_upload.start(org, admin, name, resp.content, slack_view_id=view_id)
    except aup_upload.UploadError as exc:
        _show(org, view_id, upload_view(org, {}, notice=f"⚠ {exc.message}"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("AUP upload download failed: %s", type(exc).__name__)
        _show(org, view_id, upload_view(org, {}, notice="⚠ AIDL couldn't download that file from Slack. "
                                                        "Please try again."))


def _show(org: Organization, view_id: str, view: dict) -> bool:
    if not view_id:
        return False
    result = slack_client.slack_api("views.update", slack_client.bot_token(org), json={"view_id": view_id, "view": view})
    return bool(result.get("ok"))


def submit_review(admin: AIDLUser, org: Organization, view: dict) -> dict:
    from django.utils import timezone

    from .policy_cards import check_day
    from .policy_cards_slack import sent_view

    meta = json.loads(view.get("private_metadata") or "{}")
    upload = AupUpload.objects.filter(pk=meta.get("upload_id"), organization_id=str(org.pk)).first()
    if upload is None:
        return {"response_action": "update", "view": review_view(org, None)}
    values = (view.get("state") or {}).get("values") or {}
    when = (((values.get("when") or {}).get("v") or {}).get("selected_option") or {}).get("value", "today")
    day = ((values.get("day") or {}).get("v") or {}).get("selected_date") or ""
    send_on = ""
    if when == "day":
        why = check_day(day)
        if why:
            return {"response_action": "errors", "errors": {"day": why}}
        send_on = "" if day == timezone.localdate().isoformat() else day
    try:
        aup_upload.publish(org, upload, admin, send_on)
    except aup_upload.UploadError as exc:
        return {"response_action": "update", "view": review_view(org, upload, notice=f"⚠ {exc.message}")}
    return {"view": sent_view(org, 1, meta, uploaded=True), "published": True,
            "notice": f"✅ The 14 policy cards from _{upload.file_name}_ are approved."}


def notify_done(upload: AupUpload) -> None:
    """The model finished: refresh the popup if it's still open, and DM the
    admin who uploaded the file."""
    org = Organization.objects.filter(pk=upload.organization_id).first()
    if org is None:
        return
    _show(org, upload.slack_view_id, review_view(org, upload))
    admin = AIDLUser.objects.filter(pk=upload.uploaded_by_id).first()
    if admin is None or not (admin.microsoft_id or "").startswith("slack:"):
        return
    if upload.status == AupUpload.Status.READY:
        warn = aup_upload.warnings(aup_upload.cards_of(upload))
        extra = sum(map(len, warn.values()))
        text = (f"✓ *Your 14 AUP cards are ready to review* — written from _{upload.file_name}_."
                + (f"\n⚠ {extra} card(s) need a closer look." if extra else ""))
        button = {"type": "button", "text": _text("Review cards"), "action_id": "aidl_aup_upload_review",
                  "value": str(upload.pk), "style": "primary"}
    else:
        text = f"⚠ *AIDL couldn't write cards from _{upload.file_name}_.* {upload.error}"
        button = {"type": "button", "text": _text("Upload a file"), "action_id": "aidl_aup_upload_open",
                  "value": "again"}
    slack_client.send_dm(slack_client.bot_token(org), org, admin.microsoft_id.split(":")[-1],
                         text=text.replace("*", "").replace("_", ""),
                         blocks=[{"type": "section", "text": _md(text)},
                                 {"type": "actions", "block_id": "aidl_aup_upload_dm", "elements": [button]}])

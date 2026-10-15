"""Slack screens for the question-bank AUP (aup_builder.py / aup_store.py).

Admin: "Create AUP" opens one popup that walks through the 15 sections
(Organization Profile + the 14 AUP points). Recommended answers are already
selected, so a section can be accepted with "Next". Questions that don't
apply are hidden as answers change, and text boxes appear for typed details
("(specify)" / "Other"). Every Next saves the draft; the last screen
generates the AUP and shows it with Send / View.

Employee: the AUP tab shows one point at a time with "I accept point n";
the next point appears as soon as the previous one is accepted.
"""

from __future__ import annotations

import json
import re

from . import aup_builder, aup_store
from .models import AIDLUser, Organization

CALLBACK = "aidl_aup_section"
_OPT_MAX = 75
_SPECIFY = re.compile(r"\s*\((?:please\s+)?specify[^)]*\)\s*", re.I)


def _text(t: str) -> dict:
    return {"type": "plain_text", "text": t[:150] if len(t) > 150 else t, "emoji": True}


def _md(t: str) -> dict:
    return {"type": "mrkdwn", "text": t}


def _sections() -> list[dict]:
    return aup_builder.bank()["sections"]


def _option(opt: dict, rich: bool) -> dict:
    """Slack allows 75 characters per option; longer ones continue in the
    description (radio buttons / checkboxes)."""
    label = opt["text"]
    if len(label) <= _OPT_MAX:
        return {"text": _text(label), "value": opt["id"]}
    cut = label[:_OPT_MAX - 1].rsplit(" ", 1)[0]
    o = {"text": _text(cut + "…"), "value": opt["id"]}
    if rich:
        o["description"] = _text(("…" + label[len(cut):].strip())[:_OPT_MAX])
    return o


def _depends_on(qid: str) -> bool:
    return any(q["show_if"].startswith(qid + " ") for q in aup_builder.bank()["questions"])


def _needs_fill(opt: dict) -> bool:
    return bool(_SPECIFY.search(opt["text"])) or "{fill}" in opt.get("wording", "")


def _element(q: dict, value) -> tuple[dict, bool]:
    """(Block Kit element, whether changing it must redraw the screen)."""
    kind, opts = q["type"], q["options"]
    redraw = _depends_on(q["id"]) or any(_needs_fill(o) for o in opts)
    if kind == "single":
        rich = len(opts) <= 10
        el = {"type": "radio_buttons" if rich else "static_select", "action_id": "v",
              "options": [_option(o, rich) for o in opts]}
        if not rich:
            el["placeholder"] = _text("Choose one")
        chosen = next((o for o in opts if o["id"] == value), None)
        if chosen:
            el["initial_option"] = _option(chosen, rich)
        return el, redraw
    if kind == "multi":
        rich = len(opts) <= 10
        el = {"type": "checkboxes" if rich else "multi_static_select", "action_id": "v",
              "options": [_option(o, rich) for o in opts]}
        if not rich:
            el["placeholder"] = _text("Choose all that apply")
        chosen = [o for o in opts if o["id"] in (value or [])]
        if chosen:
            el["initial_options"] = [_option(o, rich) for o in chosen]
        return el, redraw
    if kind == "date":
        el = {"type": "datepicker", "action_id": "v"}
        if value:
            el["initial_date"] = value
        return el, False
    el = {"type": "email_text_input" if kind == "email" else "plain_text_input", "action_id": "v"}
    if q["example"]:
        el["placeholder"] = _text(f"e.g. {q['example']}")
    if value:
        el["initial_value"] = str(value)
    return el, False


def section_view(org: Organization, section: int, answers: dict, fills: dict, meta: dict,
                 notice: str = "") -> dict:
    """One screen of the Create AUP popup."""
    sections = _sections()
    s = next(x for x in sections if x["no"] == section)
    last = section == sections[-1]["no"]
    step = f"{section + 1} of {len(sections)}"
    blocks = [
        {"type": "context", "elements": [_md(f"*Step {step}*  ·  {s['title']}")]},
        {"type": "section", "text": _md(f"*{s['title']}*\n{s['intro']}\n_Recommended answers are already selected — "
                                        "change only what's different for your organization._")},
    ]
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    for q in aup_builder.visible_questions(answers, section):
        value = answers.get(q["id"])
        el, redraw = _element(q, value)
        block = {"type": "input", "block_id": f"q|{q['id']}", "label": _text(q["text"]), "element": el,
                 "optional": not q["required"]}
        if redraw:
            block["dispatch_action"] = True
        if q["help"]:
            block["hint"] = _text(q["help"])
        blocks.append(block)
        chosen = value if isinstance(value, list) else [value]
        for opt in q["options"]:  # a text box for each chosen "(specify)" / "Other" answer
            if opt["id"] in chosen and _needs_fill(opt):
                base = _SPECIFY.sub(" ", opt["text"]).strip()
                label = q["fill_label"] if opt["id"].endswith(".x") or not base else f"{base} — {q['fill_label'] or 'details'}"
                fill_el = {"type": "plain_text_input", "action_id": "v"}
                if fills.get(opt["id"]):
                    fill_el["initial_value"] = fills[opt["id"]]
                if q["example"]:
                    fill_el["placeholder"] = _text(f"e.g. {q['example']}")
                blocks.append({"type": "input", "block_id": f"f|{opt['id']}", "label": _text(label or "Details"),
                               "element": fill_el})
    if section > sections[0]["no"]:
        blocks.append({"type": "actions", "block_id": "aidl_aup_nav", "elements": [
            {"type": "button", "text": _text("← Back"), "action_id": "aidl_aup_back", "value": str(section)}]})
    return {
        "type": "modal", "callback_id": CALLBACK,
        "title": _text(f"AI policy · {step}"),
        "submit": _text("Generate AUP" if last else "Next →"),
        "close": _text("Save & close"),
        "private_metadata": json.dumps({**meta, "section": section}),
        "blocks": blocks[:100],
    }


def read_state(view: dict, answers: dict, fills: dict) -> tuple[dict, dict]:
    """Merge what's on screen into the answers / fills."""
    answers, fills = dict(answers), dict(fills)
    for block_id, actions in (view.get("state") or {}).get("values", {}).items():
        el = actions.get("v") or {}
        kind, _, key = block_id.partition("|")
        if kind not in ("q", "f"):
            continue
        if "selected_options" in el:
            value = [o["value"] for o in el.get("selected_options") or []]
        elif "selected_option" in el:
            value = (el.get("selected_option") or {}).get("value", "")
        elif "selected_date" in el:
            value = el.get("selected_date") or ""
        else:
            value = (el.get("value") or "").strip()
        if kind == "q":
            answers[key] = value
        else:
            fills[key] = value
    return answers, fills


def _meta(view: dict) -> dict:
    try:
        return json.loads(view.get("private_metadata") or "{}")
    except ValueError:
        return {}


def redraw(org: Organization, view: dict) -> dict:
    """An answer changed: save it and redraw the screen (show / hide
    follow-up questions and text boxes)."""
    meta = _meta(view)
    d = aup_store.draft(org)
    answers, fills = read_state(view, d["answers"], d.get("fills") or {})
    aup_store.save_draft(org, answers, fills, meta.get("section", 0))
    return section_view(org, meta.get("section", 0), answers, fills, {k: v for k, v in meta.items() if k != "section"})


def back(org: Organization, view: dict) -> dict:
    meta = _meta(view)
    d = aup_store.draft(org)
    answers, fills = read_state(view, d["answers"], d.get("fills") or {})
    section = max(meta.get("section", 0) - 1, 0)
    aup_store.save_draft(org, answers, fills, section)
    return section_view(org, section, answers, fills, {k: v for k, v in meta.items() if k != "section"})


def submit(org: Organization, admin: AIDLUser, view: dict) -> dict:
    """Next: check this screen, save, then the next screen — or, on the last
    one, generate the AUP and show it."""
    meta = _meta(view)
    section = meta.get("section", 0)
    rest = {k: v for k, v in meta.items() if k != "section"}
    d = aup_store.draft(org)
    answers, fills = read_state(view, d["answers"], d.get("fills") or {})
    on_screen = {q["id"] for q in aup_builder.visible_questions(answers, section)}
    problems = {qid: msg for qid, msg in aup_builder.validate(answers, fills).items() if qid in on_screen}
    if problems:
        errors = {}
        for qid, msg in problems.items():
            q = aup_builder.question(qid)
            value = answers.get(qid)
            missing_fill = next((o["id"] for o in q["options"]
                                 if o["id"] in (value if isinstance(value, list) else [value])
                                 and _needs_fill(o) and not (fills.get(o["id"]) or "").strip()), None)
            errors[f"f|{missing_fill}" if missing_fill and msg.startswith("Please fill") else f"q|{qid}"] = msg
        aup_store.save_draft(org, answers, fills, section)
        return {"response_action": "errors", "errors": errors}

    sections = [s["no"] for s in _sections()]
    if section != sections[-1]:
        nxt = sections[sections.index(section) + 1]
        aup_store.save_draft(org, answers, fills, nxt)
        return {"response_action": "update", "view": section_view(org, nxt, answers, fills, rest)}

    aup_store.save_draft(org, answers, fills, section)
    try:
        aup = aup_store.publish(org, admin)
    except aup_builder.AupAnswersError as exc:  # something earlier is incomplete → go back to it
        first = min(exc.errors, key=lambda qid: (aup_builder.question(qid)["section"], qid))
        sec = aup_builder.question(first)["section"]
        aup_store.save_draft(org, answers, fills, sec)
        return {"response_action": "update", "view": section_view(
            org, sec, answers, fills, rest, notice=f"⚠️ Please complete this section first: {exc.errors[first]}")}
    return {"response_action": "update", "view": report_view(org, aup), "published": True}


# ---------- the generated AUP ----------

def _point_text(point: dict) -> str:
    return "\n".join(f"•  {c['text']}" for c in point["clauses"]) or "_Nothing for this point._"


def _clip(text: str, n: int = 2900) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def aup_blocks(aup: dict) -> list:
    org = aup["organization"]
    blocks = [{"type": "header", "text": _text("📄 AI Acceptable Use Policy")},
              {"type": "context", "elements": [_md(f"*{org['name']}*  ·  14 points")]}]
    for p in aup["points"]:
        blocks += [{"type": "divider"},
                   {"type": "section", "text": _md(_clip(f"*{p['no']} · {p['title']}*\n{_point_text(p)}"))}]
    return blocks


def report_view(org: Organization, aup: dict | None = None, notice: str = "") -> dict:
    """Shown right after the AUP is generated (or changed): Send / View and
    the full text."""
    from .admin_setup import aup_change

    aup = aup or aup_store.current(org)
    change = aup_change(org)
    if change.get("changed"):
        intro = ("✅ *Your AUP has been updated.* Your team still has the previous version "
                 "— send them this one so they can accept it.")
    else:
        intro = "✅ *Your AUP is ready.* Employees will receive it one point at a time and accept each point."
    blocks = [
        {"type": "section", "text": _md(intro)},
        {"type": "actions", "block_id": "aidl_aup_next", "elements": [
            {"type": "button", "text": _text("📤 Send AUP to team"), "action_id": "aidl_send_item", "value": "aup",
             "style": "primary"},
            {"type": "button", "text": _text("📄 View AUP"), "action_id": "aidl_aup_view", "value": "view"},
        ]},
    ]
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    blocks += aup_blocks(aup) if aup else []
    return {"type": "modal", "callback_id": "aidl_policy_done", "title": _text("Your AI policy"),
            "close": _text("Done"), "blocks": blocks[:100]}


def view_modal(org: Organization) -> dict:
    aup = aup_store.current(org)
    blocks = aup_blocks(aup) if aup else [{"type": "section", "text": _md("No AUP yet — use *📝 Create AUP*.")}]
    return {"type": "modal", "callback_id": "aidl_aup_view", "title": _text("Team AUP"), "close": _text("Close"),
            "blocks": blocks[:100]}


# ---------- employee: one point per card ----------

def employee_blocks(member: AIDLUser, org: Organization) -> list:
    aup = aup_store.current(org)
    if aup is None:
        return [{"type": "header", "text": _text("📄 AI Acceptable Use Policy")},
                {"type": "section", "text": _md(f"⏳ *{org.name}* is still setting up its AI policy. "
                                                "It will appear here when it's ready.")}]
    done = aup_store.points_accepted(member, org)
    n = aup_store.next_point(member, org)
    bar = "▰" * len(done) + "▱" * (aup_store.POINTS - len(done))
    if n is None:
        return [{"type": "header", "text": _text("📄 AI Acceptable Use Policy")},
                {"type": "section", "text": _md(f"✅ *You've accepted all {aup_store.POINTS} points* of "
                                                f"{org.name}'s AI Acceptable Use Policy.")},
                {"type": "context", "elements": [_md(bar)]}]
    point = aup["points"][n - 1]
    return [
        {"type": "header", "text": _text(f"📄 AI Acceptable Use Policy — point {n} of {aup_store.POINTS}")},
        {"type": "context", "elements": [_md(f"*{org.name}*  ·  {bar}  {len(done)} of {aup_store.POINTS} accepted")]},
        {"type": "section", "text": _md(_clip(f"*{point['no']} · {point['title']}*\n_{point['intro']}_\n\n"
                                              f"{_point_text(point)}"))},
        {"type": "actions", "block_id": "aidl_aup_ack", "elements": [
            {"type": "button", "text": _text(f"✅ I accept point {n}"), "action_id": "aidl_ack_aup",
             "value": aup_store.point_item(n), "style": "primary"}]},
        {"type": "context", "elements": [_md("Accepting all 14 points, the Highway Code and the Traffic Light Check "
                                             "earns your AI Driving License.")]},
    ]

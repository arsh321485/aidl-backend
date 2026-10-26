"""Slack screens for the question-bank AUP (aup_builder.py / aup_store.py).

Admin ("Build your AI policy", design E:\\AIDL AUP Builder (Slack).html):
one popup walks through the 15 sections (Organization Profile + the 14 AUP
points). Each screen has the brand strip (aup_banner.py: section n of 15,
minutes left, rules written, the progress road), the questions with the
recommended answers already selected, and "What this adds to your policy" —
the policy sentences, rewritten on every answer change (dispatch_action →
views.update). Every Next saves the draft. After the last section comes the
review ("Your policy at a glance": ✓ / ⚠ per section with "Go to section n",
which returns to the review), then "Generate policy" → the ready screen
(numbers, what happens next, Approve AUP / View full policy / Edit answers).

Employee: the AUP tab shows one point at a time with "I accept point n";
the next point appears as soon as the previous one is accepted.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime

from . import aup_banner, aup_builder, aup_store
from .models import AIDLUser, Organization

CALLBACK = "aidl_aup_section"
REVIEW = "aidl_aup_review"
_OPT_MAX = 75
_SPECIFY = re.compile(r"\s*\((?:please\s+)?specify[^)]*\)\s*", re.I)
_SAVED = "✓ Saved after every section · close any time and continue where you left off"
_LEGAL = "AIDL drafts the policy from your answers. Have it checked against your local law. Not legal advice."


def _text(t: str) -> dict:
    return {"type": "plain_text", "text": t[:150] if len(t) > 150 else t, "emoji": True}


def _md(t: str) -> dict:
    return {"type": "mrkdwn", "text": t}


def _sections() -> list[dict]:
    return aup_builder.bank()["sections"]


def _option(opt: dict, rich: bool) -> dict:
    """Slack allows 75 characters per option; longer ones continue in the
    description (radio buttons / checkboxes), which also says "Recommended"."""
    label = opt["text"]
    rest = ""
    if len(label) > _OPT_MAX:
        cut = label[:_OPT_MAX - 1].rsplit(" ", 1)[0]
        rest, label = "…" + label[len(cut):].strip(), cut + "…"
    o = {"text": _text(label), "value": opt["id"]}
    if rich:
        note = " · ".join(x for x in (rest, "Recommended" if opt.get("recommended") else "") if x)
        if len(note) > _OPT_MAX:
            note = note[:_OPT_MAX - 1] + "…"
        if note:
            o["description"] = _text(note)
    return o


def _needs_fill(opt: dict) -> bool:
    return bool(_SPECIFY.search(opt["text"])) or "{fill}" in opt.get("wording", "")


def _element(q: dict, value) -> dict:
    kind, opts = q["type"], q["options"]
    if kind == "single":
        rich = len(opts) <= 10
        el = {"type": "radio_buttons" if rich else "static_select", "action_id": "v",
              "options": [_option(o, rich) for o in opts]}
        if not rich:
            el["placeholder"] = _text("Choose one")
        chosen = next((o for o in opts if o["id"] == value), None)
        if chosen:
            el["initial_option"] = _option(chosen, rich)
        return el
    if kind == "multi":
        rich = len(opts) <= 10
        el = {"type": "checkboxes" if rich else "multi_static_select", "action_id": "v",
              "options": [_option(o, rich) for o in opts]}
        if not rich:
            el["placeholder"] = _text("Choose all that apply")
        chosen = [o for o in opts if o["id"] in (value or [])]
        if chosen:
            el["initial_options"] = [_option(o, rich) for o in chosen]
        return el
    if kind == "date":
        el = {"type": "datepicker", "action_id": "v"}
        if value:
            el["initial_date"] = value
        return el
    el = {"type": "email_text_input" if kind == "email" else "plain_text_input", "action_id": "v"}
    if q["example"]:
        el["placeholder"] = _text(f"e.g. {q['example']}")
    if value:
        el["initial_value"] = str(value)
    return el


# ---------- numbers for the strip, the review and the ready screen ----------

def _clauses(answers: dict, fills: dict) -> list[tuple[int, str]]:
    """(section of the question, policy sentence) for every rule the answers
    write so far — the policy as it stands, gaps and all."""
    try:
        aup = aup_builder.generate(answers, fills, strict=False)
    except Exception:  # noqa: BLE001 — a half-typed answer must not break the screen
        return []
    return [(aup_builder.question(c["qid"])["section"], c["text"]) for p in aup["points"] for c in p["clauses"]]


def _missing(answers: dict, fills: dict) -> dict[int, int]:
    """{section: questions still to answer}."""
    out: dict[int, int] = {}
    for qid in aup_builder.validate(answers, fills):
        sec = aup_builder.question(qid)["section"]
        out[sec] = out.get(sec, 0) + 1
    return out


def _answered(answers: dict) -> int:
    return sum(1 for q in aup_builder.visible_questions(answers) if answers.get(q["id"]) not in (None, "", []))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _date(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d:%b %Y}"


def _image(url: str, alt: str) -> dict:
    return {"type": "image", "image_url": url, "alt_text": alt}


def _where(section: int) -> str:
    return "Goes into points 1 and 14 of your policy" if section == 0 else f"Becomes point {section} of your policy"


# ---------- one section ----------

def section_view(org: Organization, section: int, answers: dict, fills: dict, meta: dict,
                 notice: str = "") -> dict:
    """One screen of the policy builder."""
    sections = _sections()
    s = next(x for x in sections if x["no"] == section)
    index = [x["no"] for x in sections].index(section)
    total = len(sections)
    from_review = bool(meta.get("from_review"))
    clauses = _clauses(answers, fills)

    if from_review:
        left = _missing(answers, fills).get(section, 0)
        label = f"Section {index + 1} of {total} · " + (f"{_plural(left, 'question')} left" if left else "from review")
        right = "From review"
    else:
        after = total - index - 1
        label = f"Section {index + 1} of {total} · " + (
            f"about {max(1, round(after * 0.6))} min left" if after else "last section")
        right = f"{_plural(sum(1 for sec, _ in clauses if sec < section), 'rule')} written"
    blocks = [
        _image(aup_banner.url(label, right, org.name, index, total), f"AI policy · {label}"),
        {"type": "header", "text": _text(s["title"])},
        {"type": "context", "elements": [_md(f"{_where(section)} · recommended answers are already selected")]},
    ]
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    for q in aup_builder.visible_questions(answers, section):
        value = answers.get(q["id"])
        # Every answer redraws the screen: follow-up questions, text boxes and
        # "What this adds to your policy" change with it.
        block = {"type": "input", "block_id": f"q|{q['id']}", "label": _text(q["text"]),
                 "element": _element(q, value), "optional": not q["required"], "dispatch_action": True}
        if q["help"]:
            block["hint"] = _text(q["help"])
        blocks.append(block)
        chosen = value if isinstance(value, list) else [value]
        for opt in q["options"]:  # a text box for each chosen "(specify)" / "Other" answer
            if opt["id"] in chosen and _needs_fill(opt):
                base = _SPECIFY.sub(" ", opt["text"]).strip()
                label_ = q["fill_label"] if opt["id"].endswith(".x") or not base else f"{base} — {q['fill_label'] or 'details'}"
                fill_el = {"type": "plain_text_input", "action_id": "v"}
                if fills.get(opt["id"]):
                    fill_el["initial_value"] = fills[opt["id"]]
                if q["example"]:
                    fill_el["placeholder"] = _text(f"e.g. {q['example']}")
                blocks.append({"type": "input", "block_id": f"f|{opt['id']}", "label": _text(label_ or "Details"),
                               "element": fill_el, "dispatch_action": True})

    lines = [text for sec, text in clauses if sec == section]
    preview = "\n".join(f"> {t}" for t in lines) if lines else "_Nothing yet — answer the questions above._"
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": _md(_clip(f"*What this adds to your policy*\n{preview}"))},
        {"type": "context", "elements": [_md(_SAVED)]},
    ]
    if from_review or index > 0:
        blocks.append({"type": "actions", "block_id": "aidl_aup_nav", "elements": [
            {"type": "button", "text": _text("← Back to review" if from_review else "← Back"),
             "action_id": "aidl_aup_back", "value": str(section)}]})
    last = index == total - 1
    return {
        "type": "modal", "callback_id": CALLBACK,
        "title": _text(f"AI policy · {index + 1} of {total}"),
        "submit": _text("Save and return →" if from_review else "Review →" if last else "Next →"),
        "close": _text("Save and exit"),
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


def _rest(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k != "section"}


def redraw(org: Organization, view: dict) -> dict:
    """An answer changed: save it and redraw the screen (follow-up questions,
    text boxes, the policy sentences)."""
    meta = _meta(view)
    d = aup_store.draft(org)
    answers, fills = read_state(view, d["answers"], d.get("fills") or {})
    aup_store.save_draft(org, answers, fills, meta.get("section", 0))
    return section_view(org, meta.get("section", 0), answers, fills, _rest(meta))


def back(org: Organization, view: dict) -> dict:
    """← Back: the previous section, or the review the section came from."""
    meta = _meta(view)
    d = aup_store.draft(org)
    answers, fills = d["answers"], d.get("fills") or {}
    if view.get("callback_id") == CALLBACK:
        answers, fills = read_state(view, answers, fills)
    if meta.get("from_review"):
        aup_store.save_draft(org, answers, fills, meta.get("section", 0))
        return review_view(org, answers, fills, _clean(meta))
    sections = [s["no"] for s in _sections()]
    current = meta.get("section", sections[-1])
    if view.get("callback_id") == REVIEW:  # review → the last section
        section = sections[-1]
    else:
        section = sections[max(sections.index(current) - 1, 0)]
    aup_store.save_draft(org, answers, fills, section)
    return section_view(org, section, answers, fills, _clean(meta))


def _clean(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k not in ("section", "from_review", "review")}


def goto(org: Organization, view: dict, section: int) -> dict:
    """Go to section n (from the review): its Save returns to the review."""
    d = aup_store.draft(org)
    if section not in [s["no"] for s in _sections()]:
        section = d.get("section", 0)
    aup_store.save_draft(org, d["answers"], d.get("fills") or {}, section)
    return section_view(org, section, d["answers"], d.get("fills") or {}, {**_clean(_meta(view)), "from_review": True})


def edit(org: Organization, view: dict) -> dict:
    """Edit answers (ready screen): the builder again, from section 1."""
    d = aup_store.draft(org)
    first = _sections()[0]["no"]
    aup_store.save_draft(org, d["answers"], d.get("fills") or {}, first)
    return section_view(org, first, d["answers"], d.get("fills") or {}, _clean(_meta(view)))


def submit(org: Organization, admin: AIDLUser, view: dict) -> dict:
    """Next: check this screen, save, then the next screen — after the last
    one (or when the section was opened from the review), the review."""
    meta = _meta(view)
    section = meta.get("section", 0)
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
    if meta.get("from_review") or section == sections[-1]:
        aup_store.save_draft(org, answers, fills, section)
        return {"response_action": "update", "view": review_view(org, answers, fills, _clean(meta))}
    nxt = sections[sections.index(section) + 1]
    aup_store.save_draft(org, answers, fills, nxt)
    return {"response_action": "update", "view": section_view(org, nxt, answers, fills, _rest(meta))}


# ---------- review: the policy at a glance ----------

def review_view(org: Organization, answers: dict, fills: dict, meta: dict, notice: str = "") -> dict:
    sections = _sections()
    total = len(sections)
    missing = _missing(answers, fills)
    clauses = _clauses(answers, fills)
    done = total - len(missing)
    left = sum(missing.values())
    label = f"Review · {_plural(left, 'question')} left" if left else "Review · all sections done"
    blocks = [
        _image(aup_banner.url(label, f"{done} of {total}", org.name, done, total), f"AI policy · {label}"),
        {"type": "header", "text": _text("Your policy at a glance")},
        {"type": "context", "elements": [_md(f"14 points · {_plural(len(clauses), 'rule')} written · "
                                             f"{_plural(_answered(answers), 'answer')}")]},
    ]
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    for i, s in enumerate(sections):
        n = s["no"]
        name = s["title"] if n == 0 else f"{n} · {s['title']}"
        if missing.get(n):
            text = f"⚠  *{name}*\n{_plural(missing[n], 'question')} left"
            button = {"type": "button", "text": _text(f"Go to section {i + 1}"), "action_id": "aidl_aup_goto",
                      "value": str(n), "style": "primary"}
        else:
            rules = sum(1 for sec, _ in clauses if sec == n)
            text = f"✓  *{name}*\n" + ("Your details for points 1 and 14" if n == 0
                                        else f"{_plural(rules, 'rule')} written")
            button = {"type": "button", "text": _text("Edit"), "action_id": "aidl_aup_goto", "value": str(n)}
        blocks.append({"type": "section", "block_id": f"rv|{n}", "text": _md(text), "accessory": button})
    blocks += [
        {"type": "actions", "block_id": "aidl_aup_nav", "elements": [
            {"type": "button", "text": _text("← Back"), "action_id": "aidl_aup_back", "value": "review"}]},
        {"type": "context", "elements": [_md(_SAVED)]},
    ]
    return {"type": "modal", "callback_id": REVIEW, "title": _text("AI policy · review"),
            "submit": _text("Generate policy"), "close": _text("Save and exit"),
            "private_metadata": json.dumps({**_clean(meta), "review": True}), "blocks": blocks[:100]}


def generate(org: Organization, admin: AIDLUser, view: dict) -> dict:
    """Generate policy (review screen): publish the draft and show it — or
    say which section still needs an answer."""
    meta = _meta(view)
    d = aup_store.draft(org)
    try:
        aup = aup_store.publish(org, admin)
    except aup_builder.AupAnswersError as exc:
        first = min(exc.errors, key=lambda qid: (aup_builder.question(qid)["section"], qid))
        sec = aup_builder.question(first)["section"]
        n = [s["no"] for s in _sections()].index(sec) + 1
        return {"response_action": "update", "view": review_view(
            org, d["answers"], d.get("fills") or {}, meta,
            notice=f"⚠  Answer section {n} first: use *Go to section {n}*.")}
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


def _minutes(started: str, ended: str) -> int | None:
    try:
        secs = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(1, math.ceil(secs / 60)) if secs >= 0 else None


def report_view(org: Organization, aup: dict | None = None, notice: str = "") -> dict:
    """The policy is ready (or approved): what was built, in numbers, what
    happens next, and Approve AUP / View full policy / Edit answers."""
    from .admin_setup import _slack_learners, aup_change, started
    from .card_approval import is_approved

    aup = aup or aup_store.current(org)
    info = aup_store.published_info(org)
    people = _slack_learners(org).count()
    rules = sum(len(p["clauses"]) for p in aup["points"]) if aup else 0
    published = _date(info["published_at"])
    version = f"v{info['number']}" + (f" · {published}" if published else "")
    change = aup_change(org)
    approved = is_approved(org, "aup")

    view_button = {"type": "button", "text": _text("View full policy"), "action_id": "aidl_aup_view", "value": "view"}
    edit_button = {"type": "button", "text": _text("Edit answers"), "action_id": "aidl_aup_edit", "value": "edit"}
    for_whom = f"For {people} people" if people > 1 else "For 1 person" if people == 1 else "For your team"
    if not approved:
        head, state = "Your AI policy is ready", "Policy ready"
        minutes = _minutes(info["started_at"], info["published_at"])
        built = f"Built from {_plural(_answered(info['answers']), 'answer')}" + (
            f" in {_plural(minutes, 'minute')}" if minutes else "")
        lead = f"{built}. Nothing is sent to your team until you approve it."
        buttons = [{"type": "button", "text": _text("Approve AUP"), "action_id": "aidl_crv_approve_onb",
                    "value": "aup", "style": "primary"}, view_button, edit_button]
    elif change.get("changed") and started(org):
        head, state = "Your updated AI policy is approved", "Policy approved"
        lead = "Your team still has the previous version. Send them this one so they can agree to it."
        buttons = [{"type": "button", "text": _text("Send new version"), "action_id": "aidl_send_item",
                    "value": "aup", "style": "primary"}, view_button, edit_button]
    else:
        head, state = "Your AI policy is approved", "Policy approved"
        lead = "Next: approve the cards and start the programme from the Admin Center (steps 4 and 5)."
        buttons = [view_button, edit_button]

    tools = str(info["answers"].get("7.2") or "").strip() or "—"
    who = f"Each of your {people} people agrees" if people > 1 else "Each person in your team agrees"
    blocks = [
        _image(aup_banner.url(f"{state} · {version}", for_whom, org.name, 15, 15), f"{state} · {version}"),
        {"type": "header", "text": _text(head)},
        {"type": "context", "elements": [_md(lead)]},
    ]
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    blocks += [
        {"type": "section", "fields": [
            _md(f"*Policy points*\n{aup_store.POINTS}"), _md(f"*Rules written*\n{rules}"),
            _md(f"*Approved AI tools*\n{_clip(tools, 1900)}"), _md(f"*Version*\n{version}")]},
        {"type": "section", "text": _md(
            "*What happens after you approve*\n"
            f"1. {who} to the 14 points, one at a time.\n"
            "2. AIDL keeps a dated record of who agreed to which version.\n"
            "3. Change the policy any time; a new version asks everyone to agree again.")},
        {"type": "actions", "block_id": "aidl_aup_next", "elements": buttons},
        {"type": "context", "elements": [_md(_LEGAL)]},
    ]
    return {"type": "modal", "callback_id": "aidl_policy_done", "title": _text("Your AI policy"),
            "close": _text("Done"), "blocks": blocks[:100]}


def view_modal(org: Organization) -> dict:
    aup = aup_store.current(org)
    blocks = aup_blocks(aup) if aup else [{"type": "section", "text": _md("No AI policy yet — use *Build my AI policy →* in the Admin Center.")}]
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

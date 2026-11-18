"""Slack screens for policy cards (policy_cards.py) — design
E:\\AIDL Policy Cards (Slack).html.

    Admin Center step 1 "📌 Add a policy card"
      → About your company (once: countries + industry → the Regulator card DM)
      → Pick a topic (14 topics: sent · n agreed / goes out <day> / Choose)
      → the topic's card: its questions (answers already picked), the AI
        tools picker on "AI tools at work", "Your team will see" (redrawn on
        every change), When: today / pick a day → Approve & send
      → Sent ("📌 Add another card")

Callbacks: aidl_policy_topics, aidl_policy_card, aidl_policy_sent,
aidl_company, aidl_regulators_view. Block ids on the card: q|<qid>, f|<option id>,
tools_<rev>, paste_<rev>, when, day.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from django.utils import timezone

from . import aup_banner, aup_store, policy_cards as pc, regulators
from .models import AIDLUser, Organization

TOPICS = "aidl_policy_topics"
CARD = "aidl_policy_card"
SENT = "aidl_policy_sent"
COMPANY = "aidl_company"
REGULATORS = "aidl_regulators_view"
_OPT_MAX = 75
_SPECIFY = re.compile(r"\s*\((?:please\s+)?specify[^)]*\)\s*", re.I)


def _text(t: str, n: int = 150) -> dict:
    return {"type": "plain_text", "text": t[:n], "emoji": True}


def _md(t: str) -> dict:
    return {"type": "mrkdwn", "text": t[:3000]}


def _ctx(t: str) -> dict:
    return {"type": "context", "elements": [_md(t)]}


def _modal(callback: str, title: str, blocks: list, *, submit: str = "", close: str = "Close", meta=None) -> dict:
    view = {"type": "modal", "callback_id": callback, "title": _text(title, 24), "close": _text(close, 24),
            "blocks": blocks[:100], "private_metadata": json.dumps(meta or {})}
    if submit:
        view["submit"] = _text(submit, 24)
    return view


def _meta(view: dict) -> dict:
    try:
        return json.loads(view.get("private_metadata") or "{}")
    except ValueError:
        return {}


def _keep(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k == "channel_id"}


def _banner(org: Organization, label: str, at: int | None = None) -> dict:
    """The brand strip: yellow = the topics already sent, at their own positions; outlined = the topic on
    screen (`at` = point number). Paid plans: 14 positions. Trial: only the topics it includes (3), so nobody
    thinks there are 14 cards to approve."""
    cards = sorted(aup_store.cards(org))
    if _trial(org):
        avail = pc.available(org)
        done = [avail.index(n) + 1 for n in cards if n in avail]
        return {"type": "image", "alt_text": "AI policy",
                "image_url": aup_banner.url(label, f"{len(done)} of {len(avail)} sent", org.name,
                                            avail.index(at) if at in avail else len(avail), len(avail), done=done)}
    return {"type": "image", "alt_text": "AI policy",
            "image_url": aup_banner.url(label, f"{len(cards)} of 14 sent", org.name,
                                        (at - 1) if at else 14, 14, done=cards)}


def _trial(org: Organization) -> bool:
    return any(pc.is_locked(org, n) for n in pc.TOPICS)


def _tag(org: Organization, n: int) -> str:
    """"point 5" for paid plans; nothing on the Trial (3 topics, numbered by point, would read "point 8 of 3")."""
    return "" if _trial(org) else f"  ·  _point {n}_"


# ---------- topics ----------

def topics_view(org: Organization, meta: dict, notice: str = "") -> dict:
    from .aup_upload import can_upload

    blocks = [_banner(org, "Pick one topic · about 1 minute"),
              _ctx("Your Trial includes 3 policy topics. Choose the ones relevant to your organisation and send them to your team for acknowledgement."
                   if _trial(org) else
                   "Each topic is one short card your team agrees to. Send as many as you like, in any order.")]
    if notice:
        blocks.append(_ctx(notice))
    answers = aup_store.answers(org, with_tools=False)["answers"]  # once, not per topic
    states = pc.statuses(org)
    order = pc.ordered(org)
    trial = any(pc.is_locked(org, n) for n in order)
    if trial:
        blocks.append({"type": "section", "text": _md("*✅ Included in your Trial*")})
    shown_paid = False
    for n in order:
        if pc.is_locked(org, n):
            if not shown_paid:
                shown_paid = True
                blocks += [{"type": "divider"},
                           {"type": "section", "text": _md("*🔒 Unlock with a paid plan*  ·  11 more topics\n"
                                                           "_Your Trial covers the 3 essentials. Upgrade to give your team the full AI policy._")}]
            blocks.append({"type": "section", "block_id": f"topic_{n}",
                           "text": _md(f"🔒 {pc.ICONS[n]} *{pc.SHORT[n]}*{_tag(org, n)}\n⭐ _Paid plan_"),
                           "accessory": {"type": "button", "text": _text("🔒 Upgrade to unlock"),
                                         "action_id": "aidl_pc_locked", "value": str(n)}})
            continue
        st = states.get(n) or {"state": "new"}
        if st["state"] == "sent":
            line, label = f"✅ Sent · {st['agreed']} agreed", "Edit"
        elif st["state"] == "scheduled":
            line, label = f"🗓 Goes out {_day(st['send_on'])}", "Edit"
        else:
            qs = len(pc.questions(n, answers)) + (1 if n == 7 else 0)
            line, label = f"{qs} question{'s' if qs != 1 else ''}", "Choose"
        if trial and n in pc.FREE_TOPICS:
            line += "  ·  🆓 _Free in your Trial_"
        btn = {"type": "button", "text": _text(label), "action_id": "aidl_pc_pick", "value": str(n)}
        if label == "Choose":
            btn["style"] = "primary"
        blocks.append({"type": "section", "block_id": f"topic_{n}",
                       "text": _md(f"{pc.ICONS[n]} *{pc.SHORT[n]}*{_tag(org, n)}\n{line}"), "accessory": btn})
    extra = [{"type": "button", "text": _text("🏛 Your regulators"), "action_id": "aidl_regulators_open",
              "value": "regulators"}]
    if can_upload(org):
        extra.insert(0, {"type": "button", "text": _text("📤 Upload your own AUP instead"),
                         "action_id": "aidl_aup_upload_open", "value": "upload"})
    blocks += [{"type": "divider"}, {"type": "actions", "block_id": "aidl_pc_more", "elements": extra}]
    return _modal(TOPICS, "Add a policy card", blocks, meta=_keep(meta))


def _total(org: Organization) -> int:
    """Topics this organization can use: 3 on the Trial, 14 on a paid plan."""
    return len(pc.available(org))


def _day(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    return f"{d:%a} {d.day} {d:%b}"


# ---------- paid topics (Trial) ----------

PERKS = ("All 14 policy topics, not just 3",
         "Upload your own AUP and let AIDL turn it into cards",
         "Your company logo on every card and licence",
         "Up to 208 awareness cards a year, on a schedule you choose",
         "Licences valid for a full year, up to your plan's users")


def locked_view(org: Organization, n: int, meta: dict, url: str = "") -> dict:
    """The popup a Trial admin gets from a paid topic: what it is, what a paid plan adds, one button."""
    blocks = [{"type": "header", "text": _text(f"🔒 {pc.SHORT[n]} is on the paid plans")},
              _md_section(f"{pc.ICONS[n]} *{pc.SHORT[n]}* is one of the *11 extra policy topics* that come with Basic and "
                          "Enterprise. Your Trial includes *Passwords & Sign-in*, *Email, Chat & Social Media* and "
                          "*Data Classification*."),
              {"type": "divider"},
              _md_section("*⭐ What you unlock*\n" + "\n".join(f"•  {p}" for p in PERKS))]
    if url:
        blocks.append({"type": "actions", "block_id": "aidl_pc_upsell", "elements": [
            {"type": "button", "text": _text("⬆️ See plans & upgrade"), "style": "primary", "url": url,
             "action_id": "aidl_link_plans"},
            {"type": "button", "text": _text("← Back to topics"), "action_id": "aidl_pc_back", "value": "topics"}]})
    else:
        blocks += [_ctx("Only the master admin, or an admin allowed to manage the plan, can upgrade. Ask them to open "
                        "*⬆️ Upgrade plan* in the Admin Center."),
                   {"type": "actions", "block_id": "aidl_pc_upsell", "elements": [
                       {"type": "button", "text": _text("← Back to topics"), "action_id": "aidl_pc_back",
                        "value": "topics"}]}]
    return _modal(TOPICS, "Paid plan topic", blocks, meta=_keep(meta))


def _md_section(text: str) -> dict:
    return {"type": "section", "text": _md(text)}


# ---------- one card ----------

def _option(opt: dict, rich: bool) -> dict:
    """Slack allows 75 characters; longer text continues in the description.
    No "Recommended" label — the recommended answers are simply picked."""
    label = opt["text"]
    o = {"text": _text(label), "value": opt["id"]}
    if len(label) > _OPT_MAX:
        cut = label[:_OPT_MAX - 1].rsplit(" ", 1)[0]
        o["text"] = _text(cut + "…")
        if rich:
            o["description"] = _text(("…" + label[len(cut):].strip())[:_OPT_MAX])
    return o


def _needs_fill(opt: dict) -> bool:
    return bool(_SPECIFY.search(opt["text"])) or "{fill}" in opt.get("wording", "") or opt["id"].endswith(".x")


def _element(q: dict, value) -> dict:
    kind, opts = q["type"], q["options"]
    if kind in ("single", "multi"):
        rich = len(opts) <= 10
        multi = kind == "multi"
        el = {"type": ("checkboxes" if multi else "radio_buttons") if rich else
              ("multi_static_select" if multi else "static_select"),
              "action_id": "v", "options": [_option(o, rich) for o in opts]}
        if not rich:
            el["placeholder"] = _text("Choose all that apply" if multi else "Choose one")
        chosen = [o for o in opts if o["id"] in (value if multi else [value]) and o["id"]] if value else []
        if chosen:
            if multi:
                el["initial_options"] = [_option(o, rich) for o in chosen]
            else:
                el["initial_option"] = _option(chosen[0], rich)
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


def _tools_options(org: Organization, chosen: list[str]) -> list[dict]:
    apps = aup_store._starting_tools(org)  # noqa: SLF001
    groups, seen = [], set()
    for label, names in (("In your AI apps list", apps), ("Popular AI tools", aup_store.allowed_tools(org, pc.POPULAR_AI_TOOLS)),
                         ("Added by you", chosen)):
        opts = []
        for name in names:
            if name and name not in seen:
                seen.add(name)
                opts.append({"text": _text(name, 75), "value": name[:150]})
        if opts:
            groups.append({"label": _text(label, 75), "options": opts[:100]})
    return groups


def card_view(org: Organization, n: int, a: dict, meta: dict, *, when: str = "today", day: str = "",
              rev: int = 0, notice: str = "") -> dict:
    existing = aup_store.card(org, n)
    blocks = [_banner(org, f"{pc.SHORT[n]} · " + (f"topic {pc.available(org).index(n) + 1} of {_total(org)}" if _trial(org) and n in pc.available(org) else f"point {n} of 14"), at=n),
              {"type": "header", "text": _text(f"{pc.ICONS[n]} {pc.SHORT[n]}")}]
    if existing:
        blocks.append(_ctx("✏️ Already sent. Approving sends a new version that everyone agrees to again."))
    if notice:
        blocks.append(_ctx(notice))
    answers, fills = a["answers"], a["fills"]
    for q in pc.questions(n, answers):
        value = answers.get(q["id"])
        block = {"type": "input", "block_id": f"q|{q['id']}", "label": _text(q["text"], 2000),
                 "element": _element(q, value), "optional": not q["required"], "dispatch_action": True}
        if q["help"]:
            block["hint"] = _text(q["help"])
        blocks.append(block)
        chosen = value if isinstance(value, list) else [value]
        for opt in q["options"]:
            if opt["id"] in chosen and _needs_fill(opt):
                own = opt["id"].endswith(".x") or opt["text"].lower().startswith("other")
                base = _SPECIFY.sub(" ", opt["text"]).strip()
                label = "Type your own" if own else f"{base} — {q['fill_label'] or 'details'}"
                el = {"type": "plain_text_input", "action_id": "v"}
                if fills.get(opt["id"]):
                    el["initial_value"] = fills[opt["id"]]
                blocks.append({"type": "input", "block_id": f"f|{opt['id']}", "label": _text(label),
                               "element": el, "dispatch_action": True})
        if q["id"] == "7.1" and value == pc.APPROVED_AI:
            tools = {"type": "multi_static_select", "action_id": "v", "placeholder": _text("Search AI tools…"),
                     "option_groups": _tools_options(org, a["tools"])}
            if a["tools"]:
                tools["initial_options"] = [{"text": _text(t, 75), "value": t[:150]} for t in a["tools"]]
            blocks += [
                {"type": "input", "block_id": f"tools_{rev}", "label": _text("Your approved AI tools"),
                 "element": tools, "dispatch_action": True, "optional": True,
                 "hint": _text("From your AI apps list and popular tools — or paste a list below.")},
                {"type": "input", "block_id": f"paste_{rev}", "label": _text("Or paste a list"), "optional": True,
                 "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                             "placeholder": _text("One per line, or separated by commas")}},
                {"type": "actions", "block_id": f"aidl_pc_paste_{rev}", "elements": [
                    {"type": "button", "text": _text("➕ Add pasted tools"), "action_id": "aidl_pc_paste",
                     "value": "paste"}]},
            ]
    lines = pc.lines(n, a)
    preview = "\n".join(f"> {t}" for t in lines) or "_Answer the questions to write this card._"
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": _md(f"*Your team will see*\n{preview}")},
        _ctx("Each person presses *I agree*. It counts towards their AI Driving License."),
        {"type": "divider"},
        {"type": "input", "block_id": "when", "label": _text("When should it go out?"), "dispatch_action": True,
         "element": {"type": "radio_buttons", "action_id": "v", "options": [
             {"text": _text("Send now"), "value": "today"},
             {"text": _text("Pick a day"), "value": "day",
              "description": _text("10:00 in each person's time zone")}],
             "initial_option": {"text": _text("Send now"), "value": "today"} if when == "today" else
             {"text": _text("Pick a day"), "value": "day", "description": _text("10:00 in each person's time zone")}}},
    ]
    if when == "day":
        blocks.append({"type": "input", "block_id": "day", "label": _text("Day"),
                       "element": {"type": "datepicker", "action_id": "v",
                                   "initial_date": day or (timezone.localdate() + timedelta(days=1)).isoformat()}})
    blocks.append({"type": "actions", "block_id": "aidl_pc_nav", "elements": [
        {"type": "button", "text": _text("← Topics"), "action_id": "aidl_pc_back", "value": "topics"}]})
    return _modal(CARD, "Policy card", blocks, submit="Approve & send" if when == "today" else "Approve & schedule",
                  close="Cancel", meta={**_keep(meta), "no": n, "when": when, "rev": rev})


def read_state(org: Organization, view: dict) -> tuple[dict, str, str, list[str]]:
    """(answers {answers, fills, tools}, when, day, pasted tools) from the
    screen, on top of what's saved."""
    a = aup_store.answers(org)
    answers, fills, tools = dict(a["answers"]), dict(a["fills"]), list(a["tools"])
    when, day, pasted = _meta(view).get("when", "today"), "", []
    for block_id, actions in ((view.get("state") or {}).get("values") or {}).items():
        el = actions.get("v") or {}
        if "selected_options" in el:
            value = [o["value"] for o in el.get("selected_options") or []]
        elif "selected_option" in el:
            value = (el.get("selected_option") or {}).get("value", "")
        elif "selected_date" in el:
            value = el.get("selected_date") or ""
        else:
            value = (el.get("value") or "").strip()
        kind, _, key = block_id.partition("|")
        if kind == "q":
            answers[key] = value
        elif kind == "f":
            fills[key] = value
        elif block_id.startswith("tools_"):
            tools = value
        elif block_id.startswith("paste_"):
            pasted = [t.strip() for t in re.split(r"[\n,;]+", value or "") if t.strip()]
        elif block_id == "when":
            when = value or "today"
        elif block_id == "day":
            day = value
    return ({"answers": answers, "fills": fills, "tools": aup_store.allowed_tools(org, tools)}, when, day,
            aup_store.allowed_tools(org, pasted))


def _merge(tools: list[str], more: list[str]) -> list[str]:
    out = list(tools)
    for t in more:
        if t not in out:
            out.append(t[:150])
    return out


def redraw(org: Organization, view: dict, *, paste: bool = False) -> dict:
    """An answer changed (or "Add pasted tools"): save and redraw."""
    meta = _meta(view)
    a, when, day, pasted = read_state(org, view)
    rev = int(meta.get("rev") or 0)
    if paste and pasted:
        a["tools"], rev = _merge(a["tools"], pasted), rev + 1
    aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
    return card_view(org, int(meta.get("no") or 1), a, meta, when=when, day=day, rev=rev)


def submit_card(admin: AIDLUser, org: Organization, view: dict) -> dict:
    meta = _meta(view)
    n = int(meta.get("no") or 1)
    if pc.is_locked(org, n):  # a paid topic on the Trial: never approved from here, whatever the popup says
        return {"response_action": "update", "view": locked_view(org, n, meta)}
    a, when, day, pasted = read_state(org, view)
    a["tools"] = _merge(a["tools"], pasted)
    rev = int(meta.get("rev") or 0)
    errors = {}
    for qid, msg in pc.problems(n, a).items():
        if qid == "tools":
            errors[f"tools_{rev}"] = msg
            continue
        q = next(x for x in pc.questions(n, a["answers"]) if x["id"] == qid)
        value = a["answers"].get(qid)
        missing = next((o["id"] for o in q["options"] if o["id"] in (value if isinstance(value, list) else [value])
                        and _needs_fill(o) and not (a["fills"].get(o["id"]) or "").strip()), None)
        errors[f"f|{missing}" if missing and msg.startswith("Please fill") else f"q|{qid}"] = msg
    send_on = ""
    if when == "day":
        why = pc.check_day(day)
        if why:
            errors["day"] = why
        elif day != timezone.localdate().isoformat():
            send_on = day
    if errors:
        aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
        return {"response_action": "errors", "errors": errors}
    pc.approve(org, admin, n, a, send_on)
    return {"view": sent_view(org, n, meta), "published": True,
            "notice": f"✅ Policy card *{pc.SHORT[n]}* approved."}


def sent_view(org: Organization, n: int, meta: dict, uploaded: bool = False) -> dict:
    from .admin_setup import _slack_learners, started

    c = aup_store.card(org, n) or {}
    people = _slack_learners(org).count()
    name = "All 14 policy cards from your file" if uploaded else pc.SHORT[n]
    verb = "are" if uploaded else "is"
    if not started(org):
        what = (f"✅ *{name}* {verb} approved. Your team gets {'them' if uploaded else 'it'} when you start the "
                "programme (Admin Center step 5).")
    elif c.get("send_on") and not aup_store.is_due(c):
        what = f"🗓 *{name}* {verb} approved and go{'' if uploaded else 'es'} out *{_day(c['send_on'])}* at 10:00 (each person's time)."
    else:
        what = f"✅ *{name}* {'were' if uploaded else 'was'} sent to {people} {'person' if people == 1 else 'people'}."
    blocks = [{"type": "section", "text": _md(what)},
              _ctx(f"{len(aup_store.cards(org))} of {_total(org)} policy cards approved." + ("" if _total(org) == 3 else " Send as many as you like.")),
              {"type": "actions", "block_id": "aidl_pc_sent", "elements": [
                  {"type": "button", "text": _text("📌 Add another card"), "action_id": "aidl_pc_topics",
                   "value": "topics", "style": "primary"}]}]
    return _modal(SENT, "Policy card", blocks, close="Done", meta=_keep(meta))


# ---------- company + regulators ----------

def company_view(org: Organization, meta: dict, then: str = "topics") -> dict:
    co = aup_store.company(org)
    opt = lambda t: {"text": _text(t, 75), "value": t}  # noqa: E731
    countries = {"type": "multi_static_select", "action_id": "v", "placeholder": _text("Choose countries"),
                 "options": [opt(c) for c in regulators.COUNTRIES]}
    if co.get("countries"):
        countries["initial_options"] = [opt(c) for c in co["countries"] if c in regulators.COUNTRIES]
    industry = {"type": "static_select", "action_id": "v", "placeholder": _text("Choose one"),
                "options": [opt(i) for i in regulators.INDUSTRIES]}
    if co.get("industry") in regulators.INDUSTRIES:
        industry["initial_option"] = opt(co["industry"])
    blocks = [{"type": "image", "alt_text": "Your company",
               "image_url": aup_banner.url("Your company · asked once", "2 answers", org.name, 0, _total(org))},
              _ctx(f"AIDL already knows *{org.name}* from Slack. These two answers tell us which regulators may oversee "
                   "you — AIDL works that out and sends it to you as a private card."),
              {"type": "input", "block_id": "countries", "label": _text("Where do you operate?"), "element": countries},
              {"type": "input", "block_id": "industry", "label": _text("Your industry"), "element": industry}]
    return _modal(COMPANY, "About your company", blocks, submit="Next →" if then == "topics" else "Save",
                  close="Cancel", meta={**_keep(meta), "then": then})


def submit_company(admin: AIDLUser, org: Organization, view: dict) -> dict:
    from .slack_modals import in_background

    values = (view.get("state") or {}).get("values") or {}
    countries = [o["value"] for o in ((values.get("countries") or {}).get("v") or {}).get("selected_options") or []]
    industry = (((values.get("industry") or {}).get("v") or {}).get("selected_option") or {}).get("value", "")
    if not countries:
        return {"response_action": "errors", "errors": {"countries": "Choose at least one."}}
    if not industry:
        return {"response_action": "errors", "errors": {"industry": "Choose your industry."}}
    aup_store.save_company(org, countries, industry)
    in_background(send_regulator_card, org.pk, admin.pk)
    meta = _meta(view)
    if meta.get("then") != "topics":
        return {"response_action": "update", "view": regulators_view(org, meta)}
    # Slack waits only 3 seconds: answer with a short "Loading…" screen, then
    # fill in the topics.
    in_background(_show_topics, org.pk, view.get("id", ""), meta)
    return {"response_action": "update", "view": _modal(TOPICS, "Add a policy card", [
        {"type": "section", "text": _md("✓ Saved. Loading your topics…")}], meta=_keep(meta))}


def _show_topics(org_pk, view_id: str, meta: dict) -> None:
    from . import slack_client

    org = Organization.objects.get(pk=org_pk)
    if view_id:
        slack_client.slack_api("views.update", slack_client.bot_token(org),
                               json={"view_id": view_id, "view": topics_view(org, meta)})


def regulators_view(org: Organization, meta: dict) -> dict:
    co = aup_store.company(org)
    return _modal(REGULATORS, "Your regulators",
                  regulators.card_blocks(org.name, co.get("countries") or [], co.get("industry", "")),
                  meta=_keep(meta))


def send_regulator_card(org_pk, admin_pk) -> None:
    """The private Regulator card, by DM to the admin who answered."""
    from . import slack_client

    org = Organization.objects.get(pk=org_pk)
    admin = AIDLUser.objects.get(pk=admin_pk)
    if not (admin.microsoft_id or "").startswith("slack:"):
        return
    co = aup_store.company(org)
    slack_client.send_dm(slack_client.bot_token(org), org, admin.microsoft_id.split(":")[-1],
                         text=f"Regulators that may oversee {org.name}",
                         blocks=regulators.card_blocks(org.name, co.get("countries") or [], co.get("industry", "")))


# ---------- clicks inside these popups ----------

def action(org: Organization, view: dict, action_id: str, value: str, block_id: str, url: str = "") -> dict | None:
    """The new view for a click inside a policy-card popup, or None. `url` = this admin's private plans-page link
    (empty when they can't change the plan)."""
    meta = _meta(view)
    if action_id in ("aidl_pc_pick", "aidl_pc_locked") and value.isdigit() and pc.is_locked(org, int(value)):
        return locked_view(org, int(value), meta, url)
    if action_id == "aidl_pc_pick":
        a = aup_store.answers(org)
        return card_view(org, int(value) if value.isdigit() else 1, a, meta)
    if action_id in ("aidl_pc_back", "aidl_pc_topics"):
        return topics_view(org, meta)
    if action_id == "aidl_pc_paste":
        return redraw(org, view, paste=True)
    if view.get("callback_id") == CARD and (block_id[:2] in ("q|", "f|") or block_id.startswith("tools_")
                                             or block_id == "when"):
        return redraw(org, view)
    return None


def open_view(org: Organization, meta: dict) -> dict:
    """"Add a policy card": the company questions first time, then topics."""
    return topics_view(org, meta) if aup_store.company(org) else company_view(org, meta, then="topics")

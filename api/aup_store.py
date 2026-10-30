"""The organization's AI policy as policy cards (policy_cards.py).

The policy is 14 short cards, one per AUP point. The admin sends them one at
a time (or a whole uploaded AUP at once, aup_upload.py), today or on a
chosen day; each card has its own version, and employees press "I agree"
on each one.

Organization.aup_data (JSON) holds
    answers, fills, tools   the admin's answers, shared by all cards (a
                            question on one card can depend on another's)
    company                 {countries, industry} — for the laws card (laws.py)
    cards                   {"<n>": {title, lines, version, number, send_on,
                            sent_at, approved_by, source, file_name}}
                            — every card the admin approved; send_on is ""
                            (straight away) or "YYYY-MM-DD" (10:00 in each
                            person's time zone)
    published               (older AUPs, built in one go) — read once and
                            turned into 14 cards with the same version, so
                            people's agreements still count

Employees agree to card n as Acknowledgement item "aup_<n>" at that card's
version. The licence needs every card that has been sent so far (plus the
Highway Code and the Traffic Light Check); a card scheduled for later is
needed once its day comes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from django.utils import timezone

from . import aup_builder
from .models import Acknowledgement, AIDLUser, Organization

POINTS = 14


def point_item(n: int) -> str:
    return f"aup_{n}"


def is_point_item(item: str) -> bool:
    return item.startswith("aup_") and item[4:].isdigit() and 1 <= int(item[4:]) <= POINTS


def _load(org: Organization | None) -> dict:
    if org is None or not org.aup_data:
        return {}
    try:
        data = json.loads(org.aup_data)
    except ValueError:
        return {}
    if data.get("published") and not data.get("cards"):
        data = _from_published(org, data)
    return data


def _save(org: Organization, data: dict) -> None:
    org.aup_data = json.dumps(data, ensure_ascii=False)
    org.save(update_fields=["aup_data", "updated_at"])


def _from_published(org: Organization, data: dict) -> dict:
    """An AUP published in one go (the old 15-section builder or an upload):
    each point becomes a card with the AUP's version, so existing agreements
    keep counting. Only if that AUP was approved — otherwise it was never
    sent and the admin starts with cards."""
    from .card_approval import APPROVED, status

    p = data["published"]
    ver = p.get("version", "")
    approved = bool(ver) and status(org, f"aup:{ver}") == APPROVED
    data = {k: v for k, v in data.items() if k not in ("published", "draft")}
    if p.get("answers"):
        data.setdefault("answers", p["answers"])
        data.setdefault("fills", p.get("fills") or {})
    if not approved:
        return data
    if p.get("source") == "upload":
        points = p.get("points") or []
    else:
        try:
            points = aup_builder.generate(p["answers"], p.get("fills") or {}, strict=False)["points"]
        except Exception:  # noqa: BLE001
            points = []
    data["cards"] = {str(pt["no"]): {"title": pt["title"], "lines": [c["text"] for c in pt.get("clauses") or []],
                                     "version": ver, "number": int(p.get("number") or 1), "send_on": "",
                                     "sent_at": p.get("published_at", ""), "approved_by": p.get("published_by", ""),
                                     "source": p.get("source", "builder"), "file_name": p.get("file_name", "")}
                     for pt in points}
    return data


# ---------- the admin's answers ----------

def _starting_tools(org: Organization) -> list[str]:
    try:
        from .aup import generate_aup

        return [a["name"] for a in generate_aup(org)["allowed_ai"]]
    except Exception:  # noqa: BLE001 — the app list is only a pre-fill
        return []


def answers(org: Organization) -> dict:
    """{answers, fills, tools}: what the admin has chosen so far, the
    recommended answers for everything else."""
    data = _load(org)
    a = aup_builder.recommended_answers()
    a.update(data.get("answers") or {})
    a["0.1"] = a["0.2"] = org.name
    tools = data.get("tools")
    return {"answers": a, "fills": dict(data.get("fills") or {}),
            "tools": list(tools) if tools is not None else _starting_tools(org)}


def save_answers(org: Organization, answers: dict, fills: dict, tools: list[str]) -> None:
    data = _load(org)
    data["answers"], data["fills"], data["tools"] = answers, fills, tools
    _save(org, data)


def company(org: Organization | None) -> dict:
    return dict(_load(org).get("company") or {})


def save_company(org: Organization, countries: list[str], industry: str) -> None:
    data = _load(org)
    data["company"] = {"countries": countries, "industry": industry}
    _save(org, data)


# ---------- cards ----------

def cards(org: Organization | None) -> dict[int, dict]:
    """Every approved card, by point number."""
    return {int(n): c for n, c in (_load(org).get("cards") or {}).items()}


def card(org: Organization | None, n: int) -> dict | None:
    return cards(org).get(n)


def fingerprint(n: int, title: str, lines: list[str]) -> str:
    digest = hashlib.sha256(json.dumps([n, title, lines], ensure_ascii=False).encode()).hexdigest()
    return f"P{n}-" + digest[:6].upper()


def save_card(org: Organization, n: int, title: str, lines: list[str], *, by: AIDLUser | None = None,
              send_on: str = "", source: str = "questions", file_name: str = "") -> dict:
    """Approve card n (new, or a new version of it)."""
    data = _load(org)
    all_cards = data.setdefault("cards", {})
    old = all_cards.get(str(n)) or {}
    ver = fingerprint(n, title, lines)
    all_cards[str(n)] = {
        "title": title, "lines": lines, "version": ver,
        "number": int(old.get("number") or 0) + (old.get("version") != ver),
        "send_on": send_on, "sent_at": "" if send_on else timezone.now().isoformat(),
        "approved_by": str(by.pk) if by else "", "source": source, "file_name": file_name,
        "scheduled": {},  # member id → [channel, scheduled message id]
    }
    _save(org, data)
    from .card_image import warm

    warm(org, [f"aup-{n}"])
    return all_cards[str(n)]


def set_scheduled(org: Organization, n: int, scheduled: dict) -> None:
    data = _load(org)
    c = (data.get("cards") or {}).get(str(n))
    if c is not None:
        c["scheduled"] = scheduled
        _save(org, data)


def is_due(c: dict, today: date | None = None) -> bool:
    """Sent (or its day has come)."""
    send_on = c.get("send_on") or ""
    return not send_on or send_on <= (today or timezone.localdate()).isoformat()


def due_cards(org: Organization | None) -> dict[int, dict]:
    return {n: c for n, c in cards(org).items() if is_due(c)}


def required_points(org: Organization | None) -> dict[int, str]:
    """{point: version} an employee must agree to (cards sent so far)."""
    return {n: c["version"] for n, c in due_cards(org).items()}


def card_version(org: Organization | None, n: int) -> str:
    c = card(org, n)
    return c["version"] if c else ""


def point(org: Organization | None, n: int) -> dict | None:
    """Card n in the aup_builder point shape ({no, title, intro, clauses})."""
    c = card(org, n)
    if c is None:
        return None
    return {"no": n, "title": c["title"], "intro": "", "clauses": [{"qid": "", "text": t} for t in c["lines"]]}


def current(org: Organization | None) -> dict | None:
    """The policy as it stands: the cards sent so far, in point order."""
    due = due_cards(org)
    if not due:
        return None
    name = org.name if org is not None else ""
    return {"organization": {"name": name, "short_name": name, "industry": "", "size": "", "countries": "",
                             "languages": []},
            "points": [point(org, n) for n in sorted(due)], "version": version(org)}


def is_complete(org: Organization | None) -> bool:
    """At least one card approved (sent or scheduled)."""
    return bool(cards(org))


def version(org: Organization | None) -> str:
    """One string for the policy as sent so far (changes with any card)."""
    req = required_points(org)
    if not req:
        return ""
    return "AUP-" + hashlib.sha256("|".join(f"{n}:{v}" for n, v in sorted(req.items())).encode()).hexdigest()[:6].upper()


def clear(org: Organization) -> None:
    org.aup_data = ""
    org.save(update_fields=["aup_data", "updated_at"])


# ---------- employees ----------

def points_accepted(member: AIDLUser, org: Organization | None) -> set[int]:
    """Cards the employee agreed to, at their current version."""
    req = required_points(org)
    if not req:
        return set()
    rows = Acknowledgement.objects.filter(user_id=str(member.pk), item__startswith="aup_",
                                          version__in=list(set(req.values()))).values_list("item", "version")
    return {int(item[4:]) for item, ver in rows if is_point_item(item) and req.get(int(item[4:])) == ver}


def pending_points(member: AIDLUser, org: Organization | None) -> list[int]:
    done = points_accepted(member, org)
    return [n for n in sorted(required_points(org)) if n not in done]


def next_point(member: AIDLUser, org: Organization | None) -> int | None:
    pending = pending_points(member, org)
    return pending[0] if pending else None


def all_points_accepted(member: AIDLUser, org: Organization | None) -> bool:
    """Agreed to every card sent so far (none sent = nothing to agree to)."""
    return not pending_points(member, org)

"""The organization's AUP built from the question bank (aup_builder.py).

Organization.aup_data (JSON) holds
    draft      {answers, fills, section, started_at}  — the admin's work in
               progress, saved after every screen so they can stop and continue
    published  {answers, fills, version, number, published_at, published_by,
               started_at} — the AUP employees receive (number = v1, v2 …).
               An uploaded AUP (aup_upload.py) is {source: "upload", points,
               file_name, upload_id, model, version, number, …} instead.

Employees accept it one point at a time: Acknowledgement items "aup_1" …
"aup_14", versioned by the published AUP's version. All 14 points (plus the
Highway Code and the Traffic Light Check) are needed for the licence.
"""

from __future__ import annotations

import json

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
        return json.loads(org.aup_data)
    except ValueError:
        return {}


def _save(org: Organization, data: dict) -> None:
    org.aup_data = json.dumps(data, ensure_ascii=False)
    org.save(update_fields=["aup_data", "updated_at"])


# ---------- admin: draft → published ----------

def _starting_answers(org: Organization) -> tuple[dict, dict]:
    """A new draft: the recommended answers, the organization's name, and the
    approved AI tools from the AI Apps list."""
    answers = aup_builder.recommended_answers()
    answers["0.1"] = org.name
    try:
        from .aup import generate_aup

        tools = [a["name"] for a in generate_aup(org)["allowed_ai"]]
    except Exception:  # noqa: BLE001 — the app list is only a pre-fill
        tools = []
    if tools:
        answers["7.2"] = ", ".join(tools)
    return answers, {}


def draft(org: Organization) -> dict:
    """The answers being edited: the saved draft, else a copy of the
    published AUP (Change AUP), else a fresh start."""
    data = _load(org)
    if data.get("draft"):
        return data["draft"]
    if data.get("published") and data["published"].get("source") != "upload":
        p = data["published"]
        return {"answers": dict(p["answers"]), "fills": dict(p.get("fills") or {}), "section": 0}
    answers, fills = _starting_answers(org)
    return {"answers": answers, "fills": fills, "section": 0}


def save_draft(org: Organization, answers: dict, fills: dict, section: int) -> None:
    data = _load(org)
    started = (data.get("draft") or {}).get("started_at") or timezone.now().isoformat()
    data["draft"] = {"answers": answers, "fills": fills, "section": section, "started_at": started}
    _save(org, data)


def has_draft(org: Organization) -> bool:
    return bool(_load(org).get("draft"))


def publish(org: Organization, by: AIDLUser | None = None) -> dict:
    """Generate the AUP from the draft and make it the published one.
    Raises aup_builder.AupAnswersError if anything is missing."""
    data = _load(org)
    d = data.get("draft") or draft(org)
    aup = aup_builder.generate(d["answers"], d.get("fills") or {})
    data["published"] = {"answers": d["answers"], "fills": d.get("fills") or {}, "version": aup["version"],
                         "number": _number(data, aup["version"]), "published_at": timezone.now().isoformat(),
                         "published_by": str(by.pk) if by else "", "started_at": d.get("started_at", "")}
    data.pop("draft", None)
    _save(org, data)
    _warm(org)
    return aup


def publish_points(org: Organization, points: list[dict], by: AIDLUser | None, source: dict) -> dict:
    """Make ready-written points (an uploaded AUP, aup_upload.py) the
    published AUP. Same versioning and approval as a built one."""
    from .aup_upload import fingerprint

    data = _load(org)
    ver = fingerprint(points)
    data["published"] = {"source": "upload", "points": points, "version": ver, "number": _number(data, ver),
                         "published_at": timezone.now().isoformat(), "published_by": str(by.pk) if by else "",
                         "answers": {}, **source}
    data.pop("draft", None)
    _save(org, data)
    _warm(org)
    return current(org)


def _number(data: dict, new_version: str) -> int:
    """v1, v2 …: a new number whenever the published text changes."""
    previous = data.get("published") or {}
    return int(previous.get("number") or 1) + (previous.get("version") not in (None, new_version))


def _warm(org: Organization) -> None:
    from .card_image import onboarding_kinds, warm

    warm(org, onboarding_kinds(org, "aup"))  # the 14 point cards, ready for the admin's preview


def clear(org: Organization) -> None:
    org.aup_data = ""
    org.save(update_fields=["aup_data", "updated_at"])


# ---------- the published AUP ----------

def current(org: Organization | None) -> dict | None:
    p = _load(org).get("published")
    if not p:
        return None
    if p.get("source") == "upload":
        name = org.name if org is not None else ""
        return {"organization": {"name": name, "short_name": name, "industry": "", "size": "", "countries": "",
                                 "languages": []},
                "points": p.get("points") or [], "version": p.get("version", "")}
    try:
        return aup_builder.generate(p["answers"], p.get("fills") or {}, strict=False)
    except Exception:  # noqa: BLE001 — a broken saved AUP must not break Slack
        return None


def is_complete(org: Organization | None) -> bool:
    return bool(_load(org).get("published"))


def version(org: Organization | None) -> str:
    return (_load(org).get("published") or {}).get("version", "")


def published_info(org: Organization | None) -> dict:
    """number (v1, v2 …), published_at, started_at and answers of the
    published AUP."""
    p = _load(org).get("published") or {}
    return {"number": int(p.get("number") or 1), "published_at": p.get("published_at", ""),
            "started_at": p.get("started_at", ""), "answers": p.get("answers") or {},
            "source": p.get("source", "builder"), "file_name": p.get("file_name", "")}


# ---------- employees: one point at a time ----------

def points_accepted(member: AIDLUser, org: Organization | None, ver: str | None = None) -> set[int]:
    ver = version(org) if ver is None else ver
    if not ver:
        return set()
    items = Acknowledgement.objects.filter(user_id=str(member.pk), version=ver,
                                           item__startswith="aup_").values_list("item", flat=True)
    return {int(i[4:]) for i in items if is_point_item(i)}


def next_point(member: AIDLUser, org: Organization | None) -> int | None:
    """The point the employee should accept next (None = all done)."""
    done = points_accepted(member, org)
    return next((n for n in range(1, POINTS + 1) if n not in done), None)


def all_points_accepted(member: AIDLUser, org: Organization | None) -> bool:
    return is_complete(org) and len(points_accepted(member, org)) == POINTS

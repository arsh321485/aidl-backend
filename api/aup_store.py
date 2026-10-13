"""The organization's AUP built from the question bank (aup_builder.py).

Organization.aup_data (JSON) holds
    draft      {answers, fills, section}  — the admin's work in progress,
               saved after every screen so they can stop and continue
    published  {answers, fills, version, published_at, published_by}
               — the AUP employees receive

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
    if data.get("published"):
        p = data["published"]
        return {"answers": dict(p["answers"]), "fills": dict(p.get("fills") or {}), "section": 0}
    answers, fills = _starting_answers(org)
    return {"answers": answers, "fills": fills, "section": 0}


def save_draft(org: Organization, answers: dict, fills: dict, section: int) -> None:
    data = _load(org)
    data["draft"] = {"answers": answers, "fills": fills, "section": section}
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
                         "published_at": timezone.now().isoformat(), "published_by": str(by.pk) if by else ""}
    data.pop("draft", None)
    _save(org, data)
    return aup


def clear(org: Organization) -> None:
    org.aup_data = ""
    org.save(update_fields=["aup_data", "updated_at"])


# ---------- the published AUP ----------

def current(org: Organization | None) -> dict | None:
    p = _load(org).get("published")
    if not p:
        return None
    try:
        return aup_builder.generate(p["answers"], p.get("fills") or {}, strict=False)
    except Exception:  # noqa: BLE001 — a broken saved AUP must not break Slack
        return None


def is_complete(org: Organization | None) -> bool:
    return bool(_load(org).get("published"))


def version(org: Organization | None) -> str:
    return (_load(org).get("published") or {}).get("version", "")


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

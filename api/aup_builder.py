"""AUP generator — turns answers to the AUP question bank into the 14 policy
points (platform-neutral; not wired into Slack yet).

Data: api/data/aup_question_bank.json, imported from the manager's workbook
with `python manage.py import_aup_bank <xlsx>`.

Answers are a dict keyed by question id:
    single / yes-no   "2.1": "2.1.a"                (option id)
    multi             "2.2": ["2.2.a", "2.2.c"]     (option ids)
    text / email      "0.1": "Acme Trading W.L.L."
    date              "0.9": "2026-11-01"
Typed details for "(specify)" / "Other" options and {fill} wordings go in
`fills`, keyed by option id: {"9.2.a": "security@acme.com"}.

How each answer becomes policy text:
- single choice → the option's reviewed policy sentence ("Policy wording"
  column), {fill} replaced; "(no sentence)" adds nothing
- multiple choice → the question's template with the chosen options listed;
  templates that are only "{Q}." give one sentence per option
- text / email / date → inserted into the question's template
- questions hidden by "Show if" are skipped
- Organization Profile answers go into point 1 (who it applies to) and
  point 14 (owner, contact, effective date, regulations) — see ORG_PLACEMENT
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

_BANK_FILE = Path(__file__).resolve().parent / "data" / "aup_question_bank.json"

# Organization Profile questions → (AUP point, position) in the policy.
ORG_PLACEMENT = {"0.1": (1, "start"), "0.6": (14, "end"), "0.7": (14, "end"),
                 "0.8": (14, "end"), "0.9": (14, "end")}
# Words kept capitalised when a list item is put mid-sentence.
_KEEP_CASE = {"Company", "Microsoft", "Google", "Slack", "Teams", "WhatsApp", "VPN", "USB", "IT", "HR", "MDM"}
_SPECIFY = re.compile(r"\s*\((?:please\s+)?specify[^)]*\)\s*", re.I)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Questions in the bank that AIDL doesn't ask (and the policy doesn't use) —
# the manager's cuts (Oct 2026). General-awareness ones became ready-made
# awareness cards (policy_awareness.py); the laws question became the Regulator
# card (regulators.py); the approved AI tools are picked from a list (policy_cards.py).
HIDDEN = {
    "0.6",  # laws and standards → Regulator card from country + industry
    "0.7",  # which department owns the policy
    "0.8",  # contact email
    "1.2",  # which company resources the policy covers
    "1.4",  # what personal devices must have
    "2.3",  # who can access monitoring records
    "2.4",  # how long monitoring logs are kept
    "3.1",  # is personal use allowed
    "3.2",  # which personal activities are acceptable
    "4.1",  # prohibited activities → awareness cards
    "5.5",  # screen lock time → awareness card
    "6.6",  # clean desk and printing → awareness card
    "7.2",  # approved AI tools → picked from a list
    "7.3",  # never enter into AI tools → awareness card
    "7.5",  # other free online tools (obvious)
}
# Option changes on top of the workbook (kept when it is re-imported).
_DROP_OPTIONS = {"9.2": ("'report phishing'",)}  # text starts with
_ADD_OPTIONS = {
    "5.3": [(4, {"id": "5.3.it", "text": "IT systems (servers, admin and IT tools)", "recommended": True})],
    "9.2": [(None, {"id": "9.2.x", "text": "Something else (add your own)", "recommended": False})],
}


class AupAnswersError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("invalid AUP answers")
        self.errors = errors


# ---------- question bank ----------

@lru_cache(maxsize=1)
def bank() -> dict:
    data = json.loads(_BANK_FILE.read_text(encoding="utf-8"))
    for q in data["questions"]:
        drop = _DROP_OPTIONS.get(q["id"], ())
        q["options"] = [o for o in q["options"] if not o["text"].lower().startswith(drop)] if drop else q["options"]
        for at, opt in _ADD_OPTIONS.get(q["id"], ()):
            if not any(o["id"] == opt["id"] for o in q["options"]):
                full = {"note": "", "wording": "", "no_sentence": False, **opt}
                q["options"].insert(len(q["options"]) if at is None else at, full)
    data["by_id"] = {q["id"]: q for q in data["questions"]}
    for q in data["questions"]:
        q["option_by_id"] = {o["id"]: o for o in q["options"]}
    return data


def question(qid: str) -> dict:
    return bank()["by_id"][qid]


def recommended_answers() -> dict:
    """The pre-selected (green) answers — what 'accept the whole section'
    submits. Typed answers have no recommendation and stay empty."""
    answers = {}
    for q in bank()["questions"]:
        picked = [o["id"] for o in q["options"] if o["recommended"]]
        if q["type"] == "single" and picked:
            answers[q["id"]] = picked[0]
        elif q["type"] == "multi" and picked:
            answers[q["id"]] = picked
    return answers


# ---------- show-if ----------

def _chosen_texts(answers: dict, qid: str) -> list[str]:
    q = bank()["by_id"].get(qid)
    value = answers.get(qid)
    if q is None or value in (None, "", []):
        return []
    ids = value if isinstance(value, list) else [value]
    return [q["option_by_id"][i]["text"] for i in ids if i in q["option_by_id"]]


def is_visible(q: dict, answers: dict) -> bool:
    """Evaluate the workbook's 'Show if' rule against the answers so far."""
    rule = q["show_if"]
    if not rule:
        return True
    m = re.match(r"^(\S+) is any '(.+)' option$", rule)
    if m:
        return any(t.startswith(m.group(2)) for t in _chosen_texts(answers, m.group(1)))
    m = re.match(r"^(\S+) is (not )?'(.+)'$", rule)
    if m:
        texts = _chosen_texts(answers, m.group(1))
        if not texts:  # the earlier question isn't answered yet → hide follow-ups
            return False
        hit = m.group(3) in texts
        return not hit if m.group(2) else hit
    raise ValueError(f"Unsupported show-if rule for {q['id']}: {rule!r}")


def visible_questions(answers: dict, section: int | None = None) -> list[dict]:
    return [q for q in bank()["questions"]
            if (section is None or q["section"] == section) and q["id"] not in HIDDEN and is_visible(q, answers)]


# ---------- validation ----------

def _needs_fill(q: dict, opt: dict) -> bool:
    """A chosen option that asks for typed details ("(specify)", "Other",
    "Something else (add your own)")."""
    return (bool(_SPECIFY.search(opt["text"])) or "{fill}" in opt.get("wording", "")
            or opt["id"].endswith(".x"))


def validate(answers: dict, fills: dict | None = None) -> dict[str, str]:
    """Problems per question id ({} = complete). Hidden questions are ignored."""
    fills = fills or {}
    errors: dict[str, str] = {}
    for q in visible_questions(answers):
        qid, value = q["id"], answers.get(q["id"])
        empty = value in (None, "", [])
        if empty:
            if q["required"]:
                errors[qid] = "Please answer this question."
            continue
        if q["type"] in ("single", "multi"):
            ids = value if isinstance(value, list) else [value]
            if q["type"] == "single" and isinstance(value, list):
                errors[qid] = "Choose one answer."
                continue
            unknown = [i for i in ids if i not in q["option_by_id"]]
            if unknown:
                errors[qid] = f"Unknown option {unknown[0]}."
                continue
            for i in ids:
                opt = q["option_by_id"][i]
                if _needs_fill(q, opt) and not (fills.get(i) or "").strip():
                    errors[qid] = f"Please fill in: {q['fill_label'] or opt['text']}."
                    break
        elif q["type"] == "email" and not _EMAIL.match(str(value).strip()):
            errors[qid] = "Enter a valid email address."
        elif q["type"] == "date":
            try:
                date.fromisoformat(str(value))
            except ValueError:
                errors[qid] = "Enter a date as YYYY-MM-DD."
    return errors


# ---------- wording ----------

def _mid_sentence(text: str) -> str:
    """Lower-case a list item's first letter mid-sentence ("Email" →
    "email"), but keep names ("Human Resources", "Legal / Compliance")."""
    words = text.split("(", 1)[0].split()
    first = words[0] if words else ""
    if first in _KEEP_CASE or not first[:1].isupper() or (len(first) > 1 and not first[1:].islower()):
        return text
    if any(w[:1].isupper() for w in words[1:]):  # title-case name
        return text
    return text[0].lower() + text[1:]


def _item(opt: dict, fills: dict) -> str:
    """One chosen option as list text, with its typed detail."""
    fill = (fills.get(opt["id"]) or "").strip()
    if opt["id"].endswith(".x") or opt["text"].lower().startswith("other (please specify"):
        return fill
    base = _SPECIFY.sub(" ", opt["text"]).strip()
    return f"{base}: {fill}" if fill and _SPECIFY.search(opt["text"]) else base


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\.\.+$", ".", text)
    return text if text.endswith((".", "!", "?")) else text + "."


def _render(q: dict, answers: dict, fills: dict, org: dict) -> dict | None:
    """The policy clause for one answered question, or None."""
    value = answers.get(q["id"])
    if value in (None, "", []):
        return None
    template = q["template"]
    if q["type"] == "single":
        opt = q["option_by_id"].get(value)
        if opt is None or opt["no_sentence"]:
            return None
        if opt["wording"]:
            text = opt["wording"].replace("{fill}", (fills.get(opt["id"]) or "").strip())
        elif template:  # no reviewed sentence yet → insert the answer as-is
            text = template.replace("{%s}" % q["id"], _item(opt, fills))
        else:
            return None
        return {"qid": q["id"], "text": _sentence(_fill_org(text, org))}
    if q["type"] == "multi":
        if not template:
            return None
        opts = [q["option_by_id"][i] for i in value if i in q["option_by_id"]]
        items = [_item(o, fills) for o in opts]
        items = [i for i in items if i]
        if not items:
            return None
        if template.strip() == "{%s}." % q["id"]:  # each option is its own rule
            return {"qid": q["id"], "text": " ".join(_sentence(i) for i in items), "items": items}
        text = template.replace("{%s}" % q["id"], _join([_mid_sentence(i) for i in items]))
        return {"qid": q["id"], "text": _sentence(_fill_org(text, org)), "items": items}
    if not template:
        return None
    shown = str(value).strip()
    if q["type"] == "date":
        shown = date.fromisoformat(shown).strftime("%d %B %Y").lstrip("0")
    return {"qid": q["id"], "text": _sentence(_fill_org(template.replace("{%s}" % q["id"], shown), org))}


def _fill_org(text: str, org: dict) -> str:
    """Templates may mention other answers, e.g. the company name."""
    return re.sub(r"\{(\d+\.\d+)\}", lambda m: org.get(m.group(1), m.group(0)), text)


def generate(answers: dict, fills: dict | None = None, *, strict: bool = True) -> dict:
    """Build the AUP: {"organization", "points": [14 × {no, title, intro,
    clauses: [{qid, text, items?}]}], "version"}. With strict=True,
    incomplete answers raise AupAnswersError(errors)."""
    fills = fills or {}
    if strict:
        errors = validate(answers, fills)
        if errors:
            raise AupAnswersError(errors)
    org_name = str(answers.get("0.1") or "").strip()
    org = {"0.1": org_name, "0.2": str(answers.get("0.2") or "").strip() or org_name}

    clauses: dict[int, list] = {}
    placed: dict[int, dict[str, list]] = {}
    for q in visible_questions(answers):
        clause = _render(q, answers, fills, org)
        if clause is None:
            continue
        if q["section"] == 0:
            where = ORG_PLACEMENT.get(q["id"])
            if where:
                placed.setdefault(where[0], {"start": [], "end": []})[where[1]].append(clause)
            continue
        clauses.setdefault(q["section"], []).append(clause)

    points = []
    for s in bank()["sections"]:
        if s["no"] == 0:
            continue
        extra = placed.get(s["no"], {"start": [], "end": []})
        points.append({"no": s["no"], "title": s["title"], "intro": s["intro"],
                       "clauses": extra["start"] + clauses.get(s["no"], []) + extra["end"]})

    def labels(qid):
        return [_item(o, fills) or o["text"] for o in
                (question(qid)["option_by_id"].get(i) for i in
                 (answers.get(qid) if isinstance(answers.get(qid), list) else [answers.get(qid)]))
                if o is not None]

    organization = {
        "name": org_name, "short_name": org["0.2"],
        "industry": _join(labels("0.3")), "size": _join(labels("0.4")),
        "countries": str(answers.get("0.5") or "").strip(), "languages": labels("14.5"),
    }
    digest = hashlib.sha256(json.dumps(points, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"organization": organization, "points": points, "version": "AUP-" + digest[:6].upper()}


def as_text(aup: dict) -> str:
    """Plain-text rendering (previews, exports, tests)."""
    lines = [f"AI Acceptable Use Policy — {aup['organization']['name']}  ({aup['version']})"]
    for p in aup["points"]:
        lines += ["", f"{p['no']}. {p['title']}"] + [f"   {c['text']}" for c in p["clauses"]]
    return "\n".join(lines)

"""Import the manager's AI_Usage_Awareness_Packages.xlsx into
api/data/awareness_packages.json: the Default (12) and Basic (24) packages,
the full library (655 cards) and the 15 awareness categories (topics that
admins pick from when upgrading — plans.py).

    python manage.py import_awareness_packages "E:\\AI_Usage_Awareness_Packages.xlsx"

Needs openpyxl only for this command.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

OUT = Path(__file__).resolve().parents[2] / "data" / "awareness_packages.json"


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _header_rows(ws, first: str):
    rows = list(ws.iter_rows(values_only=True))
    i = next(i for i, r in enumerate(rows) if r and _s(r[0]) == first)
    return [_s(h) for h in rows[i]], rows[i + 1:]


class Command(BaseCommand):
    help = "Import the awareness packages workbook into api/data/awareness_packages.json."

    def add_arguments(self, parser):
        parser.add_argument("workbook")

    def handle(self, *args, **options):
        try:
            import openpyxl
        except ImportError as exc:
            raise CommandError("pip install openpyxl first") from exc
        wb = openpyxl.load_workbook(options["workbook"], data_only=True)

        packages = {}
        for key, sheet in (("default", "Default (12)"), ("basic", "Basic (24)")):
            head, rows = _header_rows(wb[sheet], "Post order")
            cards = []
            for r in rows:
                if not isinstance(r[0], int):
                    break
                d = dict(zip(head, r))
                cards.append({"order": d["Post order"], "no": _s(d["Card no."]), "title": _s(d["Title"]),
                              "category": _s(d["Awareness category"]), "type": _s(d["Card type"]),
                              "hook": _s(d["Hook"]), "message": _s(d["Message"]), "action": _s(d["Action"]),
                              "local_link": _s(d["Needs local link"]) == "Yes"})
            packages[key] = cards

        head, rows = _header_rows(wb["Categories"], "Code")
        categories = []
        for r in rows:
            d = dict(zip(head, r))
            if not _s(d.get("Code")).startswith("C"):
                break
            categories.append({"code": _s(d["Code"]), "title": _s(d["Category"]), "covers": _s(d["What it covers"]),
                               "rating": _s(d["Rating"])})
        code_of = {c["title"]: c["code"] for c in categories}

        head, rows = _header_rows(wb["Full Library"], "Card no.")
        library = []
        for r in rows:
            d = dict(zip(head, r))
            if not _s(d.get("Card no.")):
                continue
            library.append({"no": _s(d["Card no."]), "title": _s(d["Title"]), "type": _s(d["Card type"]),
                            "category": _s(d["Awareness category"]), "code": code_of.get(_s(d["Awareness category"]), ""),
                            "rating": _s(d["Rating"]), "hook": _s(d["Hook"]), "message": _s(d["Message"]),
                            "action": _s(d["Action"]), "local_link": _s(d["Needs local link"]) == "Yes"})

        OUT.write_text(json.dumps({"source": Path(options["workbook"]).name, "packages": packages,
                                   "categories": categories, "library": library},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        self.stdout.write(f"default {len(packages['default'])}, basic {len(packages['basic'])}, "
                          f"{len(categories)} categories, library {len(library)} -> {OUT}")

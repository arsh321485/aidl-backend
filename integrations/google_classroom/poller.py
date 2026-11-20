"""Background check of every AIDL class (see apps.py)."""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


def sync_all() -> dict:
    from api.models import Organization

    from .learners import sync
    from .models import AidlClass

    totals: dict[str, int] = {}
    for org_id in AidlClass.objects.exclude(course_id="").values_list("organization_id", flat=True):
        org = Organization.objects.filter(pk=org_id, is_active=True).first()
        if org is None:
            continue
        for k, v in sync(org).items():
            totals[k] = totals.get(k, 0) + v
    return totals


def start(seconds: int) -> None:
    def loop():
        from django.db import connections

        time.sleep(5)  # let the server finish starting
        while True:
            try:
                totals = sync_all()
                if any(totals.values()):
                    logger.info("google classroom sync: %s", totals)
            except Exception:  # noqa: BLE001 - keep polling
                logger.exception("google classroom sync crashed")
            finally:
                connections.close_all()
            time.sleep(seconds)

    threading.Thread(target=loop, name="google-classroom-sync", daemon=True).start()
    logger.info("google classroom: checking students' answers every %ss", seconds)

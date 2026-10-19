"""Rendered card and licence PNGs, kept so Slack gets them at once.

Drawing a card takes seconds on the server, and Slack gives up waiting for
an image after a few — the card then shows a broken image even though the
link works in a browser. So every picture is drawn once and kept: in memory
(this process) and in the database (CardImage — survives restarts and is
shared by every worker). Keys carry every version that changes the picture,
so a new design / logo / AUP / avatar simply gets a new key.

    png(key, draw)          → bytes (drawn and stored on the first call)
    warm_in_background(fn)  → run the drawing after the response
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)
_MEMORY: OrderedDict[str, bytes] = OrderedDict()
_MEMORY_MAX = 48
_LOCK = threading.Lock()


def _remember(key: str, data: bytes) -> None:
    with _LOCK:
        _MEMORY[key] = data
        _MEMORY.move_to_end(key)
        while len(_MEMORY) > _MEMORY_MAX:
            _MEMORY.popitem(last=False)


def png(key: str, draw) -> bytes:
    from .models import CardImage

    with _LOCK:
        data = _MEMORY.get(key)
    if data is not None:
        return data
    row = CardImage.objects.filter(key=key).only("png").first()
    if row is not None:
        data = bytes(row.png)
        _remember(key, data)
        return data
    data = draw()
    try:
        CardImage.objects.get_or_create(key=key, defaults={"png": data})
    except Exception as exc:  # noqa: BLE001 — serving the picture matters more than keeping it
        logger.warning("card image cache: %s", exc)
    _remember(key, data)
    return data


def predraw(fn, *args) -> None:
    """Draw a picture now, before a Slack message that shows it is sent
    (skipped in tests — there the picture is drawn when it's asked for)."""
    from django.conf import settings

    if getattr(settings, "CARD_IMAGE_PREDRAW", True):
        fn(*args)


def warm_in_background(fn, *args) -> None:
    """Draw pictures ahead of time (after an approval, when the AUP is
    published …) without holding up the response."""
    from django.conf import settings

    def run():
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("card image warm-up failed: %s", exc)

    if not getattr(settings, "CARD_IMAGE_PREDRAW", True):
        return  # tests: pictures are drawn when they're asked for
    threading.Thread(target=run, daemon=True).start()

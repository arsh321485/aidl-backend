"""Django email backend that sends through SendGrid's HTTPS API (v3
mail/send) — used for the sign-up / sign-in codes. HTTPS instead of SMTP
means hosting that blocks mail ports (e.g. Render's free plan) still works.

Enabled automatically when SENDGRID_API_KEY is set (config/settings.py).
The From address (DEFAULT_FROM_EMAIL) must be a verified sender in SendGrid.
"""

from __future__ import annotations

import logging
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

SEND_URL = "https://api.sendgrid.com/v3/mail/send"


def _address(value: str) -> dict:
    name, email = parseaddr(value)
    return {"email": email, **({"name": name} if name else {})}


class SendGridBackend(BaseEmailBackend):
    def send_messages(self, email_messages) -> int:
        sent = 0
        for message in email_messages:
            content = [{"type": "text/plain", "value": message.body or " "}]
            for alt, mimetype in getattr(message, "alternatives", []) or []:
                if mimetype == "text/html":
                    content.append({"type": "text/html", "value": alt})
            payload = {
                "personalizations": [{"to": [_address(a) for a in message.to]}],
                "from": _address(message.from_email or settings.DEFAULT_FROM_EMAIL),
                "subject": message.subject,
                "content": content,
            }
            try:
                resp = requests.post(SEND_URL, json=payload, timeout=settings.EMAIL_TIMEOUT, headers={
                    "Authorization": f"Bearer {settings.SENDGRID_API_KEY}"})
                if resp.status_code >= 300:
                    raise RuntimeError(f"SendGrid {resp.status_code}: {resp.text[:300]}")
                sent += 1
            except Exception:
                logger.exception("SendGrid send failed")
                if not self.fail_silently:
                    raise
        return sent

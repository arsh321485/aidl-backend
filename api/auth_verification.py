"""Website sign-up / sign-in checks: a picture captcha (sign-in) and a
6-digit code emailed to the user (sign-up and sign-in).

Self-hosted on purpose — no third-party captcha keys needed. Only hashes of
the answers are stored (AuthChallenge), every challenge is single-use and
expires after 10 minutes, and a code allows 5 wrong tries.

Settings: AUTH_CAPTCHA / AUTH_EMAIL_OTP switch the checks on (default on);
the email goes out through Django's EMAIL_* settings. With no SMTP account
configured (console email backend) and DEBUG on, responses also carry
`dev_code` so the flow can be tested locally.
"""

from __future__ import annotations

import hashlib
import random
import secrets
from datetime import timedelta
from html import escape

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import AIDLUser, AuthChallenge

LIFETIME = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=30)
_CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


class ChallengeError(ValueError):
    pass


def _hash(token: str, answer: str) -> str:
    return hashlib.sha256(f"{token}:{answer.strip().upper()}".encode()).hexdigest()


def dev_mode() -> bool:
    return settings.DEBUG and settings.EMAIL_BACKEND.endswith("console.EmailBackend")


# ---------- captcha ----------

def _captcha_svg(text: str) -> str:
    rnd = random.SystemRandom()
    w, h = 170, 56
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
             f'<rect width="{w}" height="{h}" fill="#f5ecd2"/>']
    for _ in range(6):  # noise lines
        parts.append(f'<line x1="{rnd.randint(0, w)}" y1="{rnd.randint(0, h)}" x2="{rnd.randint(0, w)}" '
                     f'y2="{rnd.randint(0, h)}" stroke="#{rnd.choice(["14140f", "8a7f5c", "e8a400"])}" '
                     f'stroke-width="{rnd.choice([1, 1.5, 2])}" opacity=".6"/>')
    for _ in range(30):
        parts.append(f'<circle cx="{rnd.randint(0, w)}" cy="{rnd.randint(0, h)}" r="1" fill="#14140f" opacity=".5"/>')
    for i, ch in enumerate(text):
        x = 18 + i * 29 + rnd.randint(-3, 3)
        y = 38 + rnd.randint(-5, 5)
        parts.append(f'<text x="{x}" y="{y}" font-family="Courier New, monospace" font-weight="bold" '
                     f'font-size="{rnd.randint(26, 32)}" fill="#14140f" '
                     f'transform="rotate({rnd.randint(-25, 25)} {x} {y})">{escape(ch)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def new_captcha() -> dict:
    text = "".join(secrets.choice(_CAPTCHA_CHARS) for _ in range(5))
    token = secrets.token_urlsafe(24)
    AuthChallenge.objects.create(kind=AuthChallenge.Kind.CAPTCHA, token=token, answer_hash=_hash(token, text),
                                 expires_at=timezone.now() + LIFETIME)
    import base64

    svg = _captcha_svg(text)
    return {"captcha_token": token,
            "image": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()}


def check_captcha(token: str, answer: str) -> None:
    """Raises ChallengeError. A captcha can be tried once — a wrong answer
    needs a fresh picture."""
    row = AuthChallenge.objects.filter(kind=AuthChallenge.Kind.CAPTCHA, token=token or "", used_at__isnull=True).first()
    if row is None or row.expires_at < timezone.now():
        raise ChallengeError("The picture code expired — load a new one.")
    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])
    if row.answer_hash != _hash(token, answer or ""):
        raise ChallengeError("The picture code doesn't match — try the new one.")


# ---------- email code ----------

def _send_code(user: AIDLUser, code: str, purpose: str) -> None:
    action = "finish creating your AIDL account" if purpose == "signup" else "sign in to AIDL"
    send_mail(
        subject=f"Your AIDL code: {code}",
        message=(f"Hi {user.first_name or user.full_name or 'there'},\n\n"
                 f"Use this code to {action}:\n\n    {code}\n\n"
                 "It expires in 10 minutes. If you didn't ask for it, ignore this email.\n\n— AIDL"),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def start_otp(user: AIDLUser, purpose: str) -> dict:
    """Email a fresh code. Returns the response body for the website."""
    code = f"{secrets.randbelow(10**6):06d}"
    token = secrets.token_urlsafe(24)
    AuthChallenge.objects.create(kind=AuthChallenge.Kind.OTP, purpose=purpose, token=token, user_id=str(user.pk),
                                 answer_hash=_hash(token, code), expires_at=timezone.now() + LIFETIME)
    _send_code(user, code, purpose)
    name, _, domain = user.email.partition("@")
    body = {"otp_required": True, "otp_token": token, "email": f"{name[:2]}{'•' * max(len(name) - 2, 1)}@{domain}",
            "message": "We've emailed you a 6-digit code."}
    if dev_mode():
        body["dev_code"] = code
    return body


def verify_otp(token: str, code: str, purpose: str) -> AIDLUser:
    row = AuthChallenge.objects.filter(kind=AuthChallenge.Kind.OTP, purpose=purpose, token=token or "",
                                       used_at__isnull=True).first()
    if row is None or row.expires_at < timezone.now():
        raise ChallengeError("This code expired — ask for a new one.")
    if row.attempts >= MAX_ATTEMPTS:
        raise ChallengeError("Too many wrong tries — ask for a new code.")
    if row.answer_hash != _hash(token, code or ""):
        row.attempts += 1
        row.save(update_fields=["attempts"])
        left = MAX_ATTEMPTS - row.attempts
        raise ChallengeError(f"Wrong code — {left} tr{'y' if left == 1 else 'ies'} left." if left else
                             "Too many wrong tries — ask for a new code.")
    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])
    user = AIDLUser.objects.filter(pk=row.user_id).first()
    if user is None:
        raise ChallengeError("This account no longer exists.")
    return user


def resend_otp(token: str) -> dict:
    row = AuthChallenge.objects.filter(kind=AuthChallenge.Kind.OTP, token=token or "").first()
    if row is None:
        raise ChallengeError("Start again — this code request wasn't found.")
    if timezone.now() - row.created_at < RESEND_AFTER:
        raise ChallengeError("Wait a few seconds before asking for another code.")
    user = AIDLUser.objects.filter(pk=row.user_id).first()
    if user is None:
        raise ChallengeError("This account no longer exists.")
    row.used_at = row.used_at or timezone.now()  # the old code stops working
    row.save(update_fields=["used_at"])
    return start_otp(user, row.purpose)

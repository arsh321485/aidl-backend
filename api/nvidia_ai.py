"""NVIDIA hosted AI models (NVIDIA API Catalog / NIM), used instead of
OpenAI for text generation.

API (docs.api.nvidia.com/nim/reference/create_chat_completion_v1_chat_completions_post):
    POST {NVIDIA_BASE_URL}/chat/completions          default base https://integrate.api.nvidia.com/v1
    Authorization: Bearer <NVIDIA_API_KEY>           an "nvapi-…" key from build.nvidia.com
    {"model", "messages", "temperature", "top_p", "max_tokens", "stream": false, …}
    → {"choices": [{"message": {"content", "reasoning_content"?}, "finish_reason"}], "usage"}
The request schema rejects unknown fields, so only documented ones are sent
(plus NVIDIA_EXTRA_BODY, e.g. {"chat_template_kwargs": {"thinking": false}}
for models that think out loud). There is no JSON mode, so callers ask for
JSON in the prompt and parse it.

A long-running call may answer 202 with an NVCF-REQID header; the result is
then polled at {NVIDIA_STATUS_URL}/{request id} with the same key. 429 and
5xx are retried with back-off. The API key is never logged or returned.

Settings (env): NVIDIA_API_KEY, NVIDIA_MODEL (required), NVIDIA_BASE_URL,
NVIDIA_STATUS_URL, NVIDIA_TIMEOUT, NVIDIA_MAX_TOKENS, NVIDIA_TEMPERATURE,
NVIDIA_EXTRA_BODY (JSON object).
"""

from __future__ import annotations

import logging
import re
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class NvidiaError(Exception):
    """A generation failed. `message` is safe to show to an admin."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class NvidiaNotConfigured(NvidiaError):
    pass


def configured() -> bool:
    return bool(settings.NVIDIA_API_KEY and settings.NVIDIA_MODEL)


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.NVIDIA_API_KEY}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _error_text(resp: requests.Response) -> str:
    """NVIDIA's problem details (title / detail), never the request."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("title") or body.get("error") or "")[:200]
    return ""


def _poll(request_id: str, deadline: float) -> requests.Response:
    """202 → poll the NVCF status endpoint until the answer is ready."""
    url = f"{settings.NVIDIA_STATUS_URL.rstrip('/')}/{request_id}"
    while time.monotonic() < deadline:
        resp = requests.get(url, headers=_headers(), timeout=60)
        if resp.status_code != 202:
            return resp
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    raise NvidiaError("The AI model took too long to answer. Please try again.", retryable=True)


def chat(messages: list[dict], *, max_tokens: int | None = None, temperature: float | None = None) -> str:
    """One chat completion; returns the assistant's text (reasoning removed)."""
    if not configured():
        raise NvidiaNotConfigured("AI generation isn't set up yet (NVIDIA_API_KEY / NVIDIA_MODEL are missing).")
    body = {
        "model": settings.NVIDIA_MODEL,
        "messages": messages,
        "temperature": settings.NVIDIA_TEMPERATURE if temperature is None else temperature,
        "top_p": 1,
        "max_tokens": max_tokens or settings.NVIDIA_MAX_TOKENS,
        "stream": False,
        **settings.NVIDIA_EXTRA_BODY,
    }
    url = f"{settings.NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
    deadline = time.monotonic() + settings.NVIDIA_TIMEOUT
    last = ""
    for attempt in range(4):
        delay = 2 ** attempt * 2  # 2, 4, 8 s unless the server says otherwise
        try:
            resp = requests.post(url, headers=_headers(), json=body,
                                 timeout=max(10, deadline - time.monotonic()))
            if resp.status_code == 202 and resp.headers.get("NVCF-REQID"):
                resp = _poll(resp.headers["NVCF-REQID"], deadline)
        except requests.Timeout:
            raise NvidiaError("The AI model took too long to answer. Please try again.", retryable=True) from None
        except requests.RequestException as exc:
            last = type(exc).__name__
            logger.warning("NVIDIA request failed: %s", last)
        else:
            if resp.status_code == 200:
                return _content(resp)
            last = f"HTTP {resp.status_code} {_error_text(resp)}".strip()
            logger.warning("NVIDIA %s (model %s)", last, settings.NVIDIA_MODEL)
            if resp.status_code in (401, 403):
                raise NvidiaError("The NVIDIA API key was refused. Check NVIDIA_API_KEY.")
            if resp.status_code in (400, 404, 422):
                raise NvidiaError(f"The AI model rejected the request ({last}). Check NVIDIA_MODEL.")
            if resp.status_code != 429 and resp.status_code < 500:
                raise NvidiaError(f"The AI model returned an error ({last}).")
            wait = resp.headers.get("Retry-After", "")
            if wait.replace(".", "", 1).isdigit():
                delay = float(wait)
        if attempt == 3 or time.monotonic() + delay > deadline:
            break
        time.sleep(delay)
    raise NvidiaError(f"The AI model is busy or unavailable ({last or 'no answer'}). Please try again later.",
                      retryable=True)


def _content(resp: requests.Response) -> str:
    try:
        choice = resp.json()["choices"][0]
        text = choice["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise NvidiaError("The AI model sent an answer AIDL couldn't read.", retryable=True) from None
    if choice.get("finish_reason") == "length":
        logger.info("NVIDIA answer cut off at max_tokens=%s", settings.NVIDIA_MAX_TOKENS)
    return _THINK.sub("", text).strip()

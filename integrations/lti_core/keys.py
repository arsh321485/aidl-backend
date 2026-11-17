"""AIDL's own LTI key pair. The LMS asks for our public keys ("keyset URL")
when the tool is registered; later steps (sending grades back, reading the
class list) sign their requests with the private key.

Set LTI_PRIVATE_KEY in .env to a PEM private key (make one with
`python manage.py lti_new_key`). Without it, the keyset is empty: login
still works, the later steps won't.
"""

from __future__ import annotations

import hashlib
import json
import os

from cryptography.hazmat.primitives import serialization
from jwt.algorithms import RSAAlgorithm


def private_key_pem() -> str:
    return os.getenv("LTI_PRIVATE_KEY", "").replace("\\n", "\n").strip()


def public_jwks() -> dict:
    pem = private_key_pem()
    if not pem:
        return {"keys": []}
    private = serialization.load_pem_private_key(pem.encode(), password=None)
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": key_id(), "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def key_id() -> str:
    pem = private_key_pem()
    return hashlib.sha256(pem.encode()).hexdigest()[:16] if pem else ""

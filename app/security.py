from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


def new_token(bytes_len: int = 32) -> str:
    return secrets.token_urlsafe(bytes_len)


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{base64.urlsafe_b64encode(sig).decode('ascii')}"


def verify_payload(token: str, secret: str, max_age_seconds: int) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(sig.encode("ascii"))
        if not hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except Exception:
        return None
    issued_at = int(payload.get("iat", 0))
    if issued_at <= 0 or int(time.time()) - issued_at > max_age_seconds:
        return None
    return payload


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def now_ts() -> int:
    return int(time.time())

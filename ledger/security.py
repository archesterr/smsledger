"""Tokens, TOTP (RFC 6238), recovery codes and a small shared rate limiter."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache

TOKEN_PREFIX = "sml_"
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def new_token() -> str:
    """Device token. The prefix makes leaked tokens easy to grep for and to secret-scan."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_invite_code() -> str:
    return secrets.token_urlsafe(18)


# ---- TOTP -------------------------------------------------------------------
def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def hotp(key: bytes, counter: int, digits: int = 6) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)


def totp_verify(secret: str, code: str, last_step: int = 0, now: float | None = None) -> int | None:
    """Returns the matching 30 s time step (store it as last_step so a code can't be replayed),
    or None. Accepts one step of clock drift either way."""
    code = "".join((code or "").translate(_DIGITS).split())
    if len(code) != 6 or not code.isdigit() or not secret:
        return None
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    t = int((time.time() if now is None else now) // 30)
    for step in (t - 1, t, t + 1):
        if step > last_step and hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def totp_uri(secret: str, username: str, issuer: str) -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


# ---- recovery codes ---------------------------------------------------------
def new_recovery_codes(n: int = 10) -> list[str]:
    return [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(n)]


def recovery_hash(code: str) -> str:
    return sha256("".join(ch for ch in code.lower() if ch.isalnum()))


# ---- rate limiting -----------------------------------------------------------
class Limiter:
    """Fixed-window counter in the shared cache. Identifiers are hashed so the cache never holds
    usernames or IPs in clear."""

    def __init__(self, name: str, limit: int, window: int):
        self.name, self.limit, self.window = name, limit, window

    def _key(self, ident) -> str:
        return f"rl:{self.name}:{sha256(str(ident))[:32]}:{int(time.time() // self.window)}"

    def count(self, ident) -> int:
        return cache.get(self._key(ident), 0)

    def blocked(self, ident) -> bool:
        return self.count(ident) >= self.limit

    def hit(self, ident) -> int:
        key = self._key(ident)
        if cache.add(key, 1, self.window + 5):
            return 1
        try:
            return cache.incr(key)
        except ValueError:  # expired between add and incr
            cache.set(key, 1, self.window + 5)
            return 1

    def reset(self, ident) -> None:
        cache.delete(self._key(ident))


LOGIN_USER = Limiter("login-user", 10, 900)     # per username / 15 min
LOGIN_IP = Limiter("login-ip", 30, 900)         # per client IP / 15 min
TOTP_USER = Limiter("totp-user", 10, 900)
JOIN_IP = Limiter("join-ip", 20, 3600)
INGEST_BAD = Limiter("ingest-bad", 20, 60)      # failed token attempts per IP / min
INGEST_DEVICE = Limiter("ingest-dev", 120, 60)  # requests per device / min


def client_ip(request) -> str:
    if settings.TRUST_X_FORWARDED_FOR:
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")

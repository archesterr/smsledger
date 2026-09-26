"""Per-user encryption of financial data ("vault").

What is encrypted: amounts, balances, SMS text, titles, counterparties, notes, account, category
and rule names/amounts, budgets. What is not (the app needs it in SQL): who owns a row, when a
transaction happened, in/out, which of your categories/accounts it belongs to.

Keys, per user:
  DEK      32 random bytes. Encrypts every sealed row with AES-256-GCM. The row's table and
           owner are bound in as associated data, so a row can't be moved to another user.
  X25519   key pair. SMS that arrive while the owner is logged out (the phone has no key) are
           sealed to the public key and opened at the next login. The private key is stored
           encrypted under the DEK.
The DEK is stored only wrapped:
  key_pw   by Argon2id(password)            normal state ("protected")
  key_rec  by the one-time recovery key     the only way back after a forgotten password
  key_srv  in the clear                     accounts that existed before encryption, until the
                                            owner's next login ("unprotected"); then deleted
While logged in, the session row holds the DEK wrapped with a random secret that lives only in
the browser's cookie: a database dump alone has no usable key.

Limits (see README): the server necessarily sees each SMS when it arrives and a user's data while
they use the app. Someone who controls the server can change its code to capture that; this
protects the database, backups and admin screens, not against a malicious operator.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import os
import secrets
import struct

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

V1 = b"\x01"
NONCE = 12
# Argon2id for the password -> key step. Stored in each blob, so they can be raised later.
ARGON = (1, 8, 1) if getattr(settings, "TESTING", False) else (3, 64 * 1024, 2)  # time, memory KiB, lanes
_RAW = serialization.Encoding.Raw


class Locked(Exception):
    """This user's data key isn't available in this request (logged out, or password reset)."""


class BadKey(Exception):
    """Wrong password / recovery key, or tampered data."""


# ---- primitives -----------------------------------------------------------------------------
def _b(v) -> bytes:
    return bytes(v) if v is not None else b""  # BinaryField is memoryview on PostgreSQL


def encrypt(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE)
    return V1 + nonce + AESGCM(key).encrypt(nonce, data, aad)


def decrypt(key: bytes, blob, aad: bytes) -> bytes:
    blob = _b(blob)
    if blob[:1] != V1:
        raise BadKey("unknown format")
    try:
        return AESGCM(key).decrypt(blob[1:1 + NONCE], blob[1 + NONCE:], aad)
    except InvalidTag:
        raise BadKey("wrong key or tampered data") from None


def _hkdf(key: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(key)


def _argon(secret: str, salt: bytes, t: int, m: int, p: int) -> bytes:
    return hash_secret_raw(secret.encode(), salt, time_cost=t, memory_cost=m, parallelism=p,
                           hash_len=32, type=Type.ID)


# ---- wrapping the DEK -----------------------------------------------------------------------
def wrap_password(dek: bytes, password: str) -> bytes:
    t, m, p = ARGON
    salt = os.urandom(16)
    head = struct.pack(">BIB", t, m, p) + salt
    return V1 + head + encrypt(_argon(password, salt, t, m, p), dek, b"dek:pw")


def unwrap_password(blob, password: str) -> bytes:
    blob = _b(blob)
    if blob[:1] != V1 or len(blob) < 23:
        raise BadKey("no password key")
    t, m, p = struct.unpack(">BIB", blob[1:7])
    salt = blob[7:23]
    return decrypt(_argon(password, salt, t, m, p), blob[23:], b"dek:pw")


RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: read aloud or copied by hand


def new_recovery_key() -> str:
    """32 characters (~158 bits) in groups of 4: XXXX-XXXX-..."""
    s = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(32))
    return "-".join(s[i:i + 4] for i in range(0, 32, 4))


def normalize_recovery(code: str) -> str:
    from . import parsers  # Persian digits, stray spaces/dashes

    s = parsers.normalize(code or "").upper()
    return "".join(ch for ch in s if ch in RECOVERY_ALPHABET)


def wrap_recovery(dek: bytes, code: str) -> bytes:
    salt = os.urandom(16)
    return V1 + salt + encrypt(_hkdf(normalize_recovery(code).encode(), salt, b"smsledger recovery"), dek,
                               b"dek:rec")


def unwrap_recovery(blob, code: str) -> bytes:
    blob = _b(blob)
    if blob[:1] != V1 or len(blob) < 17:
        raise BadKey("no recovery key")
    return decrypt(_hkdf(normalize_recovery(code).encode(), blob[1:17], b"smsledger recovery"), blob[17:],
                   b"dek:rec")


# ---- rows -------------------------------------------------------------------------------------
def row_aad(label: str, user_id) -> bytes:
    return f"{label}:{user_id}".encode()


def seal_row(dek: bytes, label: str, user_id, data: dict) -> bytes:
    return encrypt(dek, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), row_aad(label, user_id))


def open_row(dek: bytes, label: str, user_id, blob) -> dict:
    return json.loads(decrypt(dek, blob, row_aad(label, user_id)))


# ---- inbox: SMS sealed to the public key -----------------------------------------------------
def seal_inbox(public: bytes, user_id, text: str) -> bytes:
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(_b(public)))
    key = _hkdf(shared, eph_pub + _b(public), b"smsledger inbox")
    return V1 + eph_pub + encrypt(key, text.encode(), f"inbox:{user_id}".encode())


def open_inbox(private: bytes, user_id, blob) -> str:
    blob = _b(blob)
    if blob[:1] != V1:
        raise BadKey("unknown format")
    priv = X25519PrivateKey.from_private_bytes(private)
    pub = priv.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)
    eph_pub = blob[1:33]
    key = _hkdf(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub + pub, b"smsledger inbox")
    return decrypt(key, blob[33:], f"inbox:{user_id}".encode()).decode()


# ---- per-user state -----------------------------------------------------------------------------
PROTECTED, UNPROTECTED, LOCKED, NONE = "protected", "unprotected", "locked", "none"


def state(user) -> str:
    if _b(user.key_srv):
        return UNPROTECTED
    if _b(user.key_pw):
        return PROTECTED
    return LOCKED if _b(user.vault_pub) else NONE


def new_keys(user) -> bytes:
    """Fresh DEK + key pair on `user` (not saved). Returns the DEK; wrap it before saving."""
    dek = os.urandom(32)
    priv = X25519PrivateKey.generate()
    user.vault_pub = priv.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)
    user.vault_priv = encrypt(dek, priv.private_bytes(_RAW, serialization.PrivateFormat.Raw,
                                                      serialization.NoEncryption()), b"priv")
    user.key_pw = user.key_rec = user.key_srv = b""
    if not _b(user.dedupe_key):
        user.dedupe_key = os.urandom(32)
    return dek


def private_key(user, dek: bytes) -> bytes:
    return decrypt(dek, user.vault_priv, b"priv")


def protect(user, dek: bytes, password: str) -> None:
    """Wrap the DEK with the password and forget the server-readable copy (not saved)."""
    user.key_pw = wrap_password(dek, password)
    user.key_srv = b""


def unlock_password(user, password: str) -> bytes | None:
    """The DEK for a correct password, or None when the account is locked (password was reset)."""
    if _b(user.key_srv):
        return _b(user.key_srv)
    if not _b(user.key_pw):
        return None
    try:
        return unwrap_password(user.key_pw, password)
    except BadKey:
        return None  # password changed outside the app (admin, manage.py): needs the recovery key


def password_set(user, raw: str) -> None:
    """Called from User.set_password: keep the DEK wrapped with the current password."""
    if not _b(user.vault_pub):
        user._new_dek = new_keys(user)
        protect(user, user._new_dek, raw)
        return
    if _b(user.key_srv):
        protect(user, _b(user.key_srv), raw)
        return
    if not _b(user.key_pw):
        return  # already locked: only the recovery key helps
    try:
        unwrap_password(user.key_pw, raw)
        return  # same password (Django re-hashing it): nothing to do
    except BadKey:
        pass
    dek = keyring().get(user.pk)
    if dek:
        user.key_pw = wrap_password(dek, raw)
    else:  # set by someone without the key (admin reset, changepassword): data stays locked
        user.key_pw = b""
        user._vault_locked_by_reset = True


def fingerprint(user, raw: str) -> str:
    """Per-user keyed hash of the normalized SMS: dedupe without storing a plain hash anyone
    could test guesses against."""
    from . import parsers

    return hmac.new(_b(user.dedupe_key), parsers.normalize(raw).encode(), hashlib.sha256).hexdigest()


def index_key(dek: bytes) -> bytes:
    return _hkdf(dek, b"", b"smsledger index")


def blind(dek: bytes, value: str) -> str:
    """Lookup value for an encrypted field (e.g. account number) without revealing it."""
    return hmac.new(index_key(dek), value.encode(), hashlib.sha256).hexdigest()[:32]


# ---- keyring: which users' keys this request / job holds -------------------------------------
_keyring: contextvars.ContextVar[dict | None] = contextvars.ContextVar("vault_keyring", default=None)


def keyring() -> dict:
    ring = _keyring.get()
    if ring is None:
        ring = {}
        _keyring.set(ring)
    return ring


def fresh_keyring():
    """Start an empty keyring (one per request). Returns the token for reset_keyring."""
    return _keyring.set({})


def reset_keyring(token) -> None:
    _keyring.reset(token)


def key_for(user_id) -> bytes:
    """The DEK to read/write this user's rows. Raises Locked if this request doesn't have it."""
    ring = keyring()
    dek = ring.get(user_id)
    if dek is None:
        from .models import User

        srv = User.objects.filter(pk=user_id).values_list("key_srv", flat=True).first()
        if not _b(srv):
            raise Locked(user_id)
        dek = ring[user_id] = _b(srv)
    return dek


def has_key(user_id) -> bool:
    try:
        key_for(user_id)
        return True
    except Locked:
        return False


# ---- the session half of the key ------------------------------------------------------------------
SESSION_KEY = "vault"


def cookie_name() -> str:
    return "vault" if settings.DEBUG else "__Host-vault"


def session_parts(user, dek: bytes) -> tuple[str, str]:
    """(value for the session row, value for the cookie)."""
    secret = os.urandom(32)
    return (base64.b64encode(encrypt(secret, dek, f"session:{user.pk}".encode())).decode(),
            base64.urlsafe_b64encode(secret).decode())


def start_session(request, user, dek: bytes) -> None:
    """After login: the session row gets the DEK wrapped with a secret only the cookie holds."""
    request.session[SESSION_KEY], request._vault_cookie = session_parts(user, dek)
    keyring()[user.pk] = dek


def session_key(request, user) -> bytes | None:
    wrapped = request.session.get(SESSION_KEY)
    cookie = request.COOKIES.get(cookie_name()) or getattr(request, "_vault_cookie", None)
    if not wrapped or not cookie:
        return None
    try:
        secret = base64.urlsafe_b64decode(cookie)
        return decrypt(secret, base64.b64decode(wrapped), f"session:{user.pk}".encode())
    except (BadKey, ValueError, TypeError):
        return None

"""Bank SMS parsers. Add a new bank = subclass BankParser + append to PARSERS.

Pure Python on purpose (no Django imports): easy to test with real samples.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from . import jalali

# Real tz rules, not a fixed +03:30: Iran used DST until 2022, and old SMS get backfilled.
TEHRAN = jalali.TEHRAN

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CHARS = str.maketrans({"ي": "ی", "ك": "ک", "٬": ",", "：": ":"})
_INVISIBLE = re.compile(r"[‌‍‎‏؜‪-‮⁦-⁩﻿]")

# One-time passwords and login codes must never be stored or forwarded. Dynamic-password SMS
# usually contain the amount in ریال, so the Shortcut's "contains ریال" filter doesn't stop them.
# Kept deliberately broad: a dropped real transaction still shows up as a balance gap.
# Not bare "پویا": it's a common first name ("انتقال به پویا ..."); "رمز پویا" is caught by رمز.
_SENSITIVE = re.compile(
    r"رمز(?!\s*ارز)|یک\s*بار\s*مصرف"
    r"|کد\s*(?:تایید|تأیید|ورود|فعال\s*سازی|امنیتی|یک\s*بار|محرمانه|پویا)"
    r"|\b(?:otp|password|passcode|pin|cvv2?|verification|login code)\b",
    re.I,
)


def normalize(text: str) -> str:
    text = _INVISIBLE.sub("", text).translate(_DIGITS).translate(_CHARS)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(normalize(text)))


def _int(s: str | None) -> int | None:
    return int(s.replace(",", "")) if s else None


@dataclass
class Tx:
    bank: str
    direction: str                  # IN | OUT
    amount: int                     # rial, always positive
    balance: int | None             # balance after this transaction, if the SMS has it
    occurred_at: datetime | None    # Tehran-aware
    title: str = ""                 # the bank's own label, e.g. "برداشت پول"
    account: str = ""               # masked account/card number if the bank sends one
    counterparty: str = ""          # other side of a transfer, if the bank sends it

    def as_json(self) -> dict:
        return {
            "bank": self.bank, "direction": self.direction, "amount": self.amount, "balance": self.balance,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "title": self.title, "account": self.account, "counterparty": self.counterparty,
        }


class ParseError(Exception):
    pass


class BankParser:
    name = "base"
    label = "Bank"

    def match(self, text: str) -> bool:
        raise NotImplementedError

    def parse(self, text: str) -> Tx:
        raise NotImplementedError


# ---- shared helpers -------------------------------------------------------
RE_DATE = re.compile(r"(1[34]\d{2})[./\-](\d{1,2})[./\-](\d{1,2})")
RE_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")

OUT_WORDS = ("برداشت", "پرید", "خرید", "کسر", "انتقال از", "از حساب شما")
IN_WORDS = ("واریز", "نشست", "دریافت", "به حساب شما")


def parse_datetime(text: str) -> datetime | None:
    d = RE_DATE.search(text)
    if not d or not jalali.is_valid(*map(int, d.groups())):
        return None
    gy, gm, gd = jalali.to_gregorian(*map(int, d.groups()))
    t = RE_TIME.search(text)
    hh, mm = (int(t.group(1)), int(t.group(2))) if t else (0, 0)
    try:
        return datetime(gy, gm, gd, hh, mm, tzinfo=TEHRAN)
    except ValueError:  # 25:99 in a malformed SMS
        return datetime(gy, gm, gd, tzinfo=TEHRAN)


def detect_direction(title: str, body: str) -> str | None:
    # title is authoritative; body phrases are the fallback
    for src in (title, body):
        if any(w in src for w in OUT_WORDS):
            return "OUT"
        if any(w in src for w in IN_WORDS):
            return "IN"
    return None


# ---- Blu Bank -------------------------------------------------------------
class BluParser(BankParser):
    """
    بلو
    برداشت پول
    آرمین عزیز، 1,000,000 ریال از حساب شما پرید.
    موجودی: 2,887,139 ریال
    ۱۴:۰۳
    ۱۴۰۵.۰۶.۳۱
    """
    name = "blu"
    label = "بلو"
    RE_AMOUNT = re.compile(r"([\d,]+)\s*ریال\s*(?:از|به)\s*حساب")
    RE_BALANCE = re.compile(r"موجودی\s*:?\s*(-?[\d,]+)")

    def match(self, text: str) -> bool:
        first = text.split("\n", 1)[0]
        return first.strip() in ("بلو", "blu", "Blu") or first.startswith("بلو")

    def parse(self, text: str) -> Tx:
        lines = text.split("\n")
        title = lines[1] if len(lines) > 1 else ""
        body = "\n".join(lines[2:])

        m = self.RE_AMOUNT.search(body)
        if not m:
            raise ParseError("amount not found")
        amount = _int(m.group(1))
        if not amount:
            raise ParseError("zero amount")
        direction = detect_direction(title, body)
        if not direction:
            raise ParseError("direction unknown")
        b = self.RE_BALANCE.search(body)
        return Tx(
            bank=self.name,
            direction=direction,
            amount=amount,
            balance=_int(b.group(1)) if b else None,
            occurred_at=parse_datetime(body),
            title=title[:100],
        )


PARSERS: list[BankParser] = [BluParser()]
BANK_LABELS = {p.name: p.label for p in PARSERS}


def parse(raw: str) -> tuple[Tx | None, str | None]:
    """Returns (tx, error). Never raises: unknown/broken SMS still gets stored as unparsed."""
    text = normalize(raw)
    for p in PARSERS:
        if p.match(text):
            try:
                return p.parse(text), None
            except ParseError as e:
                return None, f"{p.name}: {e}"
    return None, "no parser matched"

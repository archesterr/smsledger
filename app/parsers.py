"""Bank SMS parsers. Add a new bank = subclass BankParser + append to PARSERS."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from . import jalali

TEHRAN = timezone(timedelta(hours=3, minutes=30))  # Iran dropped DST in 2022 -> fixed offset

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CHARS = str.maketrans({"ي": "ی", "ك": "ک", "٬": ",", "：": ":"})
_INVISIBLE = re.compile(r"[‌‍‎‏؜‪-‮⁦-⁩﻿]")


def normalize(text: str) -> str:
    text = _INVISIBLE.sub("", text).translate(_DIGITS).translate(_CHARS)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.replace("\r", "").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def _int(s: str | None) -> int | None:
    return int(s.replace(",", "")) if s else None


@dataclass
class Tx:
    bank: str
    direction: str            # IN | OUT
    amount: int               # rial, always positive
    balance: int | None
    occurred_at: str | None   # ISO8601, Tehran offset
    jdate: str | None         # Jalali YYYY-MM-DD as printed in the SMS
    title: str | None

    def dict(self):
        return asdict(self)


class ParseError(Exception):
    pass


class BankParser:
    name = "base"

    def match(self, text: str) -> bool:
        raise NotImplementedError

    def parse(self, text: str) -> Tx:
        raise NotImplementedError


# ---- shared helpers -------------------------------------------------------
RE_DATE = re.compile(r"(1[34]\d{2})[./\-](\d{1,2})[./\-](\d{1,2})")
RE_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")

OUT_WORDS = ("برداشت", "پرید", "خرید", "کسر", "انتقال از", "از حساب شما")
IN_WORDS = ("واریز", "نشست", "دریافت", "به حساب شما")


def parse_datetime(text: str) -> str | None:
    d = RE_DATE.search(text)
    if not d:
        return None
    gy, gm, gd = jalali.to_gregorian(*map(int, d.groups()))
    t = RE_TIME.search(text)
    hh, mm = (int(t.group(1)), int(t.group(2))) if t else (0, 0)
    return datetime(gy, gm, gd, hh, mm, tzinfo=TEHRAN).isoformat()


def parse_jdate(text: str) -> str | None:
    d = RE_DATE.search(text)
    return "%04d-%02d-%02d" % tuple(map(int, d.groups())) if d else None


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
        direction = detect_direction(title, body)
        if not direction:
            raise ParseError("direction unknown")
        b = self.RE_BALANCE.search(body)
        return Tx(
            bank=self.name,
            direction=direction,
            amount=_int(m.group(1)),
            balance=_int(b.group(1)) if b else None,
            occurred_at=parse_datetime(body),
            jdate=parse_jdate(body),
            title=title or None,
        )


PARSERS: list[BankParser] = [BluParser()]


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

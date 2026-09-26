"""Bank SMS parsers. Add a new bank = subclass BankParser + append to PARSERS.

Pure Python on purpose (no Django imports): easy to test with real samples.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import jalali

# Changes whenever this file does: each user's unparsed SMS are re-read once with the new parsers
# (at their next request with their key; see middleware). Nothing else depends on it.
VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]

# Real tz rules, not a fixed +03:30: Iran used DST until 2022, and old SMS get backfilled.
TEHRAN = jalali.TEHRAN

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CHARS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "٬": ",", "：": ":"})
_INVISIBLE = re.compile(r"[‌‍‎‏؜‪-‮⁦-⁩﻿]")

# One-time passwords and login codes must never be stored or forwarded. Dynamic-password SMS
# usually contain the amount, so the Shortcut's "contains موجودی/مانده" filter doesn't stop them.
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
    # splitlines, not split("\n"): copied text may break lines with \r or U+2028
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
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

    def sniff(self, text: str) -> bool:
        """Fallback when no match(): the bank's name line is sometimes only the sender, not in
        the body (a copied SMS, some phones). Recognise the bank by its layout instead."""
        return False

    def parse(self, text: str, ref: datetime | None = None) -> Tx:
        """ref: when the SMS was received; banks that send no year get it from here."""
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


def infer_datetime(month: int, day: int, hh: int = 0, mm: int = 0, ref: datetime | None = None) -> datetime | None:
    """For SMS that carry month/day but no year: the latest such date not after the day the SMS
    arrived (so a 12/29 SMS read on 1/02 belongs to last year)."""
    ref = (ref or datetime.now(TEHRAN)).astimezone(TEHRAN)
    this_year = jalali.from_date(ref.date())[0]
    for jy in (this_year, this_year - 1):
        if not jalali.is_valid(jy, month, day):
            continue
        gy, gm, gd = jalali.to_gregorian(jy, month, day)
        try:
            dt = datetime(gy, gm, gd, hh, mm, tzinfo=TEHRAN)
        except ValueError:
            dt = datetime(gy, gm, gd, tzinfo=TEHRAN)
        if dt <= ref + timedelta(days=1):  # a day of slack for clock skew
            return dt
    return None


# "-4,450,000", "+500,000", "80,035,500-" (sign before or after, or none)
RE_SIGNED = re.compile(r"^([-+]?)\s*([\d,]*\d)\s*([-+]?)$")
RE_BALANCE = re.compile(r"مانده\s*:?\s*(-?[\d,]+)")


def signed_amount(s: str) -> tuple[int, str | None] | None:
    """(amount, "OUT"/"IN"/None) from a signed number, or None if s isn't one."""
    m = RE_SIGNED.match(s.strip())
    if not m or (m.group(1) and m.group(3)):
        return None
    sign = m.group(1) or m.group(3)
    return _int(m.group(2)), {"-": "OUT", "+": "IN"}.get(sign)


def last4(account: str) -> str:
    """Account numbers become a short hint: enough to tell two accounts apart, not the full number."""
    digits = re.sub(r"\D", "", account)
    return digits[-4:]


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

    def sniff(self, text: str) -> bool:
        return bool(re.search(r"ریال\s*(?:از|به)\s*حساب\s*شما\s*(?:پرید|نشست)", text))

    def parse(self, text: str, ref: datetime | None = None) -> Tx:
        lines = text.split("\n")
        if self.match(text):
            lines = lines[1:]
        title = lines[0] if lines else ""
        body = "\n".join(lines[1:])

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


def _require(amount: int | None, direction: str | None) -> None:
    if not amount:
        raise ParseError("amount not found")
    if not direction:
        raise ParseError("direction unknown")


def _balance(text: str) -> int | None:
    b = RE_BALANCE.search(text)
    return _int(b.group(1)) if b else None


# ---- Saman ------------------------------------------------------------------
class SamanParser(BankParser):
    """
    بانك سامان
    برداشت مبلغ 20,000,000 انتقال وجه
    از 884-800-4076959-1
    مانده 36,634,778
    1405/7/1
    18:50:02
    """
    name = "saman"
    label = "سامان"
    RE_AMOUNT = re.compile(r"^(برداشت|واریز)\s*مبلغ\s*([\d,]+)\s*(.*)$", re.M)
    RE_ACCOUNT = re.compile(r"^(?:از|به)\s*([\d-]{6,})\s*$", re.M)

    def match(self, text: str) -> bool:
        return text.startswith("بانک سامان")

    def parse(self, text: str, ref: datetime | None = None) -> Tx:
        m = self.RE_AMOUNT.search(text)
        if not m:
            raise ParseError("amount not found")
        kind, amount, reason = m.group(1), _int(m.group(2)), m.group(3).strip()
        direction = "OUT" if kind == "برداشت" else "IN"
        _require(amount, direction)
        acc = self.RE_ACCOUNT.search(text)
        return Tx(bank=self.name, direction=direction, amount=amount, balance=_balance(text),
                  occurred_at=parse_datetime(text), title=(reason or kind)[:100],
                  account=last4(acc.group(1)) if acc else "")


# ---- Middle East Bank (خاورمیانه) ------------------------------------------------
class KhavarmianehParser(BankParser):
    """
    بانک خاورمیانه
    خرید با کارت 0947
    -4,450,000
    020/000790644
    مانده 63,295,053
    06/31          (month/day, no year)
    20:35
    """
    name = "khavarmianeh"
    label = "خاورمیانه"
    RE_ACCOUNT = re.compile(r"^(\d{2,4}/\d{5,})$", re.M)
    RE_MD = re.compile(r"^(\d{1,2})/(\d{1,2})$", re.M)
    RE_HM = re.compile(r"^(\d{1,2}):(\d{2})$", re.M)

    def match(self, text: str) -> bool:
        return text.startswith("بانک خاورمیانه")

    def parse(self, text: str, ref: datetime | None = None) -> Tx:
        lines = text.split("\n")
        title = re.sub(r"\s*\d{4}$", "", lines[1]) if len(lines) > 1 else ""  # drop the card number
        amount = direction = None
        for ln in lines[2:]:
            sa = signed_amount(ln)
            if sa:
                amount, direction = sa
                break
        direction = direction or detect_direction(title, "")
        _require(amount, direction)
        acc = self.RE_ACCOUNT.search(text)
        md, hm = self.RE_MD.search(text), self.RE_HM.search(text)
        when = None
        if md:
            h, mi = (int(hm.group(1)), int(hm.group(2))) if hm else (0, 0)
            when = infer_datetime(int(md.group(1)), int(md.group(2)), h, mi, ref)
        return Tx(bank=self.name, direction=direction, amount=amount, balance=_balance(text),
                  occurred_at=when, title=title[:100], account=last4(acc.group(1)) if acc else "")


# ---- Pasargad -------------------------------------------------------------------
class PasargadParser(BankParser):
    """No bank name in the SMS: recognised by its account number format on the first line.
    777.888.19516768.1
    -3,200,000
    07/01_16:55     (month/day_time, no year)
    مانده: 1,178,259
    """
    name = "pasargad"
    label = "پاسارگاد"
    RE_FIRST = re.compile(r"^\d{2,4}\.\d{2,4}\.\d{4,12}\.\d{1,2}$")
    RE_WHEN = re.compile(r"^(\d{1,2})/(\d{1,2})[_ ](\d{1,2}):(\d{2})$", re.M)

    def match(self, text: str) -> bool:
        return bool(self.RE_FIRST.match(text.split("\n", 1)[0]))

    def parse(self, text: str, ref: datetime | None = None) -> Tx:
        lines = text.split("\n")
        sa = signed_amount(lines[1]) if len(lines) > 1 else None
        if not sa:
            raise ParseError("amount not found")
        amount, direction = sa
        _require(amount, direction)
        w = self.RE_WHEN.search(text)
        when = infer_datetime(*map(int, w.groups()), ref=ref) if w else None
        return Tx(bank=self.name, direction=direction, amount=amount, balance=_balance(text),
                  occurred_at=when, title="برداشت" if direction == "OUT" else "واریز",
                  account=last4(lines[0]))


# ---- Melli ----------------------------------------------------------------------
class MelliParser(BankParser):
    """
    بانك ملي ايران
    انتقال:80,035,500-
    حساب:97007
    مانده:35,206,324
    0629-18:26      (MMDD-time, no year)
    """
    name = "melli"
    label = "ملی"
    RE_LINE = re.compile(r"^([^\d:]+?)\s*:\s*([-+]?\s*[\d,]+\s*[-+]?)$")
    RE_ACCOUNT = re.compile(r"^حساب\s*:\s*(\d+)", re.M)
    RE_WHEN = re.compile(r"^(\d{2})(\d{2})-(\d{1,2}):(\d{2})$", re.M)
    NOT_AMOUNT = ("حساب", "مانده", "کارت")

    def match(self, text: str) -> bool:
        return text.startswith("بانک ملی")

    def sniff(self, text: str) -> bool:
        return bool(self.RE_WHEN.search(text)) and bool(re.search(r"^مانده\s*:", text, re.M))

    def parse(self, text: str, ref: datetime | None = None) -> Tx:
        title = amount = direction = None
        for ln in text.split("\n")[1 if self.match(text) else 0:]:
            m = self.RE_LINE.match(ln)
            if m and not m.group(1).startswith(self.NOT_AMOUNT):
                sa = signed_amount(m.group(2))
                if sa:
                    title, (amount, direction) = m.group(1).strip(), sa
                    break
        direction = direction or detect_direction(title or "", "")
        _require(amount, direction)
        acc = self.RE_ACCOUNT.search(text)
        w = self.RE_WHEN.search(text)
        when = infer_datetime(*map(int, w.groups()), ref=ref) if w else None
        return Tx(bank=self.name, direction=direction, amount=amount, balance=_balance(text),
                  occurred_at=when, title=(title or "")[:100], account=last4(acc.group(1)) if acc else "")


PARSERS: list[BankParser] = [BluParser(), SamanParser(), KhavarmianehParser(), PasargadParser(), MelliParser()]
BANK_LABELS = {p.name: p.label for p in PARSERS}


def parse(raw: str, ref: datetime | None = None) -> tuple[Tx | None, str | None]:
    """Returns (tx, error). Never raises: unknown/broken SMS still gets stored as unparsed.
    ref = when the SMS was received (default now); used by banks that send no year."""
    text = normalize(raw)
    p = next((p for p in PARSERS if p.match(text)), None)
    if not p and not text.startswith("بانک"):  # a named bank we don't know is not sniffed as another
        p = next((p for p in PARSERS if p.sniff(text)), None)
    if not p:
        return None, "no parser matched"
    try:
        return p.parse(text, ref), None
    except ParseError as e:
        return None, f"{p.name}: {e}"

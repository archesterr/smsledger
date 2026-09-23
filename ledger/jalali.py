"""Jalali (Solar Hijri) <-> Gregorian conversion. Arithmetic algorithm (jdf.scr.ir), valid 1300-1500."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
          "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_RE_DATE = re.compile(r"^\s*(1[34]\d{2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*$")


def to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186

    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365

    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    month_days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 12 and gd > month_days[gm]:
        gd -= month_days[gm]
        gm += 1
    return gy, gm + 1, gd


def from_gregorian(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def to_date(jy: int, jm: int, jd: int) -> date:
    return date(*to_gregorian(jy, jm, jd))


def from_date(d: date) -> tuple[int, int, int]:
    return from_gregorian(d.year, d.month, d.day)


def month_length(jy: int, jm: int) -> int:
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return (to_date(jy + 1, 1, 1) - to_date(jy, 12, 1)).days


def is_valid(jy: int, jm: int, jd: int) -> bool:
    """to_gregorian() silently rolls over out-of-range input (1405/13/40 -> 2027): check first."""
    return 1300 <= jy < 1500 and 1 <= jm <= 12 and 1 <= jd <= month_length(jy, jm)


def add_months(jy: int, jm: int, n: int) -> tuple[int, int]:
    i = jy * 12 + (jm - 1) + n
    return i // 12, i % 12 + 1


def month_range(jy: int, jm: int) -> tuple[datetime, datetime]:
    """[start, end) of a Jalali month as Tehran-aware datetimes."""
    ny, nm = add_months(jy, jm, 1)
    start = datetime.combine(to_date(jy, jm, 1), datetime.min.time(), TEHRAN)
    end = datetime.combine(to_date(ny, nm, 1), datetime.min.time(), TEHRAN)
    return start, end


def day_start(jy: int, jm: int, jd: int) -> datetime:
    return datetime.combine(to_date(jy, jm, jd), datetime.min.time(), TEHRAN)


def today() -> tuple[int, int, int]:
    return from_date(datetime.now(TEHRAN).date())


def of(dt: datetime) -> tuple[int, int, int]:
    """Jalali date of an aware datetime, in Tehran time."""
    return from_date(dt.astimezone(TEHRAN).date())


def parse(s: str) -> tuple[int, int, int]:
    """'1405/07/01', '۱۴۰۵-۷-۱' or '1405.07.01' -> (1405, 7, 1). Raises ValueError."""
    m = _RE_DATE.match((s or "").translate(_DIGITS))
    if not m:
        raise ValueError("bad jalali date")
    jy, jm, jd = map(int, m.groups())
    if not is_valid(jy, jm, jd):
        raise ValueError("bad jalali date")
    return jy, jm, jd


def parse_month(s: str) -> tuple[int, int]:
    """'1405-07' -> (1405, 7). Raises ValueError."""
    m = re.match(r"^\s*(1[34]\d{2})[/\-](\d{1,2})\s*$", (s or "").translate(_DIGITS))
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError("bad jalali month")
    return int(m.group(1)), int(m.group(2))


def fmt(jy: int, jm: int, jd: int, sep: str = "/") -> str:
    return f"{jy:04d}{sep}{jm:02d}{sep}{jd:02d}"


def month_title(jy: int, jm: int) -> str:
    return f"{MONTHS[jm - 1]} {jy}"


def shift_days(jy: int, jm: int, jd: int, n: int) -> tuple[int, int, int]:
    return from_date(to_date(jy, jm, jd) + timedelta(days=n))

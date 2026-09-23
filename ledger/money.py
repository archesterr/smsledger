"""Rial is the storage unit. Users see and type amounts in their own unit (toman by default)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

TO_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫", "01234567890123456789.")
UNIT_LABELS = {"toman": "تومان", "rial": "ریال"}
MAX_RIAL = 10 ** 17  # far above any real amount, far below BigInteger overflow


def fa_digits(value) -> str:
    return str(value).translate(TO_FA)


def format_amount(rial: int | None, unit: str = "toman", sign: bool = False) -> str:
    """1234567 rial -> '۱۲۳٬۴۵۶٫۷' toman. Minus is U+2212 so it isn't taken for a hyphen."""
    if rial is None:
        return ""
    v = abs(int(rial))
    if unit == "toman":
        whole, frac = divmod(v, 10)
        s = f"{whole:,}".replace(",", "٬") + (f"٫{frac}" if frac else "")
    else:
        s = f"{v:,}".replace(",", "٬")
    prefix = "−" if rial < 0 else ("+" if sign and rial > 0 else "")
    return prefix + s.translate(TO_FA)


def format_input(rial: int | None, unit: str = "toman") -> str:
    """Value for an <input>: plain digits, no separators."""
    if rial is None:
        return ""
    if unit == "toman":
        whole, frac = divmod(int(rial), 10)
        return f"{whole}.{frac}" if frac else str(whole)
    return str(int(rial))


def parse_amount(text: str, unit: str = "toman", allow_zero: bool = False) -> int:
    """'۱٬۲۰۰٬۰۰۰' / '1,200,000' / '1200000.5' (in `unit`) -> rial. Raises ValueError."""
    s = re.sub(r"[\s,٬_']", "", (text or "").translate(TO_EN))
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError("not a number") from None
    if not d.is_finite():  # "NaN", "Infinity"
        raise ValueError("not a number")
    rial = d * 10 if unit == "toman" else d
    if rial != rial.to_integral_value():
        raise ValueError("too many decimals")
    rial = int(rial)
    if rial < 0 or (rial == 0 and not allow_zero) or rial > MAX_RIAL:
        raise ValueError("out of range")
    return rial

from django import template
from django.templatetags.static import static
from django.utils import timezone
from django.utils.html import format_html

from .. import banks, jalali, money

register = template.Library()
WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")  # by date.weekday()


@register.filter
def fa(value):
    return money.fa_digits(value)


@register.filter
def amount(rial, unit="toman"):
    return money.format_amount(rial, unit)


@register.filter
def absval(value):
    return abs(value) if value is not None else value


@register.filter
def signed(tx, unit="toman"):
    return money.format_amount(tx.delta, unit, sign=True)


@register.filter
def pct(value):
    """37.0 -> '۳۷٪', 12.5 -> '۱۲٫۵٪'"""
    v = round(float(value or 0), 1)
    s = str(int(v)) if v == int(v) else str(v).replace(".", "٫")
    return money.fa_digits(s) + "٪"


@register.filter
def unit_label(unit):
    return money.UNIT_LABELS.get(unit, unit)


@register.filter
def jdate(dt):
    if not dt:
        return ""
    return money.fa_digits(jalali.fmt(*jalali.of(dt)))


@register.filter
def jtime(dt):
    return money.fa_digits(timezone.localtime(dt).strftime("%H:%M")) if dt else ""


@register.filter
def jdatetime(dt):
    return f"{jdate(dt)} · {jtime(dt)}" if dt else ""


@register.filter
def jday(dt):
    """'شنبه ۵ مهر ۱۴۰۵'"""
    if not dt:
        return ""
    jy, jm, jd = jalali.of(dt)
    wd = WEEKDAYS[timezone.localtime(dt).weekday()]
    return money.fa_digits(f"{wd} {jd} {jalali.MONTHS[jm - 1]} {jy}")


@register.simple_tag
def month_title(jy, jm):
    return money.fa_digits(jalali.month_title(jy, jm))


@register.filter
def ago(dt):
    if not dt:
        return "هرگز"
    s = int((timezone.now() - dt).total_seconds())
    if s < 60:
        return "همین حالا"
    for size, name in ((86400, "روز"), (3600, "ساعت"), (60, "دقیقه")):
        if s >= size:
            return money.fa_digits(f"{s // size} {name} پیش")
    return ""


@register.simple_tag
def bank_logo(account_or_key, size=""):
    """Bank logo tile (white, ringed in the bank's colour). Decorative: the account name is always
    next to it. Empty for cash / no-brand accounts. size: "" (badge), "md" (list icon), "lg"."""
    b = banks.get(account_or_key) if isinstance(account_or_key, str) else getattr(account_or_key, "bank_info", None)
    if not b:
        return ""
    return format_html(
        '<span class="bank bk-{}{}" title="{}"><img src="{}" alt="" width="48" height="48" decoding="async"></span>',
        b.key, f" {size}" if size else "", b.label, static(f"ledger/banks/{b.key}.svg"),
    )

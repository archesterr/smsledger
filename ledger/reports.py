"""Monthly numbers. Transfers between the user's own accounts are excluded everywhere.

Amounts are encrypted (vault.py), so sums happen here in Python over the user's decrypted rows,
not in SQL. SQL still narrows by user, date and category, which stay in the clear."""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Max

from . import jalali
from .models import IN, OUT, Account, Category, Transaction


def counted(user):
    """Transactions that count as real income/expense (not own-account transfers)."""
    return Transaction.objects.filter(user=user).exclude(category__kind=Category.TRANSFER)


def month_key(jy: int, jm: int) -> str:
    return f"{jy:04d}-{jm:02d}"


def _month(user, jy: int, jm: int) -> list[Transaction]:
    return list(counted(user).filter(jdate__startswith=month_key(jy, jm)))


def month_totals(user, jy: int, jm: int) -> dict:
    txs = _month(user, jy, jm)
    income = sum(t.amount for t in txs if t.direction == IN)
    expense = sum(t.amount for t in txs if t.direction == OUT)
    return {"income": income, "expense": expense, "net": income - expense, "count": len(txs)}


def by_category(user, jy: int, jm: int, direction: str = OUT) -> list[dict]:
    totals, counts = defaultdict(int), defaultdict(int)
    for t in _month(user, jy, jm):
        if t.direction == direction:
            totals[t.category_id] += t.amount
            counts[t.category_id] += 1
    cats = Category.objects.in_bulk([c for c in totals if c])
    total = sum(totals.values()) or 1
    rows = sorted(totals.items(), key=lambda kv: -kv[1])
    return [
        {"category": cats.get(c), "total": v, "count": counts[c], "share": round(100 * v / total, 1)}
        for c, v in rows
    ]


def trend(user, months: int = 12, end: tuple[int, int] | None = None) -> list[dict]:
    """Income/expense for the last `months` Jalali months, oldest first, zero-filled."""
    ey, em = end or jalali.today()[:2]
    keys = [jalali.add_months(ey, em, -i) for i in range(months - 1, -1, -1)]
    first, last = month_key(*keys[0]), month_key(ey, em)
    data = defaultdict(int)
    for t in counted(user).filter(jdate__gte=first, jdate__lt=last + "-99"):
        data[(t.jdate[:7], t.direction)] += t.amount
    return [
        {"jy": jy, "jm": jm, "income": data[(month_key(jy, jm), IN)], "expense": data[(month_key(jy, jm), OUT)]}
        for jy, jm in keys
    ]


def budgets(user, jy: int, jm: int) -> list[dict]:
    spent = defaultdict(int)
    for t in _month(user, jy, jm):
        if t.direction == OUT and t.category_id:
            spent[t.category_id] += t.amount
    out = []
    for c in Category.objects.filter(user=user, kind=Category.EXPENSE, archived=False).select_related("budget"):
        b = getattr(c, "budget", None)
        if not b:
            continue
        s = spent.get(c.pk, 0)
        out.append({"category": c, "budget": b.amount, "spent": s, "left": b.amount - s,
                    "pct": min(100, round(100 * s / b.amount)) if b.amount else 0, "over": s > b.amount})
    return sorted(out, key=lambda r: -r["spent"] / (r["budget"] or 1))


def balances(user) -> list[dict]:
    """Last balance each bank account reported (from its latest SMS)."""
    out = []
    for acc in Account.objects.filter(user=user, archived=False, kind=Account.BANK):
        latest = acc.transactions.filter(source=Transaction.SMS).order_by("-occurred_at", "-id")
        for t in latest.iterator(chunk_size=20):
            if t.balance is not None:
                out.append({"account": acc, "balance": t.balance, "at": t.occurred_at})
                break
    return out


def health(user) -> dict:
    """What the user should look at: missed SMS, unparsed SMS, silent automation."""
    from django.conf import settings
    from django.utils import timezone

    from .models import Message

    last = user.messages.aggregate(m=Max("received_at"))["m"]
    has_device = user.devices.filter(revoked_at__isnull=True).exists()
    silent = bool(has_device and last and (timezone.now() - last).days >= settings.SILENT_DAYS)
    return {
        "gaps": Transaction.objects.filter(user=user, has_gap=True).count(),
        "unparsed": user.messages.filter(status=Message.UNPARSED).count(),
        "uncategorized": Transaction.objects.filter(user=user, category__isnull=True).count(),
        "last_sms": last,
        "silent": silent,
        "has_device": has_device,
    }

"""Monthly numbers. Transfers between the user's own accounts are excluded everywhere."""
from __future__ import annotations

from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import Substr

from . import jalali
from .models import IN, OUT, Account, Category, Transaction


def counted(user):
    """Transactions that count as real income/expense (not own-account transfers)."""
    return Transaction.objects.filter(user=user).exclude(category__kind=Category.TRANSFER)


def month_key(jy: int, jm: int) -> str:
    return f"{jy:04d}-{jm:02d}"


def month_totals(user, jy: int, jm: int) -> dict:
    agg = counted(user).filter(jdate__startswith=month_key(jy, jm)).aggregate(
        income=Sum("amount", filter=Q(direction=IN)),
        expense=Sum("amount", filter=Q(direction=OUT)),
        n=Count("id"),
    )
    income, expense = agg["income"] or 0, agg["expense"] or 0
    return {"income": income, "expense": expense, "net": income - expense, "count": agg["n"]}


def by_category(user, jy: int, jm: int, direction: str = OUT) -> list[dict]:
    rows = (counted(user).filter(jdate__startswith=month_key(jy, jm), direction=direction)
            .values("category").annotate(total=Sum("amount"), n=Count("id")).order_by("-total"))
    cats = Category.objects.in_bulk([r["category"] for r in rows if r["category"]])
    total = sum(r["total"] for r in rows) or 1
    return [
        {"category": cats.get(r["category"]), "total": r["total"], "count": r["n"],
         "share": round(100 * r["total"] / total, 1)}
        for r in rows
    ]


def trend(user, months: int = 12, end: tuple[int, int] | None = None) -> list[dict]:
    """Income/expense for the last `months` Jalali months, oldest first, zero-filled."""
    ey, em = end or jalali.today()[:2]
    keys = [jalali.add_months(ey, em, -i) for i in range(months - 1, -1, -1)]
    first = month_key(*keys[0])
    rows = (counted(user).filter(jdate__gte=first)
            .annotate(m=Substr("jdate", 1, 7)).values("m", "direction").annotate(total=Sum("amount")))
    data = {(r["m"], r["direction"]): r["total"] for r in rows}
    return [
        {"jy": jy, "jm": jm, "income": data.get((month_key(jy, jm), IN), 0),
         "expense": data.get((month_key(jy, jm), OUT), 0)}
        for jy, jm in keys
    ]


def budgets(user, jy: int, jm: int) -> list[dict]:
    spent = {r["category"]: r["total"] for r in counted(user).filter(
        jdate__startswith=month_key(jy, jm), direction=OUT, category__isnull=False
    ).values("category").annotate(total=Sum("amount"))}
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
        last = (acc.transactions.filter(balance__isnull=False).order_by("-occurred_at", "-id")
                .values("balance", "occurred_at").first())
        if last:
            out.append({"account": acc, "balance": last["balance"], "at": last["occurred_at"]})
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
        "gaps": Transaction.objects.filter(user=user, gap_amount__isnull=False).count(),
        "unparsed": user.messages.filter(status=Message.UNPARSED).count(),
        "uncategorized": Transaction.objects.filter(user=user, category__isnull=True).count(),
        "last_sms": last,
        "silent": silent,
        "has_device": has_device,
    }

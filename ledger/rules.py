"""Automatic categorization. Order: the user's rules (lowest priority first), then a few safe
built-in hints (fees, interest, salary). A category the user picked by hand is never overridden."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from . import parsers
from .models import IN, OUT, Category, Rule, Transaction

HINTS = [  # (category.key, direction, words in the SMS)
    ("fees", OUT, ("کارمزد",)),
    ("interest", IN, ("سود",)),
    ("salary", IN, ("حقوق",)),
]


def load(user) -> list[Rule]:
    return list(Rule.objects.filter(user=user, enabled=True, category__archived=False)
                .select_related("category").order_by("priority", "id"))


def haystack(tx: Transaction) -> str:
    raw = tx.message.raw if tx.message_id else ""
    return parsers.normalize("\n".join([tx.title, tx.counterparty, tx.note, raw])).casefold()


def matches(rule: Rule, tx: Transaction, text: str) -> bool:
    if rule.direction and rule.direction != tx.direction:
        return False
    if rule.account_id and rule.account_id != tx.account_id:
        return False
    if rule.amount_min is not None and tx.amount < rule.amount_min:
        return False
    if rule.amount_max is not None and tx.amount > rule.amount_max:
        return False
    if rule.text and parsers.normalize(rule.text).casefold() not in text:
        return False
    return True


def categorize(tx: Transaction, user_rules: list[Rule] | None = None) -> bool:
    if tx.category_id:
        return False
    text = haystack(tx)
    for r in load(tx.user) if user_rules is None else user_rules:
        if matches(r, tx, text):
            tx.category, tx.category_by = r.category, Transaction.BY_RULE
            return True
    for key, direction, words in HINTS:
        if tx.direction == direction and any(w in text for w in words):
            cat = Category.objects.filter(user_id=tx.user_id, key=key, archived=False).first()
            if cat:
                tx.category, tx.category_by = cat, Transaction.BY_AUTO
                return True
    return False


def apply_to_uncategorized(user) -> int:
    user_rules, n = load(user), 0
    for tx in Transaction.objects.filter(user=user, category__isnull=True).select_related("message"):
        if categorize(tx, user_rules):
            tx.save(update_fields=["category", "category_by", "updated_at"])
            n += 1
    return n


def _top(qs, limit=3) -> list[int]:
    return [r["category"] for r in qs.values("category").annotate(n=Count("id")).order_by("-n")[:limit]]


def recent_top(user, direction: str) -> list[int]:
    since = timezone.now() - timedelta(days=90)
    return _top(Transaction.objects.filter(user=user, direction=direction, occurred_at__gte=since,
                                           category__isnull=False, category__archived=False))


def suggest(tx: Transaction, recent: list[int] | None = None, limit: int = 3) -> list[int]:
    """Category ids to offer first: what this counterparty / this exact recurring amount got
    before, then what the user picks most lately."""
    base = Transaction.objects.filter(user_id=tx.user_id, direction=tx.direction, category__isnull=False,
                                      category__archived=False).exclude(pk=tx.pk)
    ids: list[int] = []
    if tx.counterparty:
        ids += _top(base.filter(counterparty=tx.counterparty))
    ids += _top(base.filter(amount=tx.amount, title=tx.title))
    ids += recent if recent is not None else recent_top(tx.user, tx.direction)
    return list(dict.fromkeys(ids))[:limit]

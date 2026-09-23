"""SMS -> Message -> Transaction. Safe to call any number of times with the same SMS."""
from __future__ import annotations

import logging
import re
from collections import Counter
from itertools import groupby

from django.db import IntegrityError, transaction

from . import parsers, rules
from .models import Account, Message, Transaction

log = logging.getLogger(__name__)

SPLIT_RE = re.compile(r"^\s*---\s*$", re.M)
MAX_SMS_CHARS = 2000
# What arrives when the Shortcut has the words typed in instead of the blue variable
# (English and Persian iOS). Not an SMS: rejected with a hint instead of stored as "unparsed".
PLACEHOLDERS = {"shortcut input", "ورودی میانبر", "ورودی میان بر"}


class Batch:
    """Per-request work shared by many SMS: rules are loaded once, and balance gaps are
    recomputed once per touched account at the end instead of after every SMS."""

    def __init__(self, user):
        self.user = user
        self._rules = None
        self.accounts: dict[int, Account] = {}

    @property
    def rules(self):
        if self._rules is None:
            self._rules = rules.load(self.user)
        return self._rules

    def finish(self) -> None:
        for acc in self.accounts.values():
            recompute_gaps(acc)
        self.accounts.clear()


def split_batch(text: str) -> list[str]:
    """Queue file / pasted text: SMS separated by lines that are exactly '---'."""
    return [b for b in SPLIT_RE.split(text.replace("\r", "")) if b.strip()]


def ingest(user, texts: list[str], source: str, device=None) -> list[dict]:
    """No count cap on purpose: the Shortcut deletes its queue after a successful response, so
    dropping items would lose them. The request body size limit bounds the work instead."""
    batch = Batch(user)
    try:
        results = [ingest_one(user, t, source, device, batch) for t in texts]
    finally:
        batch.finish()
    # one summary line per request: counts only, never SMS text or amounts
    log.info("ingest user=%s source=%s %s", user.pk, (source or "")[:40],
             dict(Counter(r["status"] for r in results)))
    return results


def ingest_one(user, raw: str, source: str, device=None, batch: Batch | None = None) -> dict:
    raw = (raw or "").strip()[:MAX_SMS_CHARS]
    if not raw:
        return {"status": "empty"}
    if parsers.normalize(raw).casefold() in PLACEHOLDERS:
        return {"status": "ignored", "reason": "placeholder",
                "hint": "Shortcut Input must be the blue variable, not typed text"}
    if parsers.is_sensitive(raw):
        # never stored, never logged: not even the hash
        return {"status": "ignored", "reason": "sensitive"}
    h = parsers.fingerprint(raw)
    tx, err = parsers.parse(raw)
    try:
        with transaction.atomic():
            msg = Message.objects.create(
                user=user, hash=h, raw=raw, source=(source or "")[:40], device=device,
                status=Message.PARSED if tx else Message.UNPARSED, error=(err or "")[:200],
            )
            if tx:
                record(msg, tx, batch)
    except IntegrityError:
        if Message.objects.filter(user=user, hash=h).exists():
            return {"status": "duplicate"}
        raise
    if tx:
        log.debug("tx created user=%s msg=%s", user.pk, msg.pk)
        return {"status": "created", "tx": tx.as_json()}
    log.debug("sms unparsed user=%s msg=%s error=%s", user.pk, msg.pk, err)
    return {"status": "unparsed", "error": err}


def bank_account(user, bank: str, hint: str) -> Account:
    label = parsers.BANK_LABELS.get(bank, bank)
    acc, _ = Account.objects.get_or_create(
        user=user, bank=bank, hint=hint, defaults={"kind": Account.BANK, "name": f"{label} {hint}".strip()}
    )
    return acc


def record(msg: Message, tx: parsers.Tx, batch: Batch | None = None) -> Transaction:
    acc = bank_account(msg.user, tx.bank, tx.account)
    t = Transaction(
        user=msg.user, account=acc, message=msg, source=Transaction.SMS,
        direction=tx.direction, amount=tx.amount, balance=tx.balance,
        occurred_at=tx.occurred_at or msg.received_at, title=tx.title, counterparty=tx.counterparty,
    )
    rules.categorize(t, batch.rules if batch else None)
    t.save()
    if batch is None:
        recompute_gaps(acc)
    else:
        batch.accounts[acc.pk] = acc
    return t


def _chain(expected: int | None, group: list[Transaction]) -> list[Transaction]:
    """SMS times have minute resolution. Inside one minute, order transactions so their
    balances chain, whatever order they arrived in."""
    out, rest = [], list(group)
    while rest:
        nxt = None
        if expected is not None:
            nxt = next((t for t in rest if t.balance is not None and expected + t.delta == t.balance), None)
        if nxt is None:
            # chain start: a transaction whose previous balance isn't another pending one's balance
            bals = {t.balance for t in rest if t.balance is not None}
            nxt = next((t for t in rest if t.balance is None or t.balance - t.delta not in bals), rest[0])
        rest.remove(nxt)
        out.append(nxt)
        if nxt.balance is not None:
            expected = nxt.balance
        elif expected is not None:
            expected += nxt.delta
    return out


def recompute_gaps(account: Account) -> int:
    """Flag transactions whose balance doesn't follow from the previous one (an SMS is missing
    in between). Manual entries without a balance count toward the expected balance, so adding
    the missed transaction by hand clears the flag. Returns the number of gaps."""
    txs = list(account.transactions.order_by("occurred_at", "id")
               .only("id", "occurred_at", "direction", "amount", "balance", "gap_amount"))
    expected, changed, gaps = None, [], 0
    for _, group in groupby(txs, key=lambda t: t.occurred_at):
        for t in _chain(expected, list(group)):
            gap = None
            if t.balance is None:
                if expected is not None:
                    expected += t.delta
            else:
                if expected is not None and expected + t.delta != t.balance:
                    gap = t.balance - (expected + t.delta)
                expected = t.balance
            gaps += gap is not None
            if t.gap_amount != gap:
                t.gap_amount = gap
                changed.append(t)
    if changed:
        Transaction.objects.bulk_update(changed, ["gap_amount"])
    return gaps


def reparse(user=None) -> dict:
    """Re-run parsers on unparsed messages (after a new bank template ships). Also purges any
    stored message that the (possibly stricter) sensitive filter now catches."""
    qs = Message.objects.filter(status=Message.UNPARSED).select_related("user").order_by("user_id", "id")
    if user is not None:
        qs = qs.filter(user=user)
    checked = fixed = purged = 0
    batches: dict[int, Batch] = {}
    for m in qs.iterator():
        checked += 1
        if parsers.is_sensitive(m.raw):
            m.delete()
            purged += 1
            continue
        tx, err = parsers.parse(m.raw, ref=m.received_at)  # year for banks that send none
        if not tx:
            if m.error != (err or ""):
                Message.objects.filter(pk=m.pk).update(error=(err or "")[:200])
            continue
        batch = batches.setdefault(m.user_id, Batch(m.user))
        with transaction.atomic():
            m.status, m.error = Message.PARSED, ""
            m.save(update_fields=["status", "error"])
            record(m, tx, batch)
        fixed += 1
    for b in batches.values():
        b.finish()
    return {"checked": checked, "fixed": fixed, "purged": purged}

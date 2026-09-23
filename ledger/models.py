"""Every financial row carries a `user` FK and every query filters on it: users never see each
other's data. Money is always stored in rial (BigInteger); the UI converts to the user's unit."""
from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone

from . import jalali

IN, OUT = "IN", "OUT"
DIRECTIONS = [(IN, "دریافت"), (OUT, "پرداخت")]


class User(AbstractUser):
    UNITS = [("toman", "تومان"), ("rial", "ریال")]
    unit = models.CharField(max_length=5, choices=UNITS, default="toman")
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_last_step = models.BigIntegerField(default=0)  # blocks reuse of a code

    @property
    def has_2fa(self) -> bool:
        return bool(self.totp_secret)


class RecoveryCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"recovery code #{self.pk}"


class Invite(models.Model):
    code_hash = models.CharField(max_length=64, unique=True)  # the link holds the code, the DB only its hash
    note = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.note or f"invite #{self.pk}"

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()


class Device(models.Model):
    """A phone (Shortcut) allowed to send SMS. Its token can only write via /ingest, never read."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    name = models.CharField(max_length=60)
    token_hash = models.CharField(max_length=64, unique=True)
    token_prefix = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.token_prefix}…)"


class Account(models.Model):
    BANK, CASH = "bank", "cash"
    KINDS = [(BANK, "بانکی"), (CASH, "نقدی")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="accounts")
    kind = models.CharField(max_length=4, choices=KINDS, default=BANK)
    bank = models.CharField(max_length=20, blank=True)  # parser name, "" for manual accounts
    hint = models.CharField(max_length=40, blank=True)  # masked number from the SMS, "" if the bank sends none
    name = models.CharField(max_length=60)
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["archived", "kind", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "bank", "hint"], condition=~Q(bank=""), name="uniq_bank_account"),
        ]

    def __str__(self):
        return self.name


class Category(models.Model):
    EXPENSE, INCOME, TRANSFER = "expense", "income", "transfer"
    KINDS = [(EXPENSE, "هزینه"), (INCOME, "درآمد"), (TRANSFER, "انتقال (در گزارش حساب نمی‌شود)")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="categories")
    kind = models.CharField(max_length=8, choices=KINDS, default=EXPENSE)
    name = models.CharField(max_length=60)
    icon = models.CharField(max_length=8, blank=True)
    key = models.CharField(max_length=20, blank=True)  # built-in hint target: fees / interest / salary
    archived = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["archived", "kind", "order", "id"]
        constraints = [models.UniqueConstraint(fields=["user", "kind", "name"], name="uniq_category_name")]

    def __str__(self):
        return f"{self.icon} {self.name}".strip()


class Message(models.Model):
    """One raw SMS as received. Duplicates (same normalized text) are rejected per user."""
    PARSED, UNPARSED, IGNORED = "parsed", "unparsed", "ignored"
    STATUSES = [(PARSED, "parsed"), (UNPARSED, "unparsed"), (IGNORED, "ignored")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="messages")
    hash = models.CharField(max_length=64)
    raw = models.TextField()
    source = models.CharField(max_length=40, blank=True)
    device = models.ForeignKey(Device, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=8, choices=STATUSES)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "hash"], name="uniq_message")]
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return f"sms #{self.pk} ({self.status})"


class Transaction(models.Model):
    SMS, MANUAL = "sms", "manual"
    SOURCES = [(SMS, "پیامک"), (MANUAL, "دستی")]
    BY_USER, BY_RULE, BY_AUTO = "user", "rule", "auto"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="transactions")
    # RESTRICT: an account with transactions can't be deleted on its own, but deleting the user
    # (which cascades to both) still works.
    account = models.ForeignKey(Account, on_delete=models.RESTRICT, related_name="transactions")
    message = models.OneToOneField(Message, on_delete=models.CASCADE, null=True, blank=True, related_name="tx")
    source = models.CharField(max_length=6, choices=SOURCES)
    direction = models.CharField(max_length=3, choices=DIRECTIONS)
    amount = models.BigIntegerField()  # rial, > 0
    balance = models.BigIntegerField(null=True, blank=True)  # balance after, from the SMS
    occurred_at = models.DateTimeField()
    jdate = models.CharField(max_length=10, editable=False)  # Jalali YYYY-MM-DD, for month grouping
    title = models.CharField(max_length=100, blank=True)
    counterparty = models.CharField(max_length=100, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    category_by = models.CharField(max_length=4, blank=True)
    note = models.CharField(max_length=300, blank=True)
    # Set when the balance doesn't follow from the previous transaction: an SMS is missing
    # before this one. Value = net amount (rial) of what's missing.
    gap_amount = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-occurred_at"]),
            models.Index(fields=["user", "jdate"]),
            models.Index(fields=["user", "category"]),
            models.Index(fields=["account", "occurred_at"]),
        ]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="amount_positive")]

    def __str__(self):
        return f"tx #{self.pk}"

    def save(self, *args, **kwargs):
        self.jdate = jalali.fmt(*jalali.of(self.occurred_at), sep="-")
        if kwargs.get("update_fields") is not None and "occurred_at" in kwargs["update_fields"]:
            kwargs["update_fields"] = {*kwargs["update_fields"], "jdate"}
        super().save(*args, **kwargs)

    @property
    def delta(self) -> int:
        return self.amount if self.direction == IN else -self.amount


class Rule(models.Model):
    """If a new transaction matches every set condition, it gets `category`. Lowest priority wins."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="rules")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="+")
    direction = models.CharField(max_length=3, choices=DIRECTIONS, blank=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    text = models.CharField(max_length=100, blank=True)  # found in SMS text, title, counterparty or note
    amount_min = models.BigIntegerField(null=True, blank=True)  # rial
    amount_max = models.BigIntegerField(null=True, blank=True)  # rial
    priority = models.PositiveIntegerField(default=100)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"rule #{self.pk}"


class Budget(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="budgets")
    category = models.OneToOneField(Category, on_delete=models.CASCADE, related_name="budget")
    amount = models.BigIntegerField()  # rial per month

    def __str__(self):
        return f"budget #{self.pk}"


class SupportSample(models.Model):
    """An unparsed SMS a user chose to share (after editing out personal details) so the
    operator can add a parser. This is the only SMS text staff ever see."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)

    def __str__(self):
        return f"sample #{self.pk}"

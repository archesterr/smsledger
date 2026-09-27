"""Every financial row carries a `user` FK and every query filters on it: users never see each
other's data. On top of that, the sensitive values of every financial row are encrypted with the
owner's key (see vault.py): they are ordinary attributes in Python (`t.amount`), stored together
in one encrypted `sealed` column. Money is always in rial; the UI converts to the user's unit."""
from __future__ import annotations

from django.contrib.auth import models as auth_models
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone

from . import banks, jalali, vault

IN, OUT = "IN", "OUT"
DIRECTIONS = [(IN, "دریافت"), (OUT, "پرداخت")]


# ---- encrypted fields ---------------------------------------------------------------------------
class Sealed(property):
    """An attribute kept inside the row's encrypted `sealed` column. It can't be used in queries
    (filter/order/aggregate): that happens in Python, on the owner's decrypted rows. Subclassing
    property lets Django accept it as a constructor keyword (Model(amount=...))."""

    def __init__(self, default=None):
        super().__init__(self._get, self._set)
        self.default = default

    def __set_name__(self, owner, name):
        self.name = name

    def _get(self, obj):
        return obj.plain().get(self.name, self.default)

    def _set(self, obj, value):
        obj.plain()[self.name] = value
        obj.__dict__["_dirty"] = True


class SealedModel(models.Model):
    sealed = models.BinaryField(default=b"", editable=False)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        uf = kwargs.get("update_fields")
        dirty = self.__dict__.get("_dirty", False)  # untouched: no key needed (e.g. a PENDING message)
        if dirty:
            self.seal()
        if uf is not None:
            names = self.sealed_names()
            uf = {f for f in uf if f not in names}
            if dirty:
                uf.add("sealed")
            kwargs["update_fields"] = uf
        super().save(*args, **kwargs)

    def refresh_from_db(self, *args, **kwargs):
        self.__dict__.pop("_plain", None)
        self.__dict__.pop("_dirty", None)
        super().refresh_from_db(*args, **kwargs)

    @classmethod
    def sealed_names(cls) -> frozenset[str]:
        names = cls.__dict__.get("_sealed_names")
        if names is None:
            names = frozenset(n for k in cls.__mro__ for n, v in vars(k).items() if isinstance(v, Sealed))
            cls._sealed_names = names
        return names

    def plain(self) -> dict:
        p = self.__dict__.get("_plain")
        if p is None:
            blob = self.sealed
            p = vault.open_row(vault.key_for(self.user_id), self._meta.label_lower, self.user_id, blob) \
                if vault._b(blob) else {}
            self.__dict__["_plain"] = p
        return p

    def seal(self) -> None:
        """Encrypt the attributes into `sealed`. save() does this; call it yourself before
        bulk_create / bulk_update."""
        self.sealed = vault.seal_row(vault.key_for(self.user_id), self._meta.label_lower, self.user_id, self.plain())
        self.__dict__["_dirty"] = False


class UserManager(auth_models.UserManager):
    def _create_user_object(self, username, email, password, **extra_fields):
        # Django sets the hash directly here; set_password is what makes the data keys
        user = super()._create_user_object(username, email, None, **extra_fields)
        if password is not None:
            user.set_password(password)
        return user


class User(AbstractUser):
    UNITS = [("toman", "تومان"), ("rial", "ریال")]
    unit = models.CharField(max_length=5, choices=UNITS, default="toman")
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_last_step = models.BigIntegerField(default=0)  # blocks reuse of a code
    # vault.py: the data key, only ever stored wrapped (by password / recovery key), plus a key
    # pair so SMS arriving while logged out can be sealed. key_srv: pre-encryption accounts only,
    # until their owner's next login.
    vault_pub = models.BinaryField(default=b"", editable=False)
    vault_priv = models.BinaryField(default=b"", editable=False)
    key_pw = models.BinaryField(default=b"", editable=False)
    key_rec = models.BinaryField(default=b"", editable=False)
    key_srv = models.BinaryField(default=b"", editable=False)
    dedupe_key = models.BinaryField(default=b"", editable=False)
    recovery_saved_at = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    parsers_version = models.CharField(max_length=16, blank=True)  # unparsed SMS re-read when it changes

    objects = UserManager()
    REQUIRED_FIELDS = []  # no email: `createsuperuser` asks only for username + password

    @property
    def has_2fa(self) -> bool:
        return bool(self.totp_secret)

    @property
    def vault_state(self) -> str:
        return vault.state(self)

    def set_password(self, raw_password):
        super().set_password(raw_password)
        if raw_password is not None:
            vault.password_set(self, raw_password)


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


class DeviceQuerySet(models.QuerySet):
    def live(self):
        return self.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()), revoked_at__isnull=True)

    def phones(self):
        return self.filter(expires_at__isnull=True)


class Device(models.Model):
    """A phone (Shortcut) allowed to send SMS. Its token can only write via /ingest, never read.
    A key with expires_at is a computer's one-off sync of old SMS (sync_client.py), not a phone."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices")
    name = models.CharField(max_length=60)
    token_hash = models.CharField(max_length=64, unique=True)
    token_prefix = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.CharField(max_length=45, blank=True)  # network prefix only (security.ip_prefix)
    revoked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    objects = DeviceQuerySet.as_manager()

    def __str__(self):
        return f"{self.name} ({self.token_prefix}…)"


class Account(SealedModel):
    BANK, CASH = "bank", "cash"
    KINDS = [(BANK, "بانکی"), (CASH, "نقدی")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="accounts")
    kind = models.CharField(max_length=4, choices=KINDS, default=BANK)
    bank = models.CharField(max_length=20, blank=True)  # parser name, "" for manual accounts
    hint_idx = models.CharField(max_length=32, blank=True)  # vault.blind(hint): find the account by it
    brand = models.CharField(max_length=20, blank=True)  # banks.BANKS key for logo/colour; "" = use `bank`
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    name = Sealed("")
    hint = Sealed("")  # masked number from the SMS, "" if the bank sends none

    class Meta:
        ordering = ["archived", "kind", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "bank", "hint_idx"], condition=~Q(bank=""),
                                    name="uniq_bank_account"),
        ]

    def __str__(self):
        return self.name

    @property
    def bank_info(self) -> banks.Bank | None:
        return banks.get(self.brand or self.bank)


class Category(SealedModel):
    EXPENSE, INCOME, TRANSFER = "expense", "income", "transfer"
    KINDS = [(EXPENSE, "هزینه"), (INCOME, "درآمد"), (TRANSFER, "انتقال (در گزارش حساب نمی‌شود)")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="categories")
    kind = models.CharField(max_length=8, choices=KINDS, default=EXPENSE)
    name = Sealed("")  # unique per user and kind: checked in CategoryForm
    icon = models.CharField(max_length=8, blank=True)
    key = models.CharField(max_length=20, blank=True)  # built-in hint target: fees / interest / salary
    archived = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["archived", "kind", "order", "id"]

    def __str__(self):
        return f"{self.icon} {self.name}".strip()


class Message(SealedModel):
    """One raw SMS as received. Duplicates (same normalized text) are rejected per user.
    PENDING: arrived while the owner was logged out, so it's sealed to their public key in
    `inbox` and parsed at their next login."""
    PARSED, UNPARSED, IGNORED, PENDING = "parsed", "unparsed", "ignored", "pending"
    STATUSES = [(PARSED, "parsed"), (UNPARSED, "unparsed"), (IGNORED, "ignored"), (PENDING, "pending")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="messages")
    hash = models.CharField(max_length=64)  # vault.fingerprint: keyed per user
    raw = Sealed("")
    inbox = models.BinaryField(default=b"", editable=False)  # PENDING only
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


class Transaction(SealedModel):
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
    occurred_at = models.DateTimeField()
    jdate = models.CharField(max_length=10, editable=False)  # Jalali YYYY-MM-DD, for month grouping
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    category_by = models.CharField(max_length=4, blank=True)
    has_gap = models.BooleanField(default=False)  # gap_amount is set
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    amount = Sealed(0)  # rial, > 0
    balance = Sealed(None)  # balance after, from the SMS
    title = Sealed("")
    counterparty = Sealed("")
    note = Sealed("")
    # Set when the balance doesn't follow from the previous transaction: an SMS is missing
    # before this one. Value = net amount (rial) of what's missing.
    gap_amount = Sealed(None)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-occurred_at"]),
            models.Index(fields=["user", "jdate"]),
            models.Index(fields=["user", "category"]),
            models.Index(fields=["account", "occurred_at"]),
        ]

    def __str__(self):
        return f"tx #{self.pk}"

    def save(self, *args, **kwargs):
        if not self.amount or self.amount <= 0:
            raise ValueError("amount must be positive")
        self.jdate = jalali.fmt(*jalali.of(self.occurred_at), sep="-")
        self.has_gap = self.gap_amount is not None
        uf = kwargs.get("update_fields")
        if uf is not None:
            kwargs["update_fields"] = {*uf, "has_gap", *(["jdate"] if "occurred_at" in uf else [])}
        super().save(*args, **kwargs)

    @property
    def delta(self) -> int:
        return self.amount if self.direction == IN else -self.amount


class Rule(SealedModel):
    """If a new transaction matches every set condition, it gets `category`. Lowest priority wins."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="rules")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="+")
    direction = models.CharField(max_length=3, choices=DIRECTIONS, blank=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    text = Sealed("")  # found in SMS text, title, counterparty or note
    amount_min = Sealed(None)  # rial
    amount_max = Sealed(None)  # rial
    priority = models.PositiveIntegerField(default=100)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"rule #{self.pk}"


class Budget(SealedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="budgets")
    category = models.OneToOneField(Category, on_delete=models.CASCADE, related_name="budget")
    amount = Sealed(0)  # rial per month

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


class UserSession(models.Model):
    """A signed-in browser, so its owner can see it and sign it out. The Django session holds
    this row's id; a revoked row ends that session on its next request."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(default=timezone.now)
    ip = models.CharField(max_length=45, blank=True)  # network prefix only
    agent = models.CharField(max_length=80, blank=True)  # "iPhone · Safari"
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [models.Index(fields=["user", "ended_at"])]

    def __str__(self):
        return f"session #{self.pk}"


class SecurityEvent(models.Model):
    """What happened to an account (sign-ins, failures, key and device changes), shown to its
    owner. No financial data. Kept for EVENT_DAYS."""
    KINDS = {
        "login": "ورود موفق",
        "login_failed": "رمز عبور اشتباه",
        "login_blocked": "ورود به‌خاطر تلاش‌های زیاد موقتاً بسته شد",
        "2fa_failed": "کد ورود دو مرحله‌ای اشتباه",
        "recovery_code_used": "ورود با کد پشتیبان ورود دو مرحله‌ای",
        "logout": "خروج",
        "password_changed": "تغییر رمز عبور",
        "password_reset": "رمز عبور بیرون از برنامه (توسط مدیر) عوض شد؛ داده‌ها قفل شد",
        "2fa_on": "ورود دو مرحله‌ای روشن شد",
        "2fa_off": "ورود دو مرحله‌ای خاموش شد",
        "vault_on": "رمزنگاری داده‌ها فعال شد",
        "recovery_key": "کلید بازیابی جدید ساخته شد",
        "vault_recovered": "داده‌ها با کلید بازیابی باز شد",
        "vault_reset": "داده‌ها پاک شد و حساب از نو شروع شد",
        "session_revoked": "خروج یک دستگاه از راه دور",
        "sessions_revoked": "خروج همه دستگاه‌های دیگر",
        "device_added": "کلید آیفون جدید ساخته شد",
        "device_revoked": "کلید آیفون باطل شد",
        "sync_key": "دستور همگام‌سازی پیامک‌های قدیمی (رایانه) ساخته شد",
        "export": "دریافت خروجی همه داده‌ها",
        "disabled": "حساب توسط مدیر غیرفعال شد",
        "enabled": "حساب توسط مدیر فعال شد",
    }
    WARN = {"login_failed", "login_blocked", "2fa_failed", "password_reset", "disabled", "vault_reset"}
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="security_events")
    kind = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)
    ip = models.CharField(max_length=45, blank=True)  # network prefix only
    agent = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"{self.kind} #{self.pk}"

    @property
    def label(self) -> str:
        return self.KINDS.get(self.kind, self.kind)

    @property
    def warn(self) -> bool:
        return self.kind in self.WARN

"""What every new user starts with: a cash account and a Persian category set."""
from .models import Account, Category

EXPENSES = [
    ("🛒", "خواربار و سوپرمارکت", ""),
    ("🍽️", "رستوران و کافه", ""),
    ("🚕", "حمل‌ونقل", ""),
    ("🏠", "اجاره و مسکن", ""),
    ("💡", "قبوض، شارژ و اینترنت", ""),
    ("🛍️", "خرید", ""),
    ("💊", "سلامت و درمان", ""),
    ("📚", "آموزش", ""),
    ("🎬", "تفریح", ""),
    ("✈️", "سفر", ""),
    ("🎁", "هدیه و کمک", ""),
    ("💳", "قسط و وام", ""),
    ("🏦", "کارمزد بانکی", "fees"),
    ("📦", "سایر هزینه‌ها", ""),
]
INCOMES = [
    ("💼", "حقوق", "salary"),
    ("🧑‍💻", "درآمد آزاد", ""),
    ("📈", "سود بانکی", "interest"),
    ("🎁", "هدیه دریافتی", ""),
    ("↩️", "برگشت پول", ""),
    ("📦", "سایر درآمدها", ""),
]
TRANSFERS = [("🔁", "انتقال بین حساب‌های خودم", "transfer")]


def setup_defaults(user) -> None:
    """Needs the user's key in the keyring (names are encrypted)."""
    if not Account.objects.filter(user=user, kind=Account.CASH, bank="").exists():
        Account.objects.create(user=user, kind=Account.CASH, bank="", name="کیف پول (نقدی)")
    if Category.objects.filter(user=user).exists():
        return
    rows = [(Category.EXPENSE, EXPENSES), (Category.INCOME, INCOMES), (Category.TRANSFER, TRANSFERS)]
    cats = [
        Category(user=user, kind=kind, icon=icon, name=name, key=key, order=i)
        for kind, items in rows
        for i, (icon, name, key) in enumerate(items)
    ]
    for c in cats:
        c.seal()  # bulk_create skips save()
    Category.objects.bulk_create(cats)


def on_user_saved(sender, instance, created, raw=False, **kwargs):
    from . import vault

    dek = instance.__dict__.pop("_new_dek", None)
    if dek is not None:  # set_password made this user's keys: this request may use them
        vault.keyring()[instance.pk] = dek
    if getattr(instance, "_vault_locked_by_reset", False):
        from .models import SecurityEvent

        instance._vault_locked_by_reset = False
        SecurityEvent.objects.create(user=instance, kind="password_reset")
    if created and not raw and vault.has_key(instance.pk):
        setup_defaults(instance)

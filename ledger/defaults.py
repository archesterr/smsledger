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
    Account.objects.get_or_create(user=user, kind=Account.CASH, bank="", name="کیف پول (نقدی)")
    rows = [(Category.EXPENSE, EXPENSES), (Category.INCOME, INCOMES), (Category.TRANSFER, TRANSFERS)]
    Category.objects.bulk_create(
        [
            Category(user=user, kind=kind, icon=icon, name=name, key=key, order=i)
            for kind, items in rows
            for i, (icon, name, key) in enumerate(items)
        ],
        ignore_conflicts=True,
    )


def on_user_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        setup_defaults(instance)

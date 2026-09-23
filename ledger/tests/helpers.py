from django.core.cache import cache
from django.test import TestCase

from ledger import security
from ledger.models import Device, User

PASSWORD = "a-strong-test-passphrase-42"


def blu(amount: int, direction: str = "OUT", balance: int | None = None,
        jdate: str = "1405.06.31", time: str = "14:03", name: str = "آرمین") -> str:
    title, verb = ("برداشت پول", "از حساب شما پرید.") if direction == "OUT" else ("واریز پول", "به حساب شما نشست.")
    bal = f"\nموجودی: {balance:,} ریال" if balance is not None else ""
    return f"بلو\n{title}\n{name} عزیز، {amount:,} ریال {verb}{bal}\n{time}\n{jdate}"


def make_user(username: str = "ali", **extra) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, **extra)


def make_device(user: User, name: str = "iPhone") -> tuple[Device, str]:
    token = security.new_token()
    d = Device.objects.create(user=user, name=name, token_hash=security.sha256(token), token_prefix=token[:8])
    return d, token


class BaseTest(TestCase):
    def setUp(self):
        cache.clear()  # rate-limit counters live in the cache

    def login(self, user: User):
        self.client.force_login(user)

    def post_sms(self, token: str, body, content_type="application/json", query=""):
        import json

        data = json.dumps(body, ensure_ascii=False) if content_type == "application/json" else body
        return self.client.post(f"/ingest{query}", data=data, content_type=content_type,
                                HTTP_AUTHORIZATION=f"Bearer {token}")

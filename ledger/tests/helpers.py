from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from ledger import security, vault
from ledger.models import Device, User

PASSWORD = "a-strong-test-passphrase-42"


def blu(amount: int, direction: str = "OUT", balance: int | None = None,
        jdate: str = "1405.06.31", time: str = "14:03", name: str = "آرمین") -> str:
    title, verb = ("برداشت پول", "از حساب شما پرید.") if direction == "OUT" else ("واریز پول", "به حساب شما نشست.")
    bal = f"\nموجودی: {balance:,} ریال" if balance is not None else ""
    return f"بلو\n{title}\n{name} عزیز، {amount:,} ریال {verb}{bal}\n{time}\n{jdate}"


def make_user(username: str = "ali", recovery: bool = True, **extra) -> User:
    """A user with encryption keys (made by set_password), unlocked in the test's keyring.
    recovery=True: a recovery key is already saved (u.recovery_code), so pages don't redirect to it."""
    u = User.objects.create_user(username=username, password=PASSWORD, **extra)
    if recovery:
        u.recovery_code = vault.new_recovery_key()
        u.key_rec = vault.wrap_recovery(vault.key_for(u.pk), u.recovery_code)
        u.recovery_saved_at = timezone.now()
        u.save(update_fields=["key_rec", "recovery_saved_at"])
    return u


def find_all(qs, **attrs) -> list:
    """Rows whose (decrypted) attributes match: encrypted fields can't be filtered in SQL."""
    return [o for o in qs if all(getattr(o, k) == v for k, v in attrs.items())]


def find(qs, **attrs):
    rows = find_all(qs, **attrs)
    assert len(rows) == 1, f"{len(rows)} rows match {attrs}"
    return rows[0]


def login_client(client, user: User, password: str = PASSWORD) -> None:
    """Signed in with the data key in the session, as the login view does (without its
    password step, so it also works for 2FA users)."""
    client.force_login(user)
    s = client.session
    s[vault.SESSION_KEY], cookie = vault.session_parts(user, vault.unlock_password(user, password))
    s.save()
    client.cookies[vault.cookie_name()] = cookie


def make_device(user: User, name: str = "iPhone") -> tuple[Device, str]:
    token = security.new_token()
    d = Device.objects.create(user=user, name=name, token_hash=security.sha256(token), token_prefix=token[:8])
    return d, token


class BaseTest(TestCase):
    def setUp(self):
        cache.clear()  # rate-limit counters live in the cache
        self._keyring = vault.fresh_keyring()  # no keys carried over from another test

    def tearDown(self):
        vault.reset_keyring(self._keyring)

    def login(self, user: User, password: str = PASSWORD):
        login_client(self.client, user, password)

    def post_sms(self, token: str, body, content_type="application/json", query=""):
        import json

        data = json.dumps(body, ensure_ascii=False) if content_type == "application/json" else body
        return self.client.post(f"/ingest{query}", data=data, content_type=content_type,
                                HTTP_AUTHORIZATION=f"Bearer {token}")

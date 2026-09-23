import csv
import io
import time
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from ledger import ingest, security
from ledger.models import (
    Account,
    Budget,
    Category,
    Device,
    Invite,
    Message,
    RecoveryCode,
    Rule,
    SupportSample,
    Transaction,
    User,
)

from .helpers import PASSWORD, BaseTest, blu, make_device, make_user


def seed(user, n=3):
    ingest.ingest(user, [blu(1000 * (i + 1), balance=10_000 - i, time=f"1{i}:00") for i in range(n)]
                  + ["بانک ناشناس\n12 ریال"], "test")


class TestTenantIsolation(BaseTest):
    """User B must never read or change anything of user A."""

    def setUp(self):
        super().setUp()
        self.a, self.b = make_user("alice"), make_user("bob")
        seed(self.a)
        seed(self.b, 1)
        self.tx = Transaction.objects.filter(user=self.a).first()
        self.cat = Category.objects.filter(user=self.a).first()
        self.acc = Account.objects.filter(user=self.a).first()
        self.rule = Rule.objects.create(user=self.a, category=self.cat, text="x")
        self.msg = Message.objects.get(user=self.a, status=Message.UNPARSED)
        self.dev, _ = make_device(self.a)
        self.login(self.b)

    def test_foreign_objects_are_404(self):
        gets = [f"/tx/{self.tx.pk}/", f"/categories/{self.cat.pk}/", f"/rules/{self.rule.pk}/",
                f"/accounts/{self.acc.pk}/", f"/messages/{self.msg.pk}/share/"]
        for url in gets:
            self.assertEqual(self.client.get(url).status_code, 404, url)
        posts = [f"/tx/{self.tx.pk}/", f"/tx/{self.tx.pk}/delete/", f"/tx/{self.tx.pk}/category/",
                 f"/categories/{self.cat.pk}/", f"/categories/{self.cat.pk}/delete/",
                 f"/rules/{self.rule.pk}/", f"/rules/{self.rule.pk}/delete/",
                 f"/accounts/{self.acc.pk}/", f"/accounts/{self.acc.pk}/delete/",
                 f"/messages/{self.msg.pk}/delete/", f"/messages/{self.msg.pk}/share/",
                 f"/setup/devices/{self.dev.pk}/revoke/"]
        for url in posts:
            self.assertEqual(self.client.post(url, {"category": self.cat.pk, "text": "x", "name": "x"}).status_code,
                             404, url)
        # nothing of alice's changed
        self.assertTrue(Transaction.objects.filter(pk=self.tx.pk, category__isnull=True).exists())
        self.assertTrue(Category.objects.filter(pk=self.cat.pk).exists())
        self.assertTrue(Rule.objects.filter(pk=self.rule.pk).exists())
        self.assertTrue(Message.objects.filter(pk=self.msg.pk).exists())
        self.assertIsNone(Device.objects.get(pk=self.dev.pk).revoked_at)

    def test_cannot_use_foreign_category_on_own_tx(self):
        own = Transaction.objects.filter(user=self.b).first()
        r = self.client.post(f"/tx/{own.pk}/category/", {"category": self.cat.pk})
        self.assertEqual(r.status_code, 404)
        r = self.client.post(f"/tx/{own.pk}/", {"category": self.cat.pk, "note": "hi"})
        self.assertEqual(r.status_code, 200)  # form error: not a valid choice
        own.refresh_from_db()
        self.assertIsNone(own.category_id)

    def test_cannot_build_rule_or_filter_with_foreign_objects(self):
        r = self.client.post("/rules/new/", {"category": self.cat.pk, "text": "x", "priority": 1, "enabled": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Rule.objects.filter(user=self.b).exists())
        r = self.client.get(f"/tx/?account={self.acc.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["page"].paginator.count, 1)  # invalid filter -> only bob's own

    def test_lists_and_exports_only_show_own_rows(self):
        r = self.client.get("/tx/")
        self.assertEqual(r.context["page"].paginator.count, 1)
        rows = list(csv.reader(io.StringIO(self.client.get("/settings/export.csv").content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)  # header + bob's one transaction
        self.assertEqual(self.client.get("/messages/").context["total"], 1)
        self.assertEqual(self.client.get("/inbox/").context["total"], 1)


class TestPages(BaseTest):
    def setUp(self):
        super().setUp()
        self.u = make_user()
        seed(self.u)
        self.login(self.u)

    def test_every_page_renders(self):
        tx = Transaction.objects.filter(user=self.u).first()
        cat = Category.objects.filter(user=self.u).first()
        acc = Account.objects.filter(user=self.u).first()
        rule = Rule.objects.create(user=self.u, category=cat, direction="OUT")
        msg = Message.objects.filter(user=self.u, status=Message.UNPARSED).first()
        urls = ["/", "/?m=1405-06", "/?m=garbage", "/more/", "/inbox/", "/tx/", "/tx/?month=1405-06&q=بلو&gaps=on",
                "/tx/?amount_min=1&amount_max=۱۰۰۰&direction=OUT&category=none&date_from=1405/06/01",
                "/tx/new/", f"/tx/{tx.pk}/", "/reports/", "/reports/?m=1405-06", "/budgets/", "/categories/",
                "/categories/new/", f"/categories/{cat.pk}/", "/rules/", "/rules/new/?text=a&direction=OUT",
                f"/rules/{rule.pk}/", "/accounts/", "/accounts/new/", f"/accounts/{acc.pk}/", "/import/",
                "/messages/", f"/messages/{msg.pk}/share/", "/setup/", "/settings/", "/settings/2fa/",
                "/settings/delete/", "/offline/", "/manifest.webmanifest", "/sw.js"]
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)

    def test_login_required(self):
        self.client.logout()
        for url in ("/", "/tx/", "/reports/", "/settings/", "/staff/", "/tx/export.csv"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertTrue(r["Location"].startswith("/login/"), url)

    def test_manual_transaction_and_inbox_json(self):
        cash = Account.objects.get(user=self.u, kind=Account.CASH)
        cat = Category.objects.get(user=self.u, name="رستوران و کافه")
        r = self.client.post("/tx/new/", {"direction": "OUT", "amount": "۱۵۰٬۰۰۰", "account": cash.pk,
                                          "date": "۱۴۰۵/۰۷/۰۱", "time": "۱۳:۴۵", "category": cat.pk, "note": "ناهار"})
        self.assertEqual(r.status_code, 302)
        t = Transaction.objects.get(user=self.u, source=Transaction.MANUAL)
        self.assertEqual((t.amount, t.jdate, t.category_by), (1_500_000, "1405-07-01", "user"))  # toman -> rial
        self.assertEqual(t.occurred_at.astimezone(timezone.get_current_timezone()).strftime("%H:%M"), "13:45")
        sms_tx = Transaction.objects.filter(user=self.u, source=Transaction.SMS).first()
        r = self.client.post(f"/tx/{sms_tx.pk}/category/", {"category": cat.pk}, HTTP_ACCEPT="application/json")
        self.assertEqual(r.json()["ok"], True)
        sms_tx.refresh_from_db()
        self.assertEqual(sms_tx.category, cat)

    def test_rule_applies_to_uncategorized(self):
        cat = Category.objects.get(user=self.u, name="اجاره و مسکن")
        r = self.client.post("/rules/new/", {"category": cat.pk, "direction": "OUT", "amount_min": "200",
                                             "amount_max": "200", "priority": "100", "enabled": "on"})
        self.assertEqual(r.status_code, 302)
        rule = Rule.objects.get(user=self.u)
        self.assertEqual((rule.amount_min, rule.amount_max), (2000, 2000))  # entered in toman
        self.assertEqual(Transaction.objects.get(user=self.u, amount=2000).category, cat)
        ingest.ingest_one(self.u, blu(2000, balance=1, time="20:00"), "t")
        self.assertEqual(Transaction.objects.get(user=self.u, balance=1).category_by, Transaction.BY_RULE)

    def test_rule_without_conditions_rejected(self):
        cat = Category.objects.filter(user=self.u).first()
        r = self.client.post("/rules/new/", {"category": cat.pk, "priority": "100", "enabled": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Rule.objects.exists())

    def test_budgets(self):
        cat = Category.objects.get(user=self.u, name="خرید")
        r = self.client.post("/budgets/", {f"b{cat.pk}": "۲۰۰٬۰۰۰"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Budget.objects.get(category=cat).amount, 2_000_000)
        self.client.post("/budgets/", {f"b{cat.pk}": ""})
        self.assertFalse(Budget.objects.exists())
        r = self.client.post("/budgets/", {f"b{cat.pk}": "abc"})
        self.assertEqual(r.status_code, 200)

    def test_import_page(self):
        r = self.client.post("/import/", {"text": f"{blu(5, balance=3)}\n---\n{blu(5, balance=3)}\n---\nرمز پویا 1234"})
        self.assertEqual(r.context["result"], {"created": 1, "duplicate": 1, "unparsed": 0, "ignored": 1})

    def test_delete_sms_tx_keeps_dedupe(self):
        t = Transaction.objects.filter(user=self.u, source=Transaction.SMS).first()
        raw = t.message.raw
        self.client.post(f"/tx/{t.pk}/delete/")
        self.assertFalse(Transaction.objects.filter(pk=t.pk).exists())
        self.assertEqual(ingest.ingest_one(self.u, raw, "again")["status"], "duplicate")

    def test_open_redirect_blocked(self):
        t = Transaction.objects.filter(user=self.u).first()
        cat = Category.objects.filter(user=self.u).first()
        r = self.client.post(f"/tx/{t.pk}/category/", {"category": cat.pk, "back": "https://evil.example/"})
        self.assertEqual(r["Location"], "/inbox/")
        r = self.client.post(f"/tx/{t.pk}/category/", {"category": cat.pk, "back": "/tx/?q=1"})
        self.assertEqual(r["Location"], "/tx/?q=1")

    def test_csv_export_escapes_formulas(self):
        t = Transaction.objects.filter(user=self.u).first()
        t.note = "=HYPERLINK(\"http://evil\")"
        t.save()
        body = self.client.get("/tx/export.csv").content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn(",=HYPERLINK", body)

    def test_security_headers(self):
        r = self.client.get("/")
        self.assertIn("default-src 'self'", r["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", r["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r["Referrer-Policy"], "same-origin")

    def test_delete_account_cascades_only_own_data(self):
        other = make_user("sara")
        seed(other)
        r = self.client.post("/settings/delete/", {"password": PASSWORD})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="ali").exists())
        for model in (Transaction, Message, Category, Account):
            self.assertFalse(model.objects.exclude(user=other).exists(), model.__name__)
        self.assertEqual(Transaction.objects.filter(user=other).count(), 3)

    def test_delete_account_needs_password(self):
        self.client.post("/settings/delete/", {"password": "wrong"})
        self.assertTrue(User.objects.filter(username="ali").exists())

    def test_device_token_shown_once_and_works(self):
        r = self.client.post("/setup/", {"name": "iPhone 15"})
        token = r.context["new_token"]
        d = Device.objects.get(user=self.u)
        self.assertEqual(d.token_hash, security.sha256(token))
        self.assertNotIn(token, d.token_hash + d.token_prefix + d.name)
        self.assertNotIn(token, self.client.get("/setup/").content.decode())
        self.client.logout()
        self.assertEqual(self.post_sms(token, {"sms": blu(9, balance=9)}).json()["status"], "created")

    def test_password_change_keeps_session(self):
        r = self.client.post("/settings/", {"form": "password", "old_password": PASSWORD,
                                            "new_password1": "another-long-passphrase-7",
                                            "new_password2": "another-long-passphrase-7"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_unit_preference(self):
        self.client.post("/settings/", {"form": "unit", "unit": "rial"})
        self.u.refresh_from_db()
        self.assertEqual(self.u.unit, "rial")


class TestAuth(BaseTest):
    def setUp(self):
        super().setUp()
        self.u = make_user("Ali")

    def login_post(self, username="ali", password=PASSWORD, **extra):
        return self.client.post("/login/", {"username": username, "password": password, **extra})

    def test_login_case_insensitive_and_next(self):
        r = self.login_post("ALI", next="/reports/")
        self.assertEqual(r["Location"], "/reports/")
        self.client.logout()
        r = self.login_post("ali", next="https://evil.example/")
        self.assertEqual(r["Location"], "/")

    def test_lockout_after_failures(self):
        for _ in range(10):
            self.login_post(password="wrong")
        r = self.login_post()  # right password, still locked
        self.assertEqual(r.status_code, 200)
        self.assertIn("۱۵ دقیقه", r.context["error"])

    def test_admin_login_goes_through_our_login(self):
        r = self.client.get("/admin/login/?next=/admin/")
        self.assertEqual(r["Location"], "/login/?next=%2Fadmin%2F")

    def test_logout_is_post_only(self):
        self.login(self.u)
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.client.post("/logout/")
        self.assertEqual(self.client.get("/").status_code, 302)

    def enable_2fa(self):
        secret = security.totp_new_secret()
        self.u.totp_secret = secret
        self.u.save()
        codes = ["abc123-def456", "111111-222222"]
        for c in codes:
            RecoveryCode.objects.create(user=self.u, code_hash=security.recovery_hash(c))
        return secret, codes

    def current_code(self, secret):
        import base64

        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
        return security.hotp(key, int(time.time() // 30))

    def test_2fa_login_flow_and_replay(self):
        secret, codes = self.enable_2fa()
        r = self.login_post()
        self.assertEqual(r["Location"], "/login/verify/")
        self.assertEqual(self.client.get("/").status_code, 302)  # not logged in yet
        r = self.client.post("/login/verify/", {"code": "000000"})
        self.assertEqual(r.status_code, 200)
        code = self.current_code(secret)
        r = self.client.post("/login/verify/", {"code": code})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.logout()
        self.login_post()
        r = self.client.post("/login/verify/", {"code": code})  # same code again
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/login/verify/", {"code": codes[0].upper()})  # recovery code, once
        self.assertEqual(r.status_code, 302)
        self.client.logout()
        self.login_post()
        self.assertEqual(self.client.post("/login/verify/", {"code": codes[0]}).status_code, 200)

    def test_2fa_verify_requires_password_step(self):
        self.enable_2fa()
        r = self.client.get("/login/verify/")
        self.assertEqual(r["Location"], "/login/")

    def test_2fa_enrollment(self):
        self.login(self.u)
        r = self.client.get("/settings/2fa/")
        self.assertIn("<svg", r.content.decode())
        secret = self.client.session["totp_setup"]
        r = self.client.post("/settings/2fa/", {"code": self.current_code(secret)})
        self.assertEqual(len(r.context["codes"]), 10)
        self.u.refresh_from_db()
        self.assertEqual(self.u.totp_secret, secret)
        self.assertEqual(self.u.recovery_codes.count(), 10)


class TestInvitesAndStaff(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = make_user("boss", is_staff=True)

    def test_staff_requires_staff_and_2fa(self):
        friend = make_user("friend")
        self.login(friend)
        self.assertEqual(self.client.get("/staff/").status_code, 404)
        self.login(self.admin)
        self.assertEqual(self.client.get("/staff/")["Location"], "/settings/2fa/")
        self.admin.totp_secret = security.totp_new_secret()
        self.admin.save()
        self.assertEqual(self.client.get("/staff/").status_code, 200)

    def test_staff_page_shows_no_money(self):
        friend = make_user("friend")
        ingest.ingest_one(friend, blu(123_456_789, balance=987_654_321), "t")
        SupportSample.objects.create(user=friend, text="بانک ***\nنمونه")
        self.admin.totp_secret = security.totp_new_secret()
        self.admin.save()
        self.login(self.admin)
        html = self.client.get("/staff/").content.decode()
        self.assertIn("friend", html)
        self.assertIn("نمونه", html)  # explicitly shared samples are visible
        for secret in ("۱۲٬۳۴۵٬۶۷۸", "123,456", "۹۸٬۷۶۵٬۴۳۲", "987,654"):
            self.assertNotIn(secret, html)

    def test_invite_single_use_and_defaults(self):
        self.admin.totp_secret = security.totp_new_secret()
        self.admin.save()
        self.login(self.admin)
        self.client.post("/staff/invites/", {"note": "Ali"})
        link = self.client.get("/staff/").context["new_link"]
        path = link.split("testserver", 1)[1]
        self.assertIsNone(self.client.get("/staff/").context["new_link"])  # shown once
        self.client.logout()
        self.assertEqual(self.client.get(path).status_code, 200)
        data = {"username": "newfriend", "password1": "a-good-long-pass-99", "password2": "a-good-long-pass-99"}
        r = self.client.post(path, data)
        self.assertEqual(r["Location"], "/setup/")
        u = User.objects.get(username="newfriend")
        self.assertEqual(Category.objects.filter(user=u).count(), 21)
        self.assertTrue(Account.objects.filter(user=u, kind=Account.CASH).exists())
        self.assertEqual(Invite.objects.get().used_by, u)
        self.client.logout()
        self.assertEqual(self.client.post(path, {**data, "username": "second"}).status_code, 404)
        self.assertFalse(User.objects.filter(username="second").exists())

    def test_expired_invite_and_case_insensitive_username(self):
        code = security.new_invite_code()
        Invite.objects.create(code_hash=security.sha256(code), expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(f"/join/{code}/").status_code, 404)
        code2 = security.new_invite_code()
        Invite.objects.create(code_hash=security.sha256(code2), expires_at=timezone.now() + timedelta(days=1))
        r = self.client.post(f"/join/{code2}/", {"username": "BOSS", "password1": "a-good-long-pass-99",
                                                  "password2": "a-good-long-pass-99"})
        self.assertEqual(r.status_code, 200)  # "boss" exists already
        self.assertFalse(User.objects.filter(username="BOSS").exists())

    def test_weak_password_rejected(self):
        code = security.new_invite_code()
        Invite.objects.create(code_hash=security.sha256(code), expires_at=timezone.now() + timedelta(days=1))
        r = self.client.post(f"/join/{code}/", {"username": "x1", "password1": "12345678", "password2": "12345678"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="x1").exists())


class TestOps(BaseTest):
    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual((r.status_code, r.content), (200, b"ok"))

    def test_metrics_disabled_by_default(self):
        self.assertEqual(self.client.get("/metrics").status_code, 404)

    @override_settings(METRICS_TOKEN="m-secret")
    def test_metrics_with_token(self):
        u = make_user("secretname")
        seed(u)
        self.assertEqual(self.client.get("/metrics").status_code, 401)
        r = self.client.get("/metrics", HTTP_AUTHORIZATION="Bearer m-secret")
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('smsledger_messages{status="parsed"} 3', body)
        self.assertIn("smsledger_users 1", body)
        self.assertNotIn("secretname", body)

    def test_manifest_and_sw(self):
        m = self.client.get("/manifest.webmanifest")
        self.assertEqual(m["Content-Type"], "application/manifest+json")
        self.assertEqual(m.json()["dir"], "rtl")
        sw = self.client.get("/sw.js")
        self.assertEqual(sw["Content-Type"], "application/javascript")
        self.assertIn("smsledger-", sw.content.decode())


class TestAdminSite(BaseTest):
    def test_admin_requires_2fa_and_hides_money(self):
        boss = make_user("boss", is_staff=True, is_superuser=True)
        self.login(boss)
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 302)  # to admin login ...
        self.assertEqual(self.client.get(r["Location"])["Location"], "/settings/2fa/")  # ... which asks for 2FA
        boss.totp_secret = security.totp_new_secret()
        boss.save()
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        for model in ("transaction", "message", "category", "rule", "budget", "account"):
            self.assertEqual(self.client.get(f"/admin/ledger/{model}/").status_code, 404, model)
        self.assertEqual(self.client.get("/admin/ledger/invite/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/ledger/device/add/").status_code, 403)

    def test_category_delete_clears_marker(self):
        u = make_user()
        seed(u, 1)
        cat = Category.objects.filter(user=u).first()
        Transaction.objects.filter(user=u).update(category=cat, category_by="user")
        self.login(u)
        self.client.post(f"/categories/{cat.pk}/delete/")
        t = Transaction.objects.get(user=u)
        self.assertEqual((t.category_id, t.category_by), (None, ""))

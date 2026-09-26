import sqlite3
import tempfile
from datetime import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from ledger import ingest, parsers, vault
from ledger.models import Account, Message, Transaction

from .helpers import PASSWORD, BaseTest, blu, find, make_device, make_user


class TestIngestEndpoint(BaseTest):
    """The phone has no key: its SMS are sealed to the owner's public key ("received") and
    recorded on the owner's next page load."""

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.device, self.token = make_device(self.user)
        vault.keyring().clear()  # the phone's request never has the user's key

    def open_app(self, user=None):
        user = user or self.user
        self.login(user)
        self.assertEqual(self.client.get("/").status_code, 200)
        vault.keyring()[user.pk] = vault.unlock_password(user, PASSWORD)  # so the test can read the rows

    def test_received_sealed_then_recorded_at_next_login(self):
        r = self.post_sms(self.token, {"sms": blu(1_000_000, balance=2_887_139)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "received", "parsed": True})
        r = self.post_sms(self.token, {"sms": blu(1_000_000, balance=2_887_139)})
        self.assertEqual(r.json()["status"], "duplicate")
        m = Message.objects.get(user=self.user)
        self.assertEqual((m.status, bytes(m.sealed)), (Message.PENDING, b""))
        self.assertNotIn(b"887", bytes(m.inbox))
        self.assertFalse(Transaction.objects.exists())
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_used_at)
        self.assertTrue(self.device.last_ip.endswith(".x"))

        self.open_app()
        m.refresh_from_db()
        self.assertEqual((m.status, bytes(m.inbox)), (Message.PARSED, b""))
        t = Transaction.objects.get(user=self.user)
        self.assertEqual((t.amount, t.balance), (1_000_000, 2_887_139))
        self.assertIn("1,000,000", m.raw)
        # resending after it was recorded is still a duplicate
        self.assertEqual(self.post_sms(self.token, {"sms": blu(1_000_000, balance=2_887_139)}).json()["status"],
                         "duplicate")

    def test_batch_json_list_and_split_text(self):
        r = self.post_sms(self.token, {"sms": [blu(1, balance=99), blu(2, balance=97), ""]})
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["count"], {"received": 2, "empty": 1})
        queue = f"{blu(3, balance=94)}\n---\n{blu(1, balance=99)}\n---\n"
        r = self.post_sms(self.token, queue, content_type="text/plain", query="?split=1&source=queue")
        self.assertEqual(r.json()["count"], {"received": 1, "duplicate": 1})
        self.assertEqual(Message.objects.filter(user=self.user, source="queue").count(), 1)
        self.open_app()
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 3)

    def test_empty_body_is_success_for_the_setup_test(self):
        r = self.post_sms(self.token, {"sms": ""})
        self.assertEqual(r.json(), {"status": "empty"})
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_used_at)

    def test_otp_is_never_stored(self):
        otp = "بانک ملت\nرمز پویا: 12345678\nمبلغ: 1,000,000 ریال"
        r = self.post_sms(self.token, {"sms": otp})
        self.assertEqual(r.json()["status"], "ignored")
        self.assertFalse(Message.objects.exists())

    def test_unparsed_is_kept(self):
        r = self.post_sms(self.token, {"sms": "بانک ناشناس\n500 ریال"})
        self.assertEqual(r.json(), {"status": "received", "parsed": False})
        self.open_app()
        m = Message.objects.get()
        self.assertEqual((m.status, m.raw), (Message.UNPARSED, "بانک ناشناس\n500 ریال"))

    def test_auth_failures_never_contain_status(self):
        # the Sync shortcut deletes its queue only when the response has a "status" key
        other_user = make_user("reza", is_active=False)
        _, inactive_token = make_device(other_user)
        revoked, revoked_token = make_device(self.user, "old")
        revoked.revoked_at = revoked.created_at
        revoked.save()
        for auth in ("", "Bearer ", "Bearer nope", f"Bearer {revoked_token}", f"Bearer {inactive_token}",
                     f"Token {self.token}"):
            r = self.client.post("/ingest", data='{"sms": "x"}', content_type="application/json",
                                 HTTP_AUTHORIZATION=auth)
            self.assertEqual(r.status_code, 401, auth)
            self.assertNotIn("status", r.json())
        self.assertFalse(Message.objects.exists())

    def test_bad_json(self):
        for body in ("{nope", "[1, 2]", '"text"'):
            r = self.client.post("/ingest", data=body, content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {self.token}")
            self.assertEqual(r.status_code, 400, body)
            self.assertNotIn("status", r.json())

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=100)
    def test_body_too_large(self):
        r = self.post_sms(self.token, "x" * 500, content_type="text/plain")
        self.assertEqual(r.status_code, 413)

    def test_bad_tokens_are_rate_limited(self):
        for _ in range(20):
            self.client.post("/ingest", data="{}", content_type="application/json", HTTP_AUTHORIZATION="Bearer bad")
        r = self.post_sms(self.token, {"sms": blu(5)})  # same IP, even with a good token
        self.assertEqual(r.status_code, 429)

    def test_get_not_allowed_and_token_cannot_read(self):
        self.post_sms(self.token, {"sms": blu(1_000_000, balance=1)})
        self.assertEqual(self.client.get("/ingest", HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 405)
        for url in ("/", "/tx/", "/tx/export.csv", "/settings/export.csv", "/reports/"):
            r = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.token}")
            self.assertEqual(r.status_code, 302, url)  # the device token is not a login
            self.assertIn("/login/", r["Location"])

    def test_dedupe_is_per_user(self):
        other = make_user("sara")
        _, other_token = make_device(other)
        vault.keyring().clear()
        sms = blu(700_000, balance=5_000_000)
        self.assertEqual(self.post_sms(self.token, {"sms": sms}).json()["status"], "received")
        self.assertEqual(self.post_sms(other_token, {"sms": sms}).json()["status"], "received")
        # keyed per user: the same SMS doesn't even hash the same for two people
        self.assertNotEqual(*Message.objects.order_by("user_id").values_list("hash", flat=True))
        self.open_app(other)
        self.assertEqual(Transaction.objects.filter(user=other).count(), 1)
        self.assertEqual(Message.objects.get(user=self.user).status, Message.PENDING)  # not theirs to open


class TestGaps(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def ingest(self, *texts):
        return ingest.ingest(self.user, list(texts), "test")

    def gaps(self):
        return [(t.amount, t.gap_amount) for t in Transaction.objects.filter(user=self.user, has_gap=True)
                .order_by("occurred_at")]

    def test_missing_sms_is_flagged_and_cleared_when_it_arrives(self):
        a = blu(100, "OUT", 900, time="10:00")
        b = blu(50, "IN", 950, time="11:00")
        missing = blu(400, "OUT", 550, time="12:00")
        c = blu(50, "OUT", 500, time="13:00")
        self.ingest(a, b, c)
        self.assertEqual(self.gaps(), [(50, -400)])  # 400 rial of spending is missing before c
        self.ingest(missing)
        self.assertEqual(self.gaps(), [])

    def test_same_minute_out_of_order_is_not_a_gap(self):
        first = blu(100, "OUT", 900, time="10:00")
        second = blu(300, "OUT", 600, time="10:00")
        self.ingest(second, first)  # arrived in the wrong order
        self.ingest(blu(100, "IN", 700, time="11:00"))
        self.assertEqual(self.gaps(), [])

    def test_manual_entry_fills_gap(self):
        self.ingest(blu(100, "OUT", 900, time="10:00"), blu(50, "OUT", 450, time="12:00"))
        self.assertEqual(self.gaps(), [(50, -400)])
        t0 = find(Transaction.objects.all(), amount=100)
        Transaction.objects.create(user=self.user, account=t0.account, source=Transaction.MANUAL, direction="OUT",
                                   amount=400, occurred_at=timezone.localtime(t0.occurred_at).replace(hour=11))
        ingest.recompute_gaps(t0.account)
        self.assertEqual(self.gaps(), [])


class TestReparseAndLegacy(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def test_typed_shortcut_input_placeholder_is_not_stored(self):
        for raw in ("Shortcut Input", " shortcut input ", "ورودی میان\u200cبر"):
            r = ingest.ingest_one(self.user, raw, "iphone")
            self.assertEqual((r["status"], r["reason"]), ("ignored", "placeholder"), raw)
        self.assertFalse(Message.objects.exists())

    def test_reparse_after_new_parser(self):
        raw = "بانک تست\nبرداشت\n2,000 ریال از حساب شما\nموجودی: 8,000"
        self.assertEqual(ingest.ingest_one(self.user, raw, "t")["status"], "unparsed")

        class TestBank(parsers.BluParser):
            name, label = "test", "تست"

            def match(self, text):
                return text.startswith("بانک تست")

        with mock.patch.object(parsers, "PARSERS", [TestBank()]), \
                mock.patch.dict(parsers.BANK_LABELS, {"test": "تست"}):
            r = ingest.reparse(self.user)  # what the next request with the user's key runs
        self.assertEqual((r["checked"], r["fixed"]), (1, 1))
        t = Transaction.objects.get(user=self.user)
        self.assertEqual((t.amount, t.balance, t.account.bank), (2000, 8000, "test"))
        self.assertEqual(Message.objects.get().status, Message.PARSED)

    def test_reparse_takes_the_year_from_when_the_sms_arrived(self):
        # Melli sends no year: an old unparsed SMS re-parsed today must keep its own year
        received = datetime(2025, 3, 22, 9, 0, tzinfo=parsers.TEHRAN)  # 1404-01-02
        Message.objects.create(user=self.user, hash="m", received_at=received, status=Message.UNPARSED,
                               raw="بانك ملي ايران\nانتقال:1,000-\nحساب:97007\nمانده:5,000\n1228-10:00")
        self.assertEqual(ingest.reparse(self.user)["fixed"], 1)
        t = Transaction.objects.get(user=self.user)
        self.assertEqual(timezone.localtime(t.occurred_at).date().isoformat(), "2025-03-18")  # 1403-12-28

    def test_reparse_purges_stored_sensitive(self):
        Message.objects.create(user=self.user, hash="x", raw="کد تایید شما 1234", status=Message.UNPARSED)
        self.assertEqual(ingest.reparse(self.user)["purged"], 1)
        self.assertFalse(Message.objects.exists())

    def test_import_legacy_sqlite(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            con = sqlite3.connect(f.name)
            con.execute("CREATE TABLE sms (id INTEGER PRIMARY KEY, raw TEXT)")
            con.executemany("INSERT INTO sms (raw) VALUES (?)", [(blu(10, balance=90),), (blu(10, balance=90),),
                                                                 ("junk",)])
            con.commit()
            con.close()
            out = StringIO()
            call_command("import_legacy", f.name, "--user", "ali", stdout=out)
        self.assertIn("imported 3", out.getvalue())
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Account.objects.filter(user=self.user, bank="blu").count(), 1)

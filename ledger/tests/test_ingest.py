import sqlite3
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from ledger import ingest, parsers
from ledger.models import Account, Message, Transaction

from .helpers import BaseTest, blu, make_device, make_user


class TestIngestEndpoint(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.device, self.token = make_device(self.user)

    def test_created_duplicate_and_json_shapes(self):
        r = self.post_sms(self.token, {"sms": blu(1_000_000, balance=2_887_139)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "created")
        self.assertEqual(r.json()["tx"]["amount"], 1_000_000)
        r = self.post_sms(self.token, {"sms": blu(1_000_000, balance=2_887_139)})
        self.assertEqual(r.json()["status"], "duplicate")
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 1)
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_used_at)

    def test_batch_json_list_and_split_text(self):
        r = self.post_sms(self.token, {"sms": [blu(1, balance=99), blu(2, balance=97), ""]})
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["count"], {"created": 2, "empty": 1})
        queue = f"{blu(3, balance=94)}\n---\n{blu(1, balance=99)}\n---\n"
        r = self.post_sms(self.token, queue, content_type="text/plain", query="?split=1&source=queue")
        self.assertEqual(r.json()["count"], {"created": 1, "duplicate": 1})
        self.assertEqual(Message.objects.filter(user=self.user, source="queue").count(), 1)

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
        self.assertEqual(r.json()["status"], "unparsed")
        self.assertEqual(Message.objects.get().status, Message.UNPARSED)

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
        sms = blu(700_000, balance=5_000_000)
        self.assertEqual(self.post_sms(self.token, {"sms": sms}).json()["status"], "created")
        self.assertEqual(self.post_sms(other_token, {"sms": sms}).json()["status"], "created")
        self.assertEqual(Transaction.objects.filter(user=other).count(), 1)


class TestGaps(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def ingest(self, *texts):
        return ingest.ingest(self.user, list(texts), "test")

    def gaps(self):
        return list(Transaction.objects.filter(user=self.user, gap_amount__isnull=False)
                    .order_by("occurred_at").values_list("amount", "gap_amount"))

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
        t0 = Transaction.objects.get(amount=100)
        Transaction.objects.create(user=self.user, account=t0.account, source=Transaction.MANUAL, direction="OUT",
                                   amount=400, occurred_at=timezone.localtime(t0.occurred_at).replace(hour=11))
        ingest.recompute_gaps(t0.account)
        self.assertEqual(self.gaps(), [])


class TestReparseAndLegacy(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()

    def test_reparse_after_new_parser(self):
        raw = "بانک تست\nبرداشت\n2,000 ریال از حساب شما\nموجودی: 8,000"
        self.assertEqual(ingest.ingest_one(self.user, raw, "t")["status"], "unparsed")

        class TestBank(parsers.BluParser):
            name, label = "test", "تست"

            def match(self, text):
                return text.startswith("بانک تست")

        with mock.patch.object(parsers, "PARSERS", [TestBank()]), \
                mock.patch.dict(parsers.BANK_LABELS, {"test": "تست"}):
            r = ingest.reparse()
        self.assertEqual((r["checked"], r["fixed"]), (1, 1))
        t = Transaction.objects.get(user=self.user)
        self.assertEqual((t.amount, t.balance, t.account.bank), (2000, 8000, "test"))
        self.assertEqual(Message.objects.get().status, Message.PARSED)

    def test_reparse_purges_stored_sensitive(self):
        Message.objects.create(user=self.user, hash="x", raw="کد تایید شما 1234", status=Message.UNPARSED)
        self.assertEqual(ingest.reparse()["purged"], 1)
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

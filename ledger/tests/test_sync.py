"""The one-command sync of old SMS from a computer (sync_client.py) and the server side of it:
temporary keys, /ingest's backup items, the pending time budget and the served script."""
import ast
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import LiveServerTestCase
from django.utils import timezone

from ledger import ingest, jalali, sync_client, vault
from ledger.models import Device, Message, SecurityEvent, Transaction
from ledger.views import app

from .helpers import PASSWORD, BaseTest, blu, make_device, make_user
from .test_onboarding import MELLI_NO_YEAR, ms

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
OTP = "رمز یکبار مصرف شما 123456\nمانده اعتبار: 2 دقیقه"


def attributed(text: str) -> bytes:
    """An iOS 16 attributedBody (archived NSAttributedString) holding `text`."""
    raw = text.encode()
    size = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08"
            b"NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + size + raw + b"\x86\x84\x02iI")


def make_sms_db(path: Path, rows: list[tuple]) -> Path:
    """An sms.db in the iOS schema. rows: (sender, text or None, attributedBody or None, when, service,
    is_from_me); `when` is stored as Apple's nanoseconds."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, date INTEGER,
                              handle_id INTEGER, is_from_me INTEGER, service TEXT);""")
    handles: dict[str, int] = {}
    for sender, text, body, when, service, mine in rows:
        if sender not in handles:
            handles[sender] = con.execute("INSERT INTO handle (id) VALUES (?)", (sender,)).lastrowid
        date = int((when - APPLE_EPOCH).total_seconds()) * 1_000_000_000
        con.execute("INSERT INTO message (text, attributedBody, date, handle_id, is_from_me, service) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (text, body, date, handles[sender], mine, service))
    con.commit()
    con.close()
    return path


def phone_rows(now: datetime) -> list[tuple]:
    old, older, recent = now - timedelta(days=200), now - timedelta(days=400), now - timedelta(days=3)
    long_blu = blu(2_500_000, "IN", balance=9_000_000, name="آرمین " * 20)  # > 127 bytes: 2-byte length
    return [
        ("BluBank", blu(100_000, balance=5_000_000), None, older, "SMS", 0),
        ("BluBank", None, attributed(long_blu), recent, "SMS", 0),  # iOS 16: only in attributedBody
        ("+98700717", MELLI_NO_YEAR, None, old, "SMS", 0),
        ("+989121234567", "موجودی کارتت چقدره؟", None, old + timedelta(hours=1), "SMS", 0),  # a person: unticked
        ("friend@icloud.com", "مانده حساب", None, old, "iMessage", 0),  # never read
        ("BluBank", OTP, None, recent, "SMS", 0),  # one-time code: never read
        ("BluBank", "خرید با تخفیف ویژه!", None, recent, "SMS", 0),  # an ad: no balance line
        ("+98700717", "موجودی را فرستادم", None, recent, "SMS", 1),  # sent by the owner
    ]


class TestSyncKey(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.login(self.user)

    def test_command_is_shown_once_with_a_one_day_key(self):
        self.assertNotIn("sml_", self.client.get("/import/").content.decode())
        r = self.client.post("/import/sync/")
        cmd = r.context["sync_command"]
        self.assertTrue(cmd.startswith(
            """python3 -c "import urllib.request as u; exec(u.urlopen('http://testserver/sync.py').read())" sml_"""))
        self.assertIn('id="sync-cmd"', r.content.decode())
        d = Device.objects.get()
        self.assertEqual(d.token_prefix, cmd.split()[-1][:8])
        self.assertAlmostEqual((d.expires_at - timezone.now()).total_seconds(), 24 * 3600, delta=60)
        self.assertTrue(SecurityEvent.objects.filter(user=self.user, kind="sync_key").exists())
        self.client.post("/import/sync/")  # a new command replaces the old one
        self.assertEqual(Device.objects.live().count(), 1)
        self.assertEqual(Device.objects.filter(revoked_at__isnull=False).count(), 1)

    def test_computer_keys_are_not_phones(self):
        self.client.post("/import/sync/")
        Device.objects.update(last_used_at=timezone.now())
        setup = self.client.get("/setup/")
        self.assertFalse(setup.context["connected"])
        self.assertEqual(list(setup.context["devices"]), [])
        page = self.client.get("/security/").content.decode()
        self.assertIn("رایانه · پیامک‌های قدیمی", page)
        self.assertIn("موقت", page)
        self.client.post("/setup/", {"name": ""})  # a new phone key leaves the computer's alone
        self.assertEqual(Device.objects.live().count(), 2)

    def test_needs_post_and_login(self):
        self.assertEqual(self.client.get("/import/sync/").status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post("/import/sync/").status_code, 302)
        self.assertFalse(Device.objects.exists())


class TestBackupItems(BaseTest):
    """sync_client.py sends {"items": [{"text", "at"}]} with its key, like the phone: sealed, then
    recorded at the owner's next page load, with each SMS's own arrival time."""

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.device, self.token = make_device(self.user, "computer")
        Device.objects.filter(pk=self.device.pk).update(expires_at=timezone.now() + timedelta(hours=24))
        vault.keyring().clear()

    def post(self, body, query="", token=None):
        return self.client.post(f"/ingest{query}", data=json.dumps(body, ensure_ascii=False),
                                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    def test_items_are_sealed_with_their_arrival_time(self):
        arrived = datetime(2025, 3, 22, 9, 0, tzinfo=jalali.TEHRAN)  # 1404-01-02
        items = [{"text": MELLI_NO_YEAR, "at": ms(arrived)}, {"text": blu(100, balance=900), "at": ms(arrived)},
                 {"text": OTP, "at": ms(arrived)}]
        r = self.post({"items": items, "source": "backup"})
        self.assertEqual(r.json(), {"status": "ok", "count": {"received": 2, "ignored": 1}})
        self.assertEqual({m.received_at for m in Message.objects.all()}, {arrived})
        self.assertEqual(self.post({"items": items[:1]}).json()["count"], {"duplicate": 1})
        self.login(self.user)
        self.client.get("/")
        vault.keyring()[self.user.pk] = vault.unlock_password(self.user, PASSWORD)  # so the test can read rows
        melli = Transaction.objects.get(account__bank="melli")
        self.assertEqual(timezone.localtime(melli.occurred_at).date().isoformat(), "2025-03-18")  # 1403-12-28
        self.assertEqual({m.source for m in Message.objects.all()}, {"backup"})

    def test_bad_items(self):
        for items in ([], "x", [1], [{"text": "x"}] * (ingest.MAX_ITEMS + 1)):
            r = self.post({"items": items})
            self.assertEqual(r.status_code, 400, str(items)[:30])
            self.assertNotIn("status", r.json())
        self.assertFalse(Message.objects.exists())

    def test_done_revokes_only_the_temporary_key(self):
        _, phone_token = make_device(self.user, "iPhone")
        self.assertEqual(self.post({}, "?source=done", phone_token).json(), {"status": "done"})
        self.assertEqual(self.post({}, "?source=done").json(), {"status": "done"})
        self.assertEqual(self.post({"items": [{"text": blu(1, balance=1)}]}).status_code, 401)
        self.assertEqual(self.post({"sms": blu(1, balance=1)}, token=phone_token).status_code, 200)

    def test_expired_key(self):
        Device.objects.filter(pk=self.device.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post({"items": [{"text": blu(1, balance=1)}]}).status_code, 401)
        self.assertEqual(Device.objects.live().count(), 0)


class TestPendingBudget(BaseTest):
    def test_a_big_backup_is_recorded_over_several_page_loads(self):
        user = make_user()
        _, token = make_device(user)
        vault.keyring().clear()
        items = [{"text": blu(10 + i, balance=1000 - i)} for i in range(3)]
        self.client.post("/ingest", data=json.dumps({"items": items}), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")
        self.login(user)
        with mock.patch.object(ingest, "PENDING_BUDGET", 0):  # one SMS per page load
            r = self.client.get("/", follow=True)
            self.assertIn("۲ پیامک مانده", r.content.decode())
            self.client.get("/")
            self.client.get("/")
        self.assertFalse(Message.objects.filter(status=Message.PENDING).exists())
        self.assertEqual(Message.objects.filter(status=Message.PARSED).count(), 3)


class TestSyncScript(BaseTest):
    def test_served_with_this_server_and_the_periods(self):
        r = self.client.get("/sync.py")  # no login: it holds no key
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/x-python; charset=utf-8")
        src = r.content.decode()
        ast.parse(src, feature_version=(3, 8))  # the system python3 of whatever computer runs it
        cfg = ast.literal_eval(next(line for line in src.splitlines() if line.startswith("CONFIG = "))[9:])
        self.assertEqual(cfg["server"], "http://testserver/")
        self.assertEqual([p["key"] for p in cfg["periods"]], ["month", "3months", "year", "all"])
        self.assertTrue(cfg["periods"][0]["en"].startswith("Last month (since 1 "))
        self.assertIn("پویا", cfg["otp"])


class TestClientReading(BaseTest):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.db = make_sms_db(self.tmp / "sms.db", phone_rows(self.now))

    def read(self, path):
        return sync_client.read_bank_sms(path, sync_client.re.compile(sync_client.CONFIG["otp"], sync_client.re.I))

    def test_bank_sms_only(self):
        with mock.patch.dict(sync_client.CONFIG, {"otp": "رمز|OTP"}):
            scanned, sms = self.read(self.db)
        self.assertEqual(scanned, 7)  # everything not sent by the owner
        self.assertEqual([m["sender"] for m in sms], ["BluBank", "+98700717", "+989121234567", "BluBank"])
        self.assertIn("آرمین آرمین", sms[-1]["text"])  # decoded from attributedBody
        self.assertEqual(sms[-1]["at"], ms(self.now - timedelta(days=3)))
        senders = sync_client.senders_of(sms)
        self.assertEqual({s: v["on"] for s, v in senders.items()},
                         {"BluBank": True, "+98700717": True, "+989121234567": False})
        self.assertEqual(senders["BluBank"]["n"], 2)

    def test_old_ios_seconds_and_short_texts(self):
        in_2016 = (978307200 + 500_000_000) * 1000
        self.assertEqual(sync_client.arrival_ms(500_000_000), in_2016)  # before iOS 11: seconds
        self.assertEqual(sync_client.arrival_ms(500_000_000 * 10 ** 9), in_2016)  # since: nanoseconds
        self.assertEqual(sync_client.from_attributed(attributed("مانده: 5")), "مانده: 5")
        self.assertEqual(sync_client.from_attributed(b"no archive"), "")

    def test_backup_folder(self):
        dev = self.tmp / "backup" / "00008020-TEST"
        (dev / "3d").mkdir(parents=True)
        (dev / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
        (dev / "3d" / sync_client.SMS_DB).write_bytes(self.db.read_bytes())
        for given in (self.tmp / "backup", dev):
            self.assertEqual(sync_client.sms_db(given), (dev / "3d" / sync_client.SMS_DB, False))
        (dev / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
        with mock.patch.object(sync_client, "decrypt_sms_db", return_value=self.tmp / "plain.db") as dec:
            self.assertEqual(sync_client.sms_db(dev), (self.tmp / "plain.db", True))
        dec.assert_called_once_with(dev)
        with self.assertRaises(sync_client.Stop):
            sync_client.sms_db(self.tmp / "nothing-here")
        with self.assertRaises(sync_client.Stop):
            self.read(self.tmp / "backup" / "00008020-TEST" / "Manifest.plist")  # not a database


class TestClientEndToEnd(LiveServerTestCase):
    """The script as the server serves it, run like on a computer, against a live server."""

    def setUp(self):
        cache.clear()
        self._keyring = vault.fresh_keyring()
        self.user = make_user()
        self.dek = vault.key_for(self.user.pk)
        vault.keyring().clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.db = make_sms_db(self.tmp / "sms.db", phone_rows(self.now))
        self.script = self.tmp / "sync.py"
        import urllib.request
        with urllib.request.urlopen(f"{self.live_server_url}/sync.py") as r:  # noqa: S310
            self.script.write_bytes(r.read())

    def tearDown(self):
        vault.reset_keyring(self._keyring)

    def key(self) -> str:
        device, token = make_device(self.user, "computer")
        Device.objects.filter(pk=device.pk).update(expires_at=timezone.now() + timedelta(hours=24))
        return token

    def run_script(self, *args, answers=None):
        env = {**os.environ, "XDG_DATA_HOME": str(self.tmp / "data")}
        return subprocess.run([sys.executable, str(self.script), *args], input=answers, capture_output=True,
                              text=True, timeout=120, env=env)

    def test_the_page_command_sends_the_bank_sms_and_revokes_its_key(self):
        # exactly what the import page shows, in a shell, as the user pastes it
        cmd = app.sync_command(f"{self.live_server_url}/sync.py", self.key())
        r = subprocess.run(["sh", "-c", f"{cmd} --db '{self.db}' --period all --yes"], capture_output=True,
                           text=True, timeout=120, env={**os.environ, "XDG_DATA_HOME": str(self.tmp / "data")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Done: 3 new, 0 already there", r.stdout)
        self.assertEqual(Device.objects.live().count(), 0)  # revoked at the end
        self.assertEqual(Message.objects.filter(status=Message.PENDING).count(), 3)
        self.assertIn(self.now - timedelta(days=3), {m.received_at for m in Message.objects.all()})
        vault.keyring()[self.user.pk] = self.dek  # the owner opens the app
        self.assertEqual(ingest.process_pending(self.user), 3)
        self.assertEqual(Transaction.objects.count(), 3)
        again = self.run_script(self.key(), "--db", str(self.db), "--yes")
        self.assertIn("Done: 0 new, 3 already there", again.stdout)

    def test_asks_senders_and_period(self):
        # untick nothing, pick "last month": only the SMS from 3 days ago; confirm
        r = self.run_script(self.key(), "--db", str(self.db), answers="\n1\ny\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[ ]", r.stdout)  # the mobile number starts unticked
        self.assertIn("Send 1 SMS (Last month", r.stdout)
        self.assertEqual(Message.objects.count(), 1)

    def test_no_terminal_no_guessing(self):
        r = self.run_script(self.key(), "--db", str(self.db), answers="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no terminal", r.stdout)
        self.assertFalse(Message.objects.exists())

    def test_expired_key_says_what_to_do(self):
        token = self.key()
        Device.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        r = self.run_script(token, "--db", str(self.db), "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Make a new command", r.stdout)

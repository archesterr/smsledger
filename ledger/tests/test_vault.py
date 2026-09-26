"""Per-user encryption (vault.py): crypto, key states, the login/unlock/recovery flows, sessions
and security events, and the upgrade migration."""
import pathlib
import re
import unittest
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase
from django.utils import timezone

from ledger import audit, ingest, security, vault
from ledger.models import Account, Category, Message, SecurityEvent, Transaction, User, UserSession

from .helpers import PASSWORD, BaseTest, blu, login_client, make_device, make_user

MARK_AMOUNT = 918_273_645  # distinctive numbers/text that must never appear in the database
MARK_TEXT = "زعفران-قنادی-تستی"


class TestCrypto(unittest.TestCase):
    def test_rows_are_bound_to_table_and_owner(self):
        dek = bytes(range(32))
        blob = vault.seal_row(dek, "ledger.transaction", 7, {"amount": 5})
        self.assertEqual(vault.open_row(dek, "ledger.transaction", 7, blob), {"amount": 5})
        for label, uid in (("ledger.transaction", 8), ("ledger.budget", 7)):  # moved to another user / table
            with self.assertRaises(vault.BadKey):
                vault.open_row(dek, label, uid, blob)
        tampered = bytearray(blob)
        tampered[-1] ^= 1
        with self.assertRaises(vault.BadKey):
            vault.open_row(dek, "ledger.transaction", 7, bytes(tampered))
        with self.assertRaises(vault.BadKey):
            vault.open_row(bytes(32), "ledger.transaction", 7, blob)

    def test_same_value_encrypts_differently(self):
        dek = bytes(32)
        self.assertNotEqual(vault.seal_row(dek, "x", 1, {"a": 1}), vault.seal_row(dek, "x", 1, {"a": 1}))

    def test_password_and_recovery_wrapping(self):
        dek = bytes(range(32))
        w = vault.wrap_password(dek, "correct horse")
        self.assertEqual(vault.unwrap_password(w, "correct horse"), dek)
        with self.assertRaises(vault.BadKey):
            vault.unwrap_password(w, "Correct horse")
        code = vault.new_recovery_key()
        self.assertRegex(code, r"^([A-Z2-9]{4}-){7}[A-Z2-9]{4}$")
        w = vault.wrap_recovery(dek, code)
        # typed on a phone: lower case, spaces instead of dashes, Persian digits
        typed = code.lower().replace("-", " ").translate(str.maketrans("23456789", "۲۳۴۵۶۷۸۹"))
        self.assertEqual(vault.unwrap_recovery(w, typed), dek)
        with self.assertRaises(vault.BadKey):
            vault.unwrap_recovery(w, vault.new_recovery_key())

    def test_inbox_sealed_to_public_key(self):
        u = User(pk=3)
        dek = vault.new_keys(u)
        blob = vault.seal_inbox(u.vault_pub, 3, "پیامک")
        self.assertEqual(vault.open_inbox(vault.private_key(u, dek), 3, blob), "پیامک")
        with self.assertRaises(vault.BadKey):
            vault.open_inbox(vault.private_key(u, dek), 4, blob)  # other user's inbox


class TestAtRest(BaseTest):
    def test_database_holds_no_plaintext(self):
        u = make_user()
        ingest.ingest(u, [blu(MARK_AMOUNT, balance=MARK_AMOUNT + 1, name=MARK_TEXT)], "test")
        t = Transaction.objects.get(user=u)
        t.note = MARK_TEXT
        t.save()
        Account.objects.create(user=u, kind=Account.CASH, name=MARK_TEXT)
        Category.objects.create(user=u, name=MARK_TEXT)
        with connection.cursor() as c:
            dump = []
            for table in connection.introspection.table_names():
                c.execute(f'SELECT * FROM "{table}"')
                dump += [repr(row) for row in c.fetchall()]
        dump = "\n".join(dump)
        for needle in (str(MARK_AMOUNT), f"{MARK_AMOUNT:,}", MARK_TEXT, str(MARK_AMOUNT + 1)):
            self.assertNotIn(needle, dump)
        # and the app still reads it all back
        t = Transaction.objects.get(pk=t.pk)
        self.assertEqual((t.amount, t.note), (MARK_AMOUNT, MARK_TEXT))

    def test_rows_need_their_owners_key(self):
        a, b = make_user("a1"), make_user("b1")
        ingest.ingest(a, [blu(100, balance=5)], "t")
        vault.keyring().pop(a.pk)
        t = Transaction.objects.get(user=a)
        with self.assertRaises(vault.Locked):
            t.amount  # noqa: B018 (b's key in the ring doesn't open a's rows)
        self.assertIn(b.pk, vault.keyring())


class TestLoginAndKeys(BaseTest):
    def setUp(self):
        super().setUp()
        self.u = make_user("ali")
        ingest.ingest(self.u, [blu(5000, balance=9000)], "t")

    def post_login(self, password=PASSWORD, client=None):
        return (client or self.client).post("/login/", {"username": "ali", "password": password})

    def test_login_puts_the_key_in_session_and_cookie_only(self):
        r = self.post_login()
        self.assertEqual(r.status_code, 302)
        cookie = r.cookies[vault.cookie_name()]
        self.assertTrue(cookie["httponly"])
        from django.contrib.sessions.models import Session

        dek = vault.unlock_password(self.u, PASSWORD)
        stored = b"".join(s.session_data.encode() for s in Session.objects.all())
        self.assertNotIn(dek.hex().encode(), stored)
        self.assertEqual(self.client.get("/tx/").status_code, 200)
        # the session row alone (a stolen DB) can't open the data: without the cookie, signed out
        del self.client.cookies[vault.cookie_name()]
        r = self.client.get("/tx/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])

    def test_password_change_rewraps(self):
        self.login(self.u)
        r = self.client.post("/settings/", {"form": "password", "old_password": PASSWORD,
                                            "new_password1": "a-new-long-passphrase-7", "new_password2":
                                            "a-new-long-passphrase-7"})
        self.assertEqual(r.status_code, 302)
        self.u.refresh_from_db()
        self.assertEqual(self.u.vault_state, vault.PROTECTED)
        self.assertIsNone(vault.unlock_password(self.u, PASSWORD))
        dek = vault.unlock_password(self.u, "a-new-long-passphrase-7")
        self.assertEqual(dek, vault.key_for(self.u.pk))
        self.assertTrue(SecurityEvent.objects.filter(user=self.u, kind="password_changed").exists())

    def test_rehash_on_login_keeps_the_key(self):
        self.u.set_password(PASSWORD)  # what Django does when it upgrades the hash
        self.u.save()
        self.assertIsNotNone(vault.unlock_password(self.u, PASSWORD))

    def test_admin_reset_locks_then_recovery_key_opens(self):
        code = self.u.recovery_code
        vault.keyring().clear()  # the admin's request doesn't have Ali's key
        self.u.set_password("reset-by-admin-123")
        self.u.save()
        self.assertEqual(self.u.vault_state, vault.LOCKED)
        self.assertTrue(SecurityEvent.objects.filter(user=self.u, kind="password_reset").exists())

        r = self.post_login("reset-by-admin-123")
        self.assertEqual(self.client.get("/", follow=False)["Location"], "/unlock/")
        r = self.client.post("/unlock/", {"action": "recover", "code": vault.new_recovery_key(),
                                          "password": "reset-by-admin-123"})
        self.assertIn("درست نیست", r.content.decode())
        r = self.client.post("/unlock/", {"action": "recover", "code": code.lower(),
                                          "password": "reset-by-admin-123"})
        self.assertEqual(r["Location"], "/security/recovery-key/")
        self.u.refresh_from_db()
        self.assertEqual(self.u.vault_state, vault.PROTECTED)
        self.assertIsNone(self.u.recovery_saved_at)  # a fresh recovery key is required next
        self.assertEqual(self.client.get("/tx/")["Location"], "/security/recovery-key/")
        r = self.client.post("/security/recovery-key/", {"action": "create"})
        new_code = r.context["code"]
        self.assertNotEqual(new_code, code)
        self.client.post("/security/recovery-key/", {"action": "saved", "confirm": "on"})
        r = self.client.get("/tx/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["page"].paginator.count, 1)  # the data survived

    def test_locked_without_recovery_key_can_start_over(self):
        vault.keyring().clear()
        self.u.set_password("reset-by-admin-123")
        self.u.save()
        _, token = make_device(self.u)
        self.post_login("reset-by-admin-123")
        r = self.client.post("/unlock/", {"action": "reset", "password": "reset-by-admin-123", "confirm": "on"})
        self.assertEqual(r["Location"], "/security/recovery-key/")
        self.u.refresh_from_db()
        self.assertEqual(self.u.vault_state, vault.PROTECTED)
        self.assertFalse(Transaction.objects.filter(user=self.u).exists())
        self.assertTrue(Category.objects.filter(user=self.u).exists())  # fresh defaults
        # the phone keeps working with the new keys
        self.client.post("/security/recovery-key/", {"action": "create"})
        self.client.post("/security/recovery-key/", {"action": "saved", "confirm": "on"})
        self.assertEqual(self.post_sms(token, {"sms": blu(7, balance=7)}).json()["status"], "received")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(Transaction.objects.filter(user=self.u).count(), 1)

    def test_recovery_key_replacement_needs_password(self):
        self.login(self.u)
        r = self.client.post("/security/recovery-key/", {"action": "create"})
        self.assertNotIn("code", r.context)
        r = self.client.post("/security/recovery-key/", {"action": "create", "password": PASSWORD})
        self.assertIn(r.context["code"], r.content.decode())
        self.assertTrue(SecurityEvent.objects.filter(user=self.u, kind="recovery_key").exists())


class TestSignup(BaseTest):
    def test_new_user_gets_keys_and_must_save_a_recovery_key(self):
        from ledger.models import Invite

        code = security.new_invite_code()
        Invite.objects.create(code_hash=security.sha256(code), expires_at=timezone.now() + timedelta(days=1))
        r = self.client.post(f"/join/{code}/", {"username": "nima", "password1": PASSWORD, "password2": PASSWORD})
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(username="nima")
        self.assertEqual(u.vault_state, vault.PROTECTED)
        self.assertEqual(self.client.get("/")["Location"], "/security/recovery-key/")
        r = self.client.post("/security/recovery-key/", {"action": "create"})
        self.assertRegex(r.context["code"], r"^([A-Z2-9]{4}-){7}[A-Z2-9]{4}$")
        r = self.client.post("/security/recovery-key/", {"action": "saved"})  # checkbox not ticked
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/security/recovery-key/", {"action": "saved", "confirm": "on"})
        self.assertEqual(r["Location"], "/setup/")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertTrue(Category.objects.filter(user=u).exists())


class TestSessionsAndEvents(BaseTest):
    def setUp(self):
        super().setUp()
        self.u = make_user("ali")

    def login_real(self, client, password=PASSWORD, ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                                          "AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1"):
        return client.post("/login/", {"username": "ali", "password": password}, HTTP_USER_AGENT=ua,
                           REMOTE_ADDR="5.120.33.44")

    def test_sessions_listed_and_revoked_remotely(self):
        phone, laptop = self.client, Client()
        self.login_real(phone)
        self.login_real(laptop, ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Chrome/120.0 Safari/537.36")
        r = phone.get("/security/")
        sessions = r.context["sessions"]
        self.assertEqual(len(sessions), 2)
        self.assertEqual({s.agent for s in sessions}, {"iPhone · Safari", "Mac · Chrome"})
        self.assertEqual({s.ip for s in sessions}, {"5.120.33.x"})
        other = next(s for s in sessions if s.pk != phone.session[audit.SID])
        phone.post(f"/security/sessions/{other.pk}/revoke/")
        r = laptop.get("/tx/")
        self.assertIn("/login/", r["Location"])
        self.assertEqual(len(phone.get("/security/").context["sessions"]), 1)
        # can't revoke your own session from the list (that's the logout button)
        phone.post(f"/security/sessions/{phone.session[audit.SID]}/revoke/")
        self.assertEqual(phone.get("/tx/").status_code, 200)

    def test_sign_out_everywhere_else_and_on_password_change(self):
        a, b, c = self.client, Client(), Client()
        for cl in (a, b, c):
            self.login_real(cl)
        a.post("/security/sessions/revoke-others/")
        self.assertIn("/login/", b.get("/").get("Location", ""))
        self.assertIn("/login/", c.get("/").get("Location", ""))
        self.assertEqual(UserSession.objects.filter(user=self.u, ended_at__isnull=True).count(), 1)
        self.assertTrue(SecurityEvent.objects.filter(user=self.u, kind="sessions_revoked").exists())

    def test_failed_logins_are_recorded_and_shown(self):
        attacker = Client()
        for _ in range(3):
            self.login_real(attacker, password="guess")
        attacker.post("/login/", {"username": "nobody", "password": "x"})  # unknown user: nothing to record
        self.assertEqual(SecurityEvent.objects.filter(kind="login_failed").count(), 3)
        self.login_real(self.client)
        r = self.client.get("/")
        self.assertEqual(r.context["failed_logins"], 0)  # first login: nothing "since last time"
        self.client.post("/logout/")
        for _ in range(2):
            self.login_real(attacker, password="guess")
        self.login_real(self.client)
        r = self.client.get("/")
        self.assertEqual(r.context["failed_logins"], 2)
        self.assertIn("تلاش ناموفق", r.content.decode())
        events = self.client.get("/security/").context["events"]
        self.assertEqual(events[0].kind, "login")
        self.assertTrue(events[1].warn)

    def test_lockout_recorded_once(self):
        for _ in range(14):
            self.client.post("/login/", {"username": "ali", "password": "wrong"})
        self.assertEqual(SecurityEvent.objects.filter(user=self.u, kind="login_blocked").count(), 1)

    def test_security_page_checks(self):
        self.login(self.u)
        r = self.client.get("/security/")
        checks = {c["title"]: c["ok"] for c in r.context["checks"]}
        self.assertTrue(checks["رمزنگاری داده‌ها"])
        self.assertTrue(checks["کلید بازیابی"])
        self.assertFalse(checks["ورود دو مرحله‌ای"])
        self.assertEqual((r.context["score"], r.context["total"]), (4, 5))
        d, _ = make_device(self.u)
        d.last_used_at = timezone.now() - timedelta(days=40)
        d.save()
        r = self.client.get("/security/")
        self.assertEqual(r.context["score"], 3)
        self.assertTrue(r.context["devices"][0].stale)

    def test_export_needs_password_and_is_logged(self):
        self.login(self.u)
        r = self.client.get("/settings/export.csv")
        self.assertEqual(r["Content-Type"], "text/html; charset=utf-8")
        r = self.client.post("/settings/export.csv", {"password": "wrong"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/settings/export.csv", {"password": PASSWORD})
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(SecurityEvent.objects.filter(user=self.u, kind="export").count(), 1)

    def test_disabled_by_staff_is_visible_and_ends_sessions(self):
        self.login_real(self.client)
        boss = make_user("boss", is_staff=True, totp_secret=security.totp_new_secret())
        staff = Client()
        login_client(staff, boss)
        staff.post(f"/staff/users/{self.u.pk}/toggle/")
        self.assertIn("/login/", self.client.get("/").get("Location", ""))
        self.assertTrue(SecurityEvent.objects.filter(user=self.u, kind="disabled").exists())

    def test_event_labels_cover_every_kind_used(self):
        app = pathlib.Path(__file__).resolve().parents[1]
        used = {"enabled", "disabled", "password_reset"}  # passed as expressions, not literals
        for f in app.rglob("*.py"):
            if "tests" not in f.parts:
                used |= set(re.findall(r'audit\.record\([^,]+,\s*"(\w+)"', f.read_text()))
        self.assertGreater(len(used), 15)
        self.assertEqual(used - set(SecurityEvent.KINDS), set())


class TestUpgradedAccounts(BaseTest):
    """Accounts from before encryption: key readable by the server until their next login."""

    def setUp(self):
        super().setUp()
        self.u = make_user("old")
        ingest.ingest(self.u, [blu(4000, balance=6000)], "t")
        dek = vault.key_for(self.u.pk)
        self.u.key_srv, self.u.key_pw, self.u.key_rec, self.u.recovery_saved_at = dek, b"", b"", None
        self.u.save()
        vault.keyring().clear()

    def test_phone_sms_still_recorded_at_once(self):
        _, token = make_device(self.u)
        self.assertEqual(self.post_sms(token, {"sms": blu(1000, balance=5000)}).json()["status"], "created")

    def test_old_session_must_sign_in_again_which_protects_the_key(self):
        self.client.force_login(self.u)  # a session from before the upgrade
        r = self.client.get("/tx/")
        self.assertIn("/login/", r["Location"])
        r = self.client.post("/login/", {"username": "old", "password": PASSWORD})
        self.u.refresh_from_db()
        self.assertEqual(self.u.vault_state, vault.PROTECTED)
        self.assertEqual(bytes(self.u.key_srv), b"")
        self.assertTrue(SecurityEvent.objects.filter(user=self.u, kind="vault_on").exists())
        self.assertEqual(self.client.get("/tx/")["Location"], "/security/recovery-key/")
        self.assertEqual(audit.active(self.u).count(), 1)  # no phantom row for the old session

    def test_startup_reparse_only_touches_server_readable_accounts(self):
        other = make_user("new")
        Message.objects.create(user=other, hash="n", raw="بانک ناشناس\n1", status=Message.UNPARSED)
        vault.keyring().clear()
        Message.objects.create(user=self.u, hash="o", raw="بانک ناشناس\n2", status=Message.UNPARSED)
        r = ingest.reparse()
        self.assertEqual(r["checked"], 1)


class TestUpgradeMigration(TransactionTestCase):
    """0003-0005 on real pre-encryption data."""

    before = [("ledger", "0002_account_brand")]
    after = [("ledger", "0006_vault_vacuum")]

    def test_existing_data_is_encrypted_and_readable(self):
        ex = MigrationExecutor(connection)
        ex.migrate(self.before)
        apps = ex.loader.project_state(self.before).apps
        OldUser = apps.get_model("ledger", "User")
        OldAccount, OldCategory = apps.get_model("ledger", "Account"), apps.get_model("ledger", "Category")
        OldMessage, OldTx = apps.get_model("ledger", "Message"), apps.get_model("ledger", "Transaction")
        OldRule, OldBudget = apps.get_model("ledger", "Rule"), apps.get_model("ledger", "Budget")
        from django.contrib.auth.hashers import make_password

        u = OldUser.objects.create(username="legacy", password=make_password(PASSWORD))
        acc = OldAccount.objects.create(user=u, kind="bank", bank="melli", hint="7007", name="ملی 7007")
        cat = OldCategory.objects.create(user=u, kind="expense", name=MARK_TEXT)
        msg = OldMessage.objects.create(user=u, hash="old", raw=blu(MARK_AMOUNT, balance=1), status="parsed")
        OldTx.objects.create(user=u, account=acc, message=msg, source="sms", direction="OUT", amount=MARK_AMOUNT,
                             balance=1, occurred_at=timezone.now(), jdate="1405-07-01", title="برداشت", note="یادداشت",
                             category=cat, gap_amount=-5)
        OldRule.objects.create(user=u, category=cat, text="اسنپ", amount_min=10)
        OldBudget.objects.create(user=u, category=cat, amount=123)

        ex = MigrationExecutor(connection)
        ex.loader.build_graph()
        ex.migrate(self.after)

        with connection.cursor() as c:
            c.execute("SELECT * FROM ledger_transaction")
            self.assertNotIn(str(MARK_AMOUNT), repr(c.fetchall()))
        user = User.objects.get(username="legacy")
        self.assertEqual(user.vault_state, vault.UNPROTECTED)
        t = Transaction.objects.get(user=user)
        self.assertEqual((t.amount, t.balance, t.note, t.gap_amount, t.has_gap), (MARK_AMOUNT, 1, "یادداشت", -5, True))
        self.assertEqual((t.account.name, t.account.hint, t.category.name), ("ملی 7007", "7007", MARK_TEXT))
        self.assertEqual(t.account.hint_idx, vault.blind(vault.key_for(user.pk), "7007"))
        self.assertEqual(t.message.raw, blu(MARK_AMOUNT, balance=1))
        self.assertEqual(t.message.hash, vault.fingerprint(user, t.message.raw))  # dedupe keeps working
        self.assertEqual(ingest.ingest(user, [blu(MARK_AMOUNT, balance=1)], "t")[0]["status"], "duplicate")
        from ledger.models import Budget, Rule

        self.assertEqual((Rule.objects.get(user=user).text, Rule.objects.get(user=user).amount_min), ("اسنپ", 10))
        self.assertEqual(Budget.objects.get(user=user).amount, 123)
        # first login after the upgrade moves the key under the password
        self.client.post("/login/", {"username": "legacy", "password": PASSWORD})
        user.refresh_from_db()
        self.assertEqual(user.vault_state, vault.PROTECTED)
        vault.keyring().clear()
        with self.assertRaises(vault.Locked):
            Transaction.objects.get(user=user).amount  # noqa: B018 (server alone can't read it any more)

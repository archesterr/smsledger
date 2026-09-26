import json
from datetime import datetime, timedelta
from unittest import mock

from django.test import Client
from django.utils import timezone

from ledger import jalali, security
from ledger.models import Invite, Message, Transaction
from ledger.views.app import import_periods

from .helpers import BaseTest, blu, login_client, make_user

UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_16 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
MELLI_NO_YEAR = "بانك ملي ايران\nانتقال:1,000-\nحساب:97007\nمانده:5,000\n1228-10:00"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class TestInvite(BaseTest):
    def setUp(self):
        super().setUp()
        self.admin = make_user("boss", is_staff=True, totp_secret=security.totp_new_secret())
        self.login(self.admin)

    def test_ready_to_send_message(self):
        self.client.post("/staff/invites/", {"note": "علی"})
        r = self.client.get("/staff/")
        msg, link = r.context["invite_message"], r.context["new_link"]
        self.assertTrue(msg.startswith("سلام علی 👋"))
        self.assertIn(link, msg)
        self.assertIn("Safari", msg)
        _, jm, jd = jalali.of(Invite.objects.get().expires_at)
        self.assertIn(jalali.MONTHS[jm - 1], msg)
        self.assertIn('id="invite-msg"', r.content.decode())
        self.assertIsNone(self.client.get("/staff/").context["invite_message"])  # shown once

    def test_note_is_not_escaped_twice(self):
        self.client.post("/staff/invites/", {"note": "Sara & Reza"})
        r = self.client.get("/staff/")
        self.assertIn("سلام Sara & Reza", r.context["invite_message"])
        self.assertIn("سلام Sara &amp; Reza", r.content.decode())

    def test_join_page_is_the_guide(self):
        self.client.post("/staff/invites/", {"note": ""})
        path = self.client.get("/staff/").context["new_link"].split("testserver", 1)[1]
        self.client.logout()
        page = self.client.get(path, HTTP_USER_AGENT=UA_IPHONE).content.decode()
        for step in ("ساخت حساب", "کلید بازیابی", "نصب میان‌بر", "اتصال این آیفون", "اتوماسیون پیامک",
                     "Add to Home Screen"):
            self.assertIn(step, page)
        self.assertIn("<b>boss</b> شما را دعوت کرده", page)
        self.assertNotIn('class="qr', page)
        desktop = self.client.get(path, HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)").content.decode()
        self.assertIn('class="qr join-qr"', desktop)  # carry on on the phone
        r = self.client.post(path, {"username": "newfriend", "password1": "a-good-long-pass-99",
                                    "password2": "a-good-long-pass-99"})
        self.assertEqual(r["Location"], "/setup/")


class TestImportPeriods(BaseTest):
    def test_shamsi_month_boundaries(self):
        with mock.patch.object(jalali, "today", return_value=(1405, 7, 4)):
            p = {x["key"]: x for x in import_periods()}
        self.assertEqual(p["month"]["hint"], "از ۱ شهریور")
        self.assertEqual(p["month"]["start"], ms(jalali.day_start(1405, 6, 1)))
        self.assertEqual(p["3months"]["hint"], "از ۱ تیر")
        self.assertEqual(p["year"]["hint"], "از ۱ مهر ۱۴۰۴")
        self.assertEqual(p["year"]["start"], ms(jalali.day_start(1404, 7, 1)))
        self.assertEqual(p["year"]["en"], "Last year (since 1 Mehr 1404)")  # for the terminal
        self.assertEqual(p["all"]["start"], 0)

    def test_across_the_new_year(self):
        with mock.patch.object(jalali, "today", return_value=(1405, 2, 10)):
            p = {x["key"]: x for x in import_periods()}
        self.assertEqual(p["month"]["hint"], "از ۱ فروردین")
        self.assertEqual(p["3months"]["start"], ms(jalali.day_start(1404, 11, 1)))  # 1 Bahman last year


class TestImportBatch(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.login(self.user)

    def post(self, items, client=None):
        return (client or self.client).post("/import/batch/", data=json.dumps({"items": items}),
                                            content_type="application/json")

    def test_backup_sms_keep_their_arrival_time(self):
        arrived = datetime(2025, 3, 22, 9, 0, tzinfo=jalali.TEHRAN)  # 1404-01-02
        r = self.post([{"text": MELLI_NO_YEAR, "at": ms(arrived)}, {"text": blu(100, balance=900), "at": ms(arrived)},
                       {"text": "کد تایید شما 1234", "at": ms(arrived)}])
        self.assertEqual(r.json(), {"count": {"created": 2, "ignored": 1}})
        melli = Transaction.objects.get(account__bank="melli")
        self.assertEqual(timezone.localtime(melli.occurred_at).date().isoformat(), "2025-03-18")  # 1403-12-28
        self.assertEqual({m.received_at for m in Message.objects.all()}, {arrived})
        self.assertEqual({m.source for m in Message.objects.all()}, {"backup"})
        again = self.post([{"text": MELLI_NO_YEAR, "at": ms(arrived)}])
        self.assertEqual(again.json(), {"count": {"duplicate": 1}})

    def test_unusable_times_fall_back_to_now(self):
        before = timezone.now()
        future = ms(timezone.now() + timedelta(days=30))
        for i, at in enumerate(("yesterday", True, 10 ** 30, -5, future, None)):
            self.assertEqual(self.post([{"text": blu(10 + i, balance=1000 - i)}] if at is None else
                                       [{"text": blu(10 + i, balance=1000 - i), "at": at}]).status_code, 200)
        self.assertTrue(all(m.received_at >= before for m in Message.objects.all()))
        self.assertEqual(Message.objects.count(), 6)

    def test_bad_requests(self):
        for body in ("nope", json.dumps([1]), json.dumps({"items": []}), json.dumps({"items": "x"}),
                     json.dumps({"items": [1, 2]}), json.dumps({"items": [{"text": "x"}] * 501})):
            r = self.client.post("/import/batch/", data=body, content_type="application/json")
            self.assertEqual(r.status_code, 400, body[:40])
        self.assertFalse(Message.objects.exists())

    def test_needs_login_and_csrf(self):
        self.assertEqual(self.post([{"text": blu(1, balance=1)}], Client()).status_code, 302)  # to login
        strict = Client(enforce_csrf_checks=True)
        login_client(strict, self.user)
        self.assertEqual(self.post([{"text": blu(1, balance=1)}], strict).status_code, 403)
        self.assertFalse(Message.objects.exists())

    def test_import_page(self):
        r = self.client.get("/import/")
        page = r.content.decode()
        self.assertIn("'wasm-unsafe-eval'", r["Content-Security-Policy"])
        self.assertNotIn("wasm-unsafe-eval", self.client.get("/")["Content-Security-Policy"])
        for label in ("ماه گذشته", "سه ماه اخیر", "یک سال اخیر", "همه"):
            self.assertIn(label, page)
        self.assertIn("sql-wasm-browser.js", page)
        self.assertIn("3d0d7e5fb2ce288813306e4d4636395e047a3d28", page)
        self.assertIn('name="csrfmiddlewaretoken"', page)

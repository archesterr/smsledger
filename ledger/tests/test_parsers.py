"""Parser + Jalali tests. Pure Python: no database needed."""
import unittest
from datetime import date, datetime, timedelta

from ledger import jalali, parsers

BLU_OUT = """بلو
برداشت پول
آرمین عزیز، 1,000,000 ریال از حساب شما پرید.
موجودی: 2,887,139 ریال
۱۴:۰۳
۱۴۰۵.۰۶.۳۱"""

# hypothetical deposit wording — replace with a real sample when you have one
BLU_IN = """بلو
واریز پول
آرمین عزیز، ۵۰۰٬۰۰۰ ریال به حساب شما نشست.
موجودی: ۳٬۳۸۷٬۱۳۹ ریال
۰۹:۱۵
۱۴۰۵.۰۷.۰۱"""


class TestJalali(unittest.TestCase):
    def test_known_dates(self):
        self.assertEqual(jalali.to_gregorian(1405, 1, 1), (2026, 3, 21))
        self.assertEqual(jalali.to_gregorian(1405, 6, 31), (2026, 9, 22))
        self.assertEqual(jalali.to_gregorian(1405, 7, 1), (2026, 9, 23))
        self.assertEqual(jalali.to_gregorian(1403, 12, 30), (2025, 3, 20))  # leap Esfand
        self.assertEqual(jalali.to_gregorian(1399, 10, 11), (2020, 12, 31))

    def test_round_trip_every_day_1300_1499(self):
        d, end = jalali.to_date(1300, 1, 1), jalali.to_date(1500, 1, 1)
        while d < end:
            jy, jm, jd = jalali.from_date(d)
            self.assertTrue(1 <= jd <= jalali.month_length(jy, jm), (d, jy, jm, jd))
            self.assertEqual(jalali.to_date(jy, jm, jd), d)
            d += timedelta(days=1)

    def test_month_helpers(self):
        self.assertEqual(jalali.month_length(1403, 12), 30)
        self.assertEqual(jalali.month_length(1404, 12), 29)
        self.assertEqual(jalali.month_length(1405, 7), 30)
        self.assertEqual(jalali.add_months(1405, 12, 1), (1406, 1))
        self.assertEqual(jalali.add_months(1405, 1, -1), (1404, 12))
        start, end = jalali.month_range(1405, 7)
        self.assertEqual(start.date(), date(2026, 9, 23))
        self.assertEqual(end.date(), date(2026, 10, 23))

    def test_parse(self):
        self.assertEqual(jalali.parse("۱۴۰۵/۷/۱"), (1405, 7, 1))
        self.assertEqual(jalali.parse("1405-07-01"), (1405, 7, 1))
        for bad in ("1405/13/01", "1405/07/31", "1404/12/30", "hello", ""):
            with self.assertRaises(ValueError):
                jalali.parse(bad)
        self.assertEqual(jalali.parse_month("۱۴۰۵-۰۷"), (1405, 7))


class TestBlu(unittest.TestCase):
    def test_withdrawal(self):
        tx, err = parsers.parse(BLU_OUT)
        self.assertIsNone(err)
        self.assertEqual((tx.bank, tx.direction, tx.amount, tx.balance), ("blu", "OUT", 1_000_000, 2_887_139))
        self.assertEqual(tx.occurred_at.isoformat(), "2026-09-22T14:03:00+03:30")
        self.assertEqual(tx.title, "برداشت پول")

    def test_deposit_persian_digits(self):
        tx, err = parsers.parse(BLU_IN)
        self.assertIsNone(err)
        self.assertEqual((tx.direction, tx.amount, tx.balance), ("IN", 500_000, 3_387_139))
        self.assertEqual(tx.occurred_at.isoformat(), "2026-09-23T09:15:00+03:30")

    def test_old_sms_uses_historical_dst(self):
        # Iran observed DST until 2022; a backfilled summer-2021 SMS is +04:30, not +03:30
        old = BLU_OUT.replace("۱۴۰۵.۰۶.۳۱", "۱۴۰۰.۰۴.۰۱")
        tx, _ = parsers.parse(old)
        self.assertEqual(tx.occurred_at.isoformat(), "2021-06-22T14:03:00+04:30")

    def test_invisible_marks_and_crlf(self):
        noisy = "‏" + BLU_OUT.replace("\n", "\r\n  ").replace("1,000,000", "1,000,000‎")
        tx, err = parsers.parse(noisy)
        self.assertIsNone(err)
        self.assertEqual(tx.amount, 1_000_000)
        self.assertEqual(parsers.fingerprint(noisy), parsers.fingerprint(BLU_OUT))

    def test_unknown_bank(self):
        tx, err = parsers.parse("سلام\nخوبی؟")
        self.assertIsNone(tx)
        self.assertEqual(err, "no parser matched")

    def test_bad_date_is_not_a_crash(self):
        tx, err = parsers.parse(BLU_OUT.replace("۱۴۰۵.۰۶.۳۱", "۱۴۰۵.۱۳.۴۰"))
        self.assertIsNone(err)
        self.assertIsNone(tx.occurred_at)

    def test_zero_amount_rejected(self):
        tx, err = parsers.parse(BLU_OUT.replace("1,000,000", "0"))
        self.assertIsNone(tx)
        self.assertIn("zero amount", err)


class TestSensitive(unittest.TestCase):
    def test_otp_and_login_codes(self):
        for text in (
            "بانک ملت\nرمز پویا: 12345678\nمبلغ: 1,000,000 ریال\nپذیرنده: فروشگاه",
            "رمز دوم یکبار مصرف شما 889900",
            "بلو\nکد ورود شما: 123456",
            "کد تأیید: 5521",
            "کد‌تایید 5521",
            "Your OTP is 123456",
            "یک‌بار مصرف: 1234",
        ):
            self.assertTrue(parsers.is_sensitive(text), text)

    def test_normal_transactions_pass(self):
        for text in (
            BLU_OUT, BLU_IN,
            "بلو\nانتقال پول\nانتقال به پویا احمدی\n1,000 ریال از حساب شما پرید.",  # Pouya is a name
            "خرید رمزارز 1,000,000 ریال از حساب شما",
            "کد پیگیری: 123456\n500,000 ریال به حساب شما نشست.",
        ):
            self.assertFalse(parsers.is_sensitive(text), text)


class TestTxJson(unittest.TestCase):
    def test_as_json(self):
        tx = parsers.Tx("blu", "OUT", 10, None, datetime(2026, 9, 22, 14, 3, tzinfo=parsers.TEHRAN))
        self.assertEqual(tx.as_json()["occurred_at"], "2026-09-22T14:03:00+03:30")


if __name__ == "__main__":
    unittest.main()


# Real samples (1405-07-01), sent by the owner. Deposit variants are guesses from the same layout:
# replace them with real ones when available.
REF = datetime(2026, 9, 23, 21, 0, tzinfo=parsers.TEHRAN)  # 1405-07-01 21:00

BLU_REAL = """بلو
برداشت پول
آرمین عزیز، 1,000,000 ریال از حساب شما پرید.
موجودی: 1,887,139 ریال
۲۰:۲۵
۱۴۰۵.۰۷.۰۱"""

SAMAN_OUT = """بانك سامان
برداشت مبلغ 20,000,000 انتقال وجه
از ‪884-800-4076959-1‬
مانده 36,634,778
1405/7/1
18:50:02"""

KHAV_OUT = """بانک خاورمیانه
خرید با کارت 0947
-4,450,000
020/000790644
مانده 63,295,053
06/31
20:35"""

PASARGAD_OUT = """777.888.19516768.1
-3,200,000
07/01_16:55
مانده: 1,178,259"""

MELLI_OUT = """بانك ملي ايران
انتقال:80,035,500-
حساب:97007
مانده:35,206,324
0629-18:26"""


class TestOtherBanks(unittest.TestCase):
    def check(self, raw, bank, direction, amount, balance, when, title, account):
        tx, err = parsers.parse(raw, REF)
        self.assertIsNone(err, err)
        self.assertEqual((tx.bank, tx.direction, tx.amount, tx.balance), (bank, direction, amount, balance))
        self.assertEqual(tx.occurred_at.isoformat(), when)
        self.assertEqual((tx.title, tx.account), (title, account))

    def test_real_samples(self):
        self.check(BLU_REAL, "blu", "OUT", 1_000_000, 1_887_139, "2026-09-23T20:25:00+03:30", "برداشت پول", "")
        self.check(SAMAN_OUT, "saman", "OUT", 20_000_000, 36_634_778, "2026-09-23T18:50:00+03:30",
                   "انتقال وجه", "9591")
        self.check(KHAV_OUT, "khavarmianeh", "OUT", 4_450_000, 63_295_053, "2026-09-22T20:35:00+03:30",
                   "خرید با کارت", "0644")
        self.check(PASARGAD_OUT, "pasargad", "OUT", 3_200_000, 1_178_259, "2026-09-23T16:55:00+03:30",
                   "برداشت", "7681")
        self.check(MELLI_OUT, "melli", "OUT", 80_035_500, 35_206_324, "2026-09-20T18:26:00+03:30",
                   "انتقال", "7007")

    def test_deposits(self):
        self.check(SAMAN_OUT.replace("برداشت مبلغ", "واریز مبلغ").replace("از ", "به "), "saman", "IN",
                   20_000_000, 36_634_778, "2026-09-23T18:50:00+03:30", "انتقال وجه", "9591")
        self.check(KHAV_OUT.replace("خرید با کارت 0947", "واریز").replace("-4,450,000", "+4,450,000"),
                   "khavarmianeh", "IN", 4_450_000, 63_295_053, "2026-09-22T20:35:00+03:30", "واریز", "0644")
        self.check(PASARGAD_OUT.replace("-3,200,000", "+3,200,000"), "pasargad", "IN", 3_200_000, 1_178_259,
                   "2026-09-23T16:55:00+03:30", "واریز", "7681")
        self.check(MELLI_OUT.replace("انتقال:80,035,500-", "واریز:80,035,500+"), "melli", "IN", 80_035_500,
                   35_206_324, "2026-09-20T18:26:00+03:30", "واریز", "7007")

    def test_unsigned_amount_falls_back_to_title(self):
        self.check(MELLI_OUT.replace("انتقال:80,035,500-", "خرید:80,035,500"), "melli", "OUT", 80_035_500,
                   35_206_324, "2026-09-20T18:26:00+03:30", "خرید", "7007")

    def test_broken_layouts_are_unparsed_not_wrong(self):
        for raw in (SAMAN_OUT.replace("مبلغ 20,000,000", "مبلغ"),
                    KHAV_OUT.replace("-4,450,000", ""),
                    PASARGAD_OUT.replace("-3,200,000", "سلام"),
                    MELLI_OUT.replace("انتقال:80,035,500-", "انتقال:80,035,500+-")):
            tx, err = parsers.parse(raw, REF)
            self.assertIsNone(tx, raw)
            self.assertTrue(err)

    def test_pasargad_needs_its_account_format(self):
        # a message whose first line is just some number must not be taken for Pasargad
        tx, err = parsers.parse("12345\n-3,200,000\n07/01_16:55", REF)
        self.assertEqual((tx, err), (None, "no parser matched"))

    def test_full_account_numbers_are_not_kept_as_hint(self):
        self.assertEqual(parsers.last4("884-800-4076959-1"), "9591")
        self.assertEqual(parsers.last4("777.888.19516768.1"), "7681")


class TestYearInference(unittest.TestCase):
    def test_same_year(self):
        self.assertEqual(parsers.infer_datetime(6, 31, 20, 35, REF).isoformat(), "2026-09-22T20:35:00+03:30")

    def test_new_year_rollover(self):
        # an Esfand SMS read in Farvardin belongs to last year
        ref = datetime(2026, 3, 23, 12, 0, tzinfo=parsers.TEHRAN)  # 1405-01-03
        self.assertEqual(parsers.infer_datetime(12, 29, 10, 0, ref).date().isoformat(), "2026-03-20")  # 1404-12-29

    def test_tomorrow_allowed_for_clock_skew_but_not_later(self):
        self.assertEqual(parsers.infer_datetime(7, 2, 0, 30, REF).date().isoformat(), "2026-09-24")
        self.assertEqual(parsers.infer_datetime(7, 5, 0, 0, REF).date().isoformat(), "2025-09-27")  # 1404-07-05

    def test_invalid_dates(self):
        self.assertIsNone(parsers.infer_datetime(13, 1, ref=REF))
        self.assertIsNone(parsers.infer_datetime(7, 31, ref=REF))  # Mehr has 30 days
        self.assertEqual(parsers.infer_datetime(6, 31, 25, 99, REF).isoformat(), "2026-09-22T00:00:00+03:30")

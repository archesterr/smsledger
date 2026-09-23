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

import unittest

from app import jalali, parsers
from app.server import gaps

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


class TestBlu(unittest.TestCase):
    def test_withdrawal(self):
        tx, err = parsers.parse(BLU_OUT)
        self.assertIsNone(err)
        self.assertEqual((tx.bank, tx.direction, tx.amount, tx.balance), ("blu", "OUT", 1_000_000, 2_887_139))
        self.assertEqual(tx.occurred_at, "2026-09-22T14:03:00+03:30")
        self.assertEqual(tx.jdate, "1405-06-31")
        self.assertEqual(tx.title, "برداشت پول")

    def test_deposit_persian_digits(self):
        tx, err = parsers.parse(BLU_IN)
        self.assertIsNone(err)
        self.assertEqual((tx.direction, tx.amount, tx.balance), ("IN", 500_000, 3_387_139))
        self.assertEqual(tx.occurred_at, "2026-09-23T09:15:00+03:30")

    def test_invisible_marks_and_crlf(self):
        noisy = "‏" + BLU_OUT.replace("\n", "\r\n  ").replace("1,000,000", "1,000,000‎")
        tx, err = parsers.parse(noisy)
        self.assertIsNone(err)
        self.assertEqual(tx.amount, 1_000_000)
        self.assertEqual(parsers.fingerprint(noisy), parsers.fingerprint(BLU_OUT))

    def test_unknown_bank(self):
        tx, err = parsers.parse("سلام\nکد تایید: 12345")
        self.assertIsNone(tx)
        self.assertEqual(err, "no parser matched")

    def test_blu_otp_is_unparsed_not_crash(self):
        tx, err = parsers.parse("بلو\nکد ورود شما: 123456")
        self.assertIsNone(tx)
        self.assertIn("amount not found", err)


class TestGaps(unittest.TestCase):
    def test_gap_detection(self):
        rows = [
            {"id": 1, "bank": "blu", "direction": "OUT", "amount": 100, "balance": 900},
            {"id": 2, "bank": "blu", "direction": "IN", "amount": 50, "balance": 950},
            {"id": 3, "bank": "blu", "direction": "OUT", "amount": 10, "balance": 500},  # missed SMS before
        ]
        self.assertEqual(gaps(rows), {3})


if __name__ == "__main__":
    unittest.main()

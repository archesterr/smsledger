import base64
import unittest

from django.core.cache import cache
from django.test import SimpleTestCase

from ledger import charts, money, security
from ledger.views.app import csv_safe

RFC_KEY = b"12345678901234567890"
RFC_SECRET = base64.b32encode(RFC_KEY).decode().rstrip("=")


class TestHotpTotp(unittest.TestCase):
    def test_rfc4226_vectors(self):
        expected = ["755224", "287082", "359152", "969429", "338314",
                    "254676", "287922", "162583", "399871", "520489"]
        self.assertEqual([security.hotp(RFC_KEY, c) for c in range(10)], expected)

    def test_rfc6238_sha1_vectors(self):
        for t, code in [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                        (1234567890, "89005924"), (2000000000, "69279037")]:
            self.assertEqual(security.hotp(RFC_KEY, t // 30, digits=8), code)

    def test_verify_window_and_replay(self):
        code = security.hotp(RFC_KEY, 1)  # step 1 = t in [30, 60)
        self.assertEqual(security.totp_verify(RFC_SECRET, code, 0, now=59), 1)
        self.assertEqual(security.totp_verify(RFC_SECRET, code, 0, now=89), 1)      # one step of drift
        self.assertIsNone(security.totp_verify(RFC_SECRET, code, 0, now=150))      # too old
        self.assertIsNone(security.totp_verify(RFC_SECRET, code, 1, now=59))       # replay
        self.assertEqual(security.totp_verify(RFC_SECRET, f" {code[:3]} {code[3:]} ", 0, now=59), 1)
        fa = code.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        self.assertEqual(security.totp_verify(RFC_SECRET, fa, 0, now=59), 1)       # Persian keyboard
        for bad in ("", "12345", "abcdef", "1234567"):
            self.assertIsNone(security.totp_verify(RFC_SECRET, bad, 0, now=59))

    def test_tokens(self):
        t = security.new_token()
        self.assertTrue(t.startswith("sml_") and len(t) > 40)
        self.assertNotEqual(t, security.new_token())
        self.assertEqual(security.recovery_hash("AB12-cd34"), security.recovery_hash("ab12cd34"))

    def test_totp_uri(self):
        uri = security.totp_uri("ABC", "ali", "دخل و خرج")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=ABC", uri)


class TestLimiter(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_blocks_after_limit(self):
        lim = security.Limiter("t", 3, 60)
        for _ in range(3):
            self.assertFalse(lim.blocked("x"))
            lim.hit("x")
        self.assertTrue(lim.blocked("x"))
        self.assertFalse(lim.blocked("y"))
        lim.reset("x")
        self.assertFalse(lim.blocked("x"))


class TestMoney(unittest.TestCase):
    def test_format(self):
        self.assertEqual(money.format_amount(2_887_139, "rial"), "۲٬۸۸۷٬۱۳۹")
        self.assertEqual(money.format_amount(2_887_139, "toman"), "۲۸۸٬۷۱۳٫۹")
        self.assertEqual(money.format_amount(-1_000_000, "toman"), "−۱۰۰٬۰۰۰")
        self.assertEqual(money.format_amount(500, "toman", sign=True), "+۵۰")
        self.assertEqual(money.format_amount(None), "")

    def test_parse(self):
        self.assertEqual(money.parse_amount("۱۲۰٬۰۰۰", "toman"), 1_200_000)
        self.assertEqual(money.parse_amount("1,200,000", "rial"), 1_200_000)
        self.assertEqual(money.parse_amount("12.5", "toman"), 125)
        self.assertEqual(money.parse_amount("0", "toman", allow_zero=True), 0)
        for bad in ("", "abc", "-5", "0", "1.25", "NaN", "Infinity", "1e30"):
            with self.assertRaises(ValueError, msg=bad):
                money.parse_amount(bad, "toman")
        self.assertEqual(money.format_input(1_234_565, "toman"), "123456.5")


class TestCsvSafe(unittest.TestCase):
    def test_formula_injection(self):
        for evil in ("=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\tx"):
            self.assertTrue(csv_safe(evil).startswith("'"), evil)
        self.assertEqual(csv_safe("بلو"), "بلو")
        self.assertEqual(csv_safe(None), "")


class TestCharts(unittest.TestCase):
    def test_ticks(self):
        self.assertEqual(charts.nice_ticks(17_500_000), [0, 5_000_000, 10_000_000, 15_000_000, 20_000_000])
        self.assertEqual(charts.nice_ticks(0), [0.0, 1.0])
        self.assertEqual(charts.nice_ticks(9), [0, 2.5, 5, 7.5, 10])

    def test_compact(self):
        self.assertEqual(charts.compact(12_500_000), "۱۲٫۵ میلیون")
        self.assertEqual(charts.compact(2_000_000_000), "۲ میلیارد")
        self.assertEqual(charts.compact(250_000), "۲۵۰ هزار")
        self.assertEqual(charts.compact(0), "۰")

    def test_width_class(self):
        self.assertEqual(charts.width_class(0, 10), "w-0")
        self.assertEqual(charts.width_class(1, 1000), "w-1")  # tiny but visible
        self.assertEqual(charts.width_class(5, 10), "w-50")
        self.assertEqual(charts.width_class(20, 10), "w-100")

    def test_trend_geometry_is_rtl(self):
        rows = [{"jy": 1405, "jm": m, "income": 100, "expense": 50 * m} for m in (1, 2, 3)]
        c = charts.trend_chart(rows, "rial", (1405, 3))
        xs = [m["x"] for m in c["months"]]
        self.assertEqual(xs, sorted(xs, reverse=True))  # oldest month is rightmost
        self.assertTrue(c["months"][-1]["selected"])
        self.assertTrue(all(m["income_path"].startswith("M") for m in c["months"]))
        self.assertFalse(c["empty"])

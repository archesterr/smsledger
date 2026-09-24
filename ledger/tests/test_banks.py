import re
import unittest
import xml.dom.minidom

from ledger import banks, parsers
from ledger.models import Account, Transaction

from .helpers import BaseTest, blu, make_user

LOGOS = banks.STATIC / "banks"


class TestRegistry(unittest.TestCase):
    def test_every_parsed_bank_has_a_logo_entry(self):
        for p in parsers.PARSERS:
            self.assertIn(p.name, banks.BANKS, p.name)

    def test_every_bank_has_a_safe_logo(self):
        for key in banks.BANKS:
            svg = (LOGOS / f"{key}.svg").read_text()
            xml.dom.minidom.parseString(svg)  # well-formed
            self.assertTrue(svg.startswith("<svg"), key)
            self.assertNotRegex(svg, r"(?i)<script|\son\w+=|href=\"(?:https?|javascript):", key)
        extra = {f.stem for f in LOGOS.glob("*.svg")} - set(banks.BANKS)
        self.assertEqual(extra, set(), "logo files nothing uses")
        self.assertIn("MIT", (LOGOS / "LICENSE").read_text())

    def test_colors_and_labels(self):
        for b in banks.BANKS.values():
            self.assertRegex(b.color, r"^#[0-9a-f]{6}$", b.key)
            self.assertRegex(b.key, r"^[a-z_]+$")
            self.assertTrue(b.label)
        self.assertEqual(len({b.label for b in banks.BANKS.values()}), len(banks.BANKS))

    def test_css_is_generated_from_the_registry(self):
        # run `python -m ledger.banks` after editing BANKS
        self.assertEqual((banks.STATIC / "banks.css").read_text(), banks.render_css())

    def test_get(self):
        self.assertEqual(banks.get("melli").label, "ملی")
        self.assertIsNone(banks.get(""))
        self.assertIsNone(banks.get(None))
        self.assertIsNone(banks.get("nope"))


class TestBankUI(BaseTest):
    def setUp(self):
        super().setUp()
        self.u = make_user()
        self.n = Account.objects.count()
        self.login(self.u)

    def test_sms_account_gets_its_banks_logo(self):
        from ledger import ingest
        ingest.ingest(self.u, [blu(5000, balance=9000)], "test")
        acc = Account.objects.get(user=self.u, bank="blu")
        self.assertEqual((acc.bank, acc.brand, acc.bank_info.key), ("blu", "", "blu"))
        tx = Transaction.objects.get(user=self.u)
        for url in ("/", "/tx/", "/accounts/", f"/tx/{tx.pk}/", "/inbox/"):
            html = self.client.get(url).content.decode()
            self.assertRegex(html, r'class="bank bk-blu[ "]', url)
            self.assertRegex(html, r'src="/static/ledger/banks/blu(\.\w+)?\.svg"', url)
        self.assertIn("/static/ledger/banks.css", html)

    def test_cash_account_has_no_logo(self):
        self.assertTrue(Account.objects.filter(user=self.u, kind=Account.CASH).exists())  # every user starts with one
        html = self.client.get("/accounts/").content.decode()
        self.assertIn("💵", html)
        self.assertNotIn('class="bank bk-', html)

    def test_manual_account_picks_a_bank_and_gets_its_name(self):
        r = self.client.post("/accounts/new/", {"brand": "mellat", "name": "", "kind": "cash"})
        self.assertEqual(r.status_code, 302)
        a = Account.objects.get(user=self.u, brand="mellat")
        # a bank logo means a bank account; empty name = the bank's name
        self.assertEqual((a.brand, a.name, a.kind, a.bank), ("mellat", "ملت", Account.BANK, ""))
        self.assertIn('class="bank bk-mellat md"', self.client.get("/accounts/").content.decode())

    def test_manual_account_needs_a_name_or_a_bank(self):
        r = self.client.post("/accounts/new/", {"brand": "", "name": "", "kind": "cash"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Account.objects.count(), self.n)
        self.assertIn("نام حساب را بنویسید", r.content.decode())

    def test_unknown_brand_rejected(self):
        r = self.client.post("/accounts/new/", {"brand": "evil\"><script>", "name": "x", "kind": "cash"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Account.objects.count(), self.n)

    def test_sms_account_can_change_or_reset_its_logo(self):
        from ledger import ingest
        ingest.ingest(self.u, [blu(5000, balance=9000)], "test")
        acc = Account.objects.get(user=self.u, bank="blu")
        page = self.client.get(f"/accounts/{acc.pk}/").content.decode()
        self.assertIn("خودکار (بلو)", page)
        self.assertNotIn('name="kind"', page)  # SMS accounts stay bank accounts
        self.client.post(f"/accounts/{acc.pk}/", {"brand": "saman", "name": acc.name})
        acc.refresh_from_db()
        self.assertEqual((acc.brand, acc.bank_info.key, acc.bank), ("saman", "saman", "blu"))
        self.client.post(f"/accounts/{acc.pk}/", {"brand": "", "name": acc.name})
        acc.refresh_from_db()
        self.assertEqual(acc.bank_info.key, "blu")

    def test_account_form_shows_every_bank(self):
        page = self.client.get("/accounts/new/").content.decode()
        for b in banks.BANKS.values():
            self.assertIn(f'value="{b.key}"', page)
        self.assertEqual(len(re.findall(r'type="radio" name="brand"', page)), len(banks.BANKS) + 1)

    def test_service_worker_precaches_bank_colors(self):
        self.assertIn("/static/ledger/banks.css", self.client.get("/sw.js").content.decode())

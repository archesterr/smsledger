from ledger import ingest, reports
from ledger.models import Budget, Category, Transaction

from .helpers import BaseTest, blu, make_user


class TestReports(BaseTest):
    def setUp(self):
        super().setUp()
        self.u = make_user()
        ingest.ingest(self.u, [
            blu(10_000_000, "IN", 10_000_000, time="09:00"),       # salary-ish
            blu(2_000_000, "OUT", 8_000_000, time="10:00"),
            blu(3_000_000, "OUT", 5_000_000, time="11:00"),
            blu(1_000_000, "OUT", 4_000_000, time="12:00"),        # will be an own-account transfer
            blu(500_000, "OUT", 0, time="09:00", jdate="1405.05.10"),  # previous month
        ], "t")
        self.food = Category.objects.get(user=self.u, name="رستوران و کافه")
        self.transfer = Category.objects.get(user=self.u, kind=Category.TRANSFER)
        Transaction.objects.filter(user=self.u, amount=2_000_000).update(category=self.food)
        Transaction.objects.filter(user=self.u, amount=1_000_000).update(category=self.transfer)

    def test_month_totals_exclude_transfers(self):
        t = reports.month_totals(self.u, 1405, 6)
        self.assertEqual((t["income"], t["expense"], t["net"]), (10_000_000, 5_000_000, 5_000_000))
        self.assertEqual(t["count"], 3)

    def test_by_category(self):
        rows = reports.by_category(self.u, 1405, 6)
        self.assertEqual([(r["category"], r["total"]) for r in rows], [(None, 3_000_000), (self.food, 2_000_000)])
        self.assertEqual(rows[0]["share"], 60.0)

    def test_trend_zero_fills(self):
        rows = reports.trend(self.u, 3, (1405, 6))
        self.assertEqual([(r["jm"], r["income"], r["expense"]) for r in rows],
                         [(4, 0, 0), (5, 0, 500_000), (6, 10_000_000, 5_000_000)])

    def test_budgets(self):
        Budget.objects.create(user=self.u, category=self.food, amount=1_500_000)
        b = reports.budgets(self.u, 1405, 6)[0]
        self.assertEqual((b["spent"], b["left"], b["pct"], b["over"]), (2_000_000, -500_000, 100, True))

    def test_health_and_balances(self):
        h = reports.health(self.u)
        self.assertEqual((h["gaps"], h["unparsed"], h["uncategorized"], h["silent"]), (0, 0, 3, False))
        self.assertEqual(reports.balances(self.u)[0]["balance"], 4_000_000)

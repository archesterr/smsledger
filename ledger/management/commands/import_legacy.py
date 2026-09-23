import sqlite3

from django.core.management.base import BaseCommand, CommandError

from ledger import ingest
from ledger.models import User


class Command(BaseCommand):
    help = "Import SMS from the old single-user smsledger SQLite file (table 'sms') into a user's ledger."

    def add_arguments(self, parser):
        parser.add_argument("path", help="path to the old ledger.db")
        parser.add_argument("--user", required=True, help="username that receives the SMS")

    def handle(self, *args, path, user, **opts):
        u = User.objects.filter(username=user).first()
        if not u:
            raise CommandError(f"no user {user!r}")
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = [r[0] for r in con.execute("SELECT raw FROM sms ORDER BY id")]
        except sqlite3.Error as e:
            raise CommandError(f"can't read {path}: {e}") from e
        results = ingest.ingest(u, rows, "legacy")
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        self.stdout.write(f"imported {len(rows)} SMS: {counts}")

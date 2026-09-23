from django.core.management.base import BaseCommand

from ledger import ingest


class Command(BaseCommand):
    help = "Re-run parsers on unparsed SMS (runs on every container start, so new bank templates apply)."

    def handle(self, *args, **opts):
        r = ingest.reparse()
        self.stdout.write(f"reparse: checked={r['checked']} fixed={r['fixed']} purged={r['purged']}")

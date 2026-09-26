from django.core.management.base import BaseCommand

from ledger import ingest


class Command(BaseCommand):
    help = ("Re-run parsers on unparsed SMS of accounts whose key the server holds (runs on every container "
            "start). Encrypted accounts re-parse at their owner's next visit.")

    def handle(self, *args, **opts):
        r = ingest.reparse()
        self.stdout.write(f"reparse: checked={r['checked']} fixed={r['fixed']} purged={r['purged']}")

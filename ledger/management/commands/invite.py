from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from ledger import security
from ledger.models import Invite


class Command(BaseCommand):
    help = "Create a single-use signup link for a friend."

    def add_arguments(self, parser):
        parser.add_argument("--note", default="", help="who it's for (shown on the staff page)")
        parser.add_argument("--days", type=int, default=settings.INVITE_DAYS)

    def handle(self, *args, note, days, **opts):
        code = security.new_invite_code()
        Invite.objects.create(code_hash=security.sha256(code), note=note[:100],
                              expires_at=timezone.now() + timedelta(days=days))
        self.stdout.write(f"https://{settings.DOMAIN}/join/{code}/  (valid {days} days, single use)")

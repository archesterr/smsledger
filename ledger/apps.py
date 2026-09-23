from django.apps import AppConfig
from django.db.models.signals import post_save


class LedgerConfig(AppConfig):
    name = "ledger"
    verbose_name = "Ledger"

    def ready(self):
        from .defaults import on_user_saved
        from .models import User

        post_save.connect(on_user_saved, sender=User, dispatch_uid="ledger_user_defaults")

"""PostgreSQL keeps dropped columns' bytes in the table files until rows are rewritten:
VACUUM FULL rewrites the tables so the old plaintext is really gone from disk. (It stays in
backups taken before the upgrade until they are pruned, see README.)"""
from django.db import migrations

TABLES = ["ledger_account", "ledger_category", "ledger_message", "ledger_transaction", "ledger_rule",
          "ledger_budget"]


def vacuum(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as c:
        for t in TABLES:
            c.execute(f"VACUUM FULL {t}")


class Migration(migrations.Migration):
    atomic = False  # VACUUM can't run inside a transaction

    dependencies = [
        ("ledger", "0005_vault_drop_plaintext"),
    ]

    operations = [
        migrations.RunPython(vacuum, migrations.RunPython.noop),
    ]

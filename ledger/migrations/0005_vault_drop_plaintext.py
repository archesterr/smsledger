"""Encryption, step 3 of 3: drop the plaintext columns (their values are in `sealed` now)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0004_vault_encrypt"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="account", name="uniq_bank_account"),
        migrations.RemoveField(model_name="account", name="name"),
        migrations.RemoveField(model_name="account", name="hint"),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.UniqueConstraint(condition=~models.Q(bank=""), fields=("user", "bank", "hint_idx"),
                                               name="uniq_bank_account"),
        ),
        migrations.RemoveConstraint(model_name="category", name="uniq_category_name"),
        migrations.RemoveField(model_name="category", name="name"),
        migrations.RemoveField(model_name="message", name="raw"),
        migrations.RemoveConstraint(model_name="transaction", name="amount_positive"),
        *(migrations.RemoveField(model_name="transaction", name=n)
          for n in ("amount", "balance", "title", "counterparty", "note", "gap_amount")),
        *(migrations.RemoveField(model_name="rule", name=n) for n in ("text", "amount_min", "amount_max")),
        migrations.RemoveField(model_name="budget", name="amount"),
    ]

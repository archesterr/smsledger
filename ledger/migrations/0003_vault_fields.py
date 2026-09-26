"""Encryption, step 1 of 3: new columns (sealed data, user keys) and the session/event tables.
Old plaintext columns stay until 0005."""
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import ledger.models


def binary():
    return models.BinaryField(default=b"", editable=False)


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0002_account_brand"),
    ]

    operations = [
        # its create_user goes through set_password, which makes the keys
        migrations.AlterModelManagers(name="user", managers=[("objects", ledger.models.UserManager())]),
        *(migrations.AddField(model_name="user", name=n, field=binary())
          for n in ("vault_pub", "vault_priv", "key_pw", "key_rec", "key_srv", "dedupe_key")),
        migrations.AddField(model_name="user", name="recovery_saved_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="user", name="password_changed_at",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="user", name="parsers_version",
                            field=models.CharField(blank=True, max_length=16)),
        migrations.AddField(model_name="device", name="last_ip", field=models.CharField(blank=True, max_length=45)),
        *(migrations.AddField(model_name=m, name="sealed", field=binary())
          for m in ("account", "category", "message", "transaction", "rule", "budget")),
        migrations.AddField(model_name="account", name="hint_idx", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="message", name="inbox", field=binary()),
        migrations.AlterField(
            model_name="message", name="status",
            field=models.CharField(choices=[("parsed", "parsed"), ("unparsed", "unparsed"), ("ignored", "ignored"),
                                            ("pending", "pending")], max_length=8),
        ),
        migrations.AddField(model_name="transaction", name="has_gap", field=models.BooleanField(default=False)),
        # nullable while they're phased out: lets 0005 be undone before 0004 refills them
        migrations.AlterField(model_name="transaction", name="amount", field=models.BigIntegerField(null=True)),
        migrations.AlterField(model_name="budget", name="amount", field=models.BigIntegerField(null=True)),
        migrations.AlterField(model_name="account", name="name", field=models.CharField(max_length=60, null=True)),
        migrations.AlterField(model_name="category", name="name", field=models.CharField(max_length=60, null=True)),
        migrations.AlterField(model_name="message", name="raw", field=models.TextField(null=True)),
        migrations.CreateModel(
            name="UserSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("ip", models.CharField(blank=True, max_length=45)),
                ("agent", models.CharField(blank=True, max_length=80)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sessions",
                                           to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-last_seen"],
                "indexes": [models.Index(fields=["user", "ended_at"], name="ledger_user_user_id_236146_idx")],
            },
        ),
        migrations.CreateModel(
            name="SecurityEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("ip", models.CharField(blank=True, max_length=45)),
                ("agent", models.CharField(blank=True, max_length=80)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="security_events", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [models.Index(fields=["user", "-created_at"], name="ledger_secu_user_id_5cee65_idx")],
            },
        ),
    ]

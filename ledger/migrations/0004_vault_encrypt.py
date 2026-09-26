"""Encryption, step 2 of 3: encrypt every existing user's data with a new per-user key.

Nobody's password is known here, so each key is kept readable by the server (User.key_srv)
until its owner's next login, which wraps it with their password and deletes that copy
(vault.py). Until then those accounts are protected against nothing new: same as before.
Reversible only while every account's key is still server-readable (nobody logged in since):
after that, data is locked under passwords the server doesn't have. Take a backup first
(README, "Upgrading to encrypted storage")."""
from django.db import migrations

from ledger import vault

BATCH = 500


def seal_all(model, user, dek, fields, extra=None):
    rows, label = [], "ledger." + model._meta.model_name
    for obj in model.objects.filter(user=user).iterator(chunk_size=BATCH):
        obj.sealed = vault.seal_row(dek, label, user.pk, {f: getattr(obj, f) for f in fields})
        if extra:
            extra(obj)
        rows.append(obj)
    return rows


def encrypt_existing(apps, schema_editor):
    User = apps.get_model("ledger", "User")
    m = {n: apps.get_model("ledger", n) for n in ("Account", "Category", "Message", "Transaction", "Rule", "Budget")}
    for u in User.objects.all().iterator():
        dek = vault.new_keys(u)
        u.key_srv = dek
        u.save(update_fields=["vault_pub", "vault_priv", "key_pw", "key_rec", "key_srv", "dedupe_key"])

        def account_idx(a, dek=dek):
            a.hint_idx = vault.blind(dek, a.hint) if a.bank else ""

        def message_hash(msg, u=u):
            msg.hash = vault.fingerprint(u, msg.raw)

        def tx_gap(t):
            t.has_gap = t.gap_amount is not None

        for model, fields, extra, update in [
            (m["Account"], ["name", "hint"], account_idx, ["sealed", "hint_idx"]),
            (m["Category"], ["name"], None, ["sealed"]),
            (m["Message"], ["raw"], message_hash, ["sealed", "hash"]),
            (m["Transaction"], ["amount", "balance", "title", "counterparty", "note", "gap_amount"], tx_gap,
             ["sealed", "has_gap"]),
            (m["Rule"], ["text", "amount_min", "amount_max"], None, ["sealed"]),
            (m["Budget"], ["amount"], None, ["sealed"]),
        ]:
            rows = seal_all(model, u, dek, fields, extra)
            model.objects.bulk_update(rows, update, batch_size=BATCH)


FIELDS = {"Account": ["name", "hint"], "Category": ["name"], "Message": ["raw"],
          "Transaction": ["amount", "balance", "title", "counterparty", "note", "gap_amount"],
          "Rule": ["text", "amount_min", "amount_max"], "Budget": ["amount"]}


def decrypt_back(apps, schema_editor):
    User = apps.get_model("ledger", "User")
    locked = [u.username for u in User.objects.all() if not bytes(u.key_srv)]
    if locked:
        raise RuntimeError(f"can't decrypt data locked under users' passwords: {locked}. Restore a backup instead.")
    for u in User.objects.all():
        dek = bytes(u.key_srv)
        for name, fields in FIELDS.items():
            model, rows = apps.get_model("ledger", name), []
            for obj in model.objects.filter(user=u):
                data = vault.open_row(dek, "ledger." + model._meta.model_name, u.pk, obj.sealed) \
                    if bytes(obj.sealed) else {}
                for f in fields:
                    setattr(obj, f, data.get(f, model._meta.get_field(f).get_default()))
                rows.append(obj)
            model.objects.bulk_update(rows, fields, batch_size=BATCH)


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0003_vault_fields"),
    ]

    operations = [
        migrations.RunPython(encrypt_existing, reverse_code=decrypt_back, elidable=False),
    ]

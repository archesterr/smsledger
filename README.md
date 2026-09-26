# smsledger

Bank SMS → a private, shared-server ledger for you and your friends.
Each iPhone forwards its bank SMS through a Shortcut; the server parses them into transactions,
and each person gets a Persian (RTL) web app with categories, budgets, reports and CSV export.
Invite-only, per-user isolation, 2FA, no OTP ever stored.

Banks: **Blu, Saman, Middle East Bank (خاورمیانه), Pasargad, Melli**. SMS from other banks are
kept and parsed automatically once their template is added.
Every account shows its bank's logo and colour: SMS accounts automatically, and for a manual
account you pick one of 32 Iranian banks (Accounts → the account → بانک).

```
iPhone (each user)                                  Server (docker compose)
 Message automation (bank sender, "موجودی"/"مانده")
  └─ Shortcut "SMS to Ledger"
       1. drop OTP / login-code SMS on the phone
       2. append SMS to iCloud queue file ───── offline? sent by the nightly run of the same shortcut
       3. POST /ingest  (device token) ───────► Caddy (HTTPS) ─► Django app ─► PostgreSQL
                                                  ├─ drop OTP again, never store it
                                                  ├─ per-user sha256 dedupe (resending is safe)
                                                  ├─ parse (bank template) or keep as "unparsed"
                                                  ├─ categorize (your rules → built-in hints)
                                                  └─ balance-gap check (finds missed SMS)
 PWA (Safari → Add to Home Screen) ◄──────────── inbox · transactions · reports · budgets
                                                 restic ─► encrypted offsite backups
```

## What "automatic" means on each iOS version

iOS gives apps no access to SMS. The only hook is the Shortcuts **Message** automation:

| iOS | Each new SMS | Nightly queue sync |
|---|---|---|
| 17+ | fully automatic, silent ("Run Immediately") | automatic |
| 14 – 16 | a notification appears; **tap it** (Apple's rule, no workaround except a Mac relay) | automatic on 15.4+ |

A missed tap is not silent: every supported bank's SMS carries the balance, so the next SMS shows a
**balance gap** with the missing amount, and the user pastes the missed SMS into the app.

---

## Security model

Built so friends can use one server without seeing each other, and so that the operator, the
database and the backups can't read anyone's money.

- **Encrypted per user** ([`ledger/vault.py`](ledger/vault.py)). Amounts, balances, SMS text,
  titles, counterparties, notes, and account, category and rule names are stored encrypted
  (AES-256-GCM, each row bound to its table and owner) with a random key per user. That key is
  stored only wrapped: by the user's password (Argon2id) and by a one-time **recovery key** the
  user saves at signup. In a logged-in session it sits in the database wrapped with a secret
  that only the browser's cookie holds, so a database dump or backup alone opens nothing.
  SMS that arrive while the user is logged out are sealed to their public key (X25519) and
  recorded at their next login. Still in the clear, because the app needs them in SQL: who owns
  a row, when a transaction happened, in/out, and which of the user's categories/accounts it's in.
- **Forgotten password.** Only the user's recovery key brings the data back. A password set by
  the operator (admin, `changepassword`) locks the data until the user enters their recovery
  key; without it they can only start over. The operator can't unlock anyone's data.
- **Security page** (More → امنیت) for each user: a checklist (encryption, recovery key, 2FA,
  failed logins in 30 days, unused iPhone keys), every signed-in browser with device, network
  prefix and last activity (sign out one or all others), the iPhone keys with where they last
  sent from, and an activity log (logins, failures, lockouts, password/2FA/key changes,
  exports, staff actions). The home page warns about failed logins since the last visit.
  Network addresses are kept only to the first three parts (`5.120.33.x`).
- **Invite-only.** Single-use signup links (7 days), created by staff.
- **Isolation.** Every row has a `user` and every query filters on it; tests try to read/modify
  other users' objects through every URL.
- **Device tokens are write-only.** A token in a Shortcut can add SMS through `/ingest` and
  nothing else. Tokens are random 256-bit, stored as sha256, shown once, revocable per device.
- **OTP / login codes never stored.** Filtered on the phone *and* on the server (not stored,
  not logged, not even hashed). Dynamic-password SMS usually contain "ریال", so the
  automation's text filter alone would not stop them.
- **Logins.** Argon2 password hashing, case-insensitive usernames, 15-minute lockout after
  10 failures per username or 30 per IP, optional TOTP 2FA with one-time recovery codes. **2FA is mandatory
  for staff**, and Django admin's login is routed through the same 2FA flow.
- **Risky actions ask for the password again**: exporting everything (the CSV isn't encrypted),
  replacing the recovery key, turning 2FA off, deleting the account.
- **Browser.** Strict CSP (no inline scripts or styles), HSTS, COOP/CORP, `__Host-` cookies,
  `SameSite=Lax`, CSRF on every form, `Cache-Control: no-store` on every page, frame-busting,
  CSV-injection escaping in exports, no third-party requests (the font is self-hosted).
- **Operator minimization.** Staff pages show only counts and health (e.g. "3 unparsed SMS").
  Transactions, SMS and budgets are not in Django admin. The only SMS text staff see is what
  a user explicitly shares from the app after editing out personal details.
- **Logs** hold counts and request paths without query strings: no SMS text, amounts or tokens.
- **Infra.** App container is non-root, read-only, all capabilities dropped; Postgres sits on an
  internal network with no internet; dependencies are hash-pinned and audited in CI.

**What this is not:** end-to-end encrypted. iPhone Shortcuts can't encrypt, and the server has
to read each SMS once to parse it, and a user's data while they use the app. Someone who
controls the server can change its code to capture that. The encryption protects the database,
backups, logs and admin screens, not against a malicious operator. So only invite people who
trust you, and protect the server (SSH keys only, updates, offsite backups whose password
lives somewhere else).

### Upgrading to encrypted storage

The upgrade (migrations 0003–0006) runs by itself on `docker compose up -d --build`. **Take a
backup first**: `docker compose exec backup backup run` (or a `pg_dump`).

- Existing data is encrypted right away, with a new key per user. Nobody's password is known
  during the upgrade, so each key stays readable by the server until **that user's next login**.
  Their open sessions are signed out once. The next login moves the key under their password
  and asks them to save a recovery key. The staff page shows who is still "منتظر ورود".
- Until then those accounts work as before (the phone's SMS are recorded at once).
- PostgreSQL files are rewritten (`VACUUM FULL`) so the old plaintext columns are gone from the
  tables. Copies still exist in backups taken before the upgrade and briefly in the database's
  write-ahead log. For a fully clean disk: after everyone has logged in, prune old snapshots
  (`restic forget`) and move the database to a fresh volume (`pg_dump` → new volume → restore).

---

## Server setup

Any small VPS with Docker (1 vCPU / 1 GB is plenty for a group of friends) and a DNS name.
An Iran VPS is recommended: phones reach it without a VPN and it keeps working during
international internet cuts.

```bash
git clone <this repo> smsledger && cd smsledger/deploy
cp .env.example .env
python3 - <<'EOF'
import re, secrets
s = open(".env").read()
s = re.sub(r"^SECRET_KEY=$", "SECRET_KEY=" + secrets.token_urlsafe(48), s, flags=re.M)
s = re.sub(r"^POSTGRES_PASSWORD=$", "POSTGRES_PASSWORD=" + secrets.token_urlsafe(24), s, flags=re.M)
open(".env", "w").write(s)
EOF
vi .env                      # DOMAIN=tx.yourdomain.ir  (A record -> this server)
docker compose up -d --build
docker compose ps            # app and db should become "healthy"
```

Create your admin account, then log in at `https://tx.yourdomain.ir`, turn on 2FA
(required for the admin pages) and open **بیشتر → مدیریت** to create invite links:

```bash
docker compose exec app python manage.py createsuperuser
docker compose exec app python manage.py invite --note "Ali"   # or use the staff page
```

### Iran servers: Docker Hub and PyPI

Docker Hub refuses Iranian IPs. Add a registry mirror to the Docker daemon; it covers
every image, including the ones the Dockerfiles build from:

```json
// /etc/docker/daemon.json  then: systemctl restart docker
{ "registry-mirrors": ["https://docker.arvancloud.ir"] }
```

Image names are written out in full (not variables) so Dependabot can keep them updated.
If PyPI is unreachable during the build, set
`PIP_INDEX_URL` to a PyPI mirror; hashes are still verified, so a mirror can't swap packages.

---

## iPhone setup (each user)

Everything is explained in Persian inside the app, with the user's own server URL filled in:
**بیشتر → راه‌اندازی آیفون**. Once the admin has published the shortcut (below), each person does:

1. **Install**: one tap on the iCloud link → *Add Shortcut*.
2. **Connect**: one tap on *اتصال این آیفون*. The server makes a device key and opens
   `shortcuts://run-shortcut?name=SMS%20to%20Ledger&input=text&text=Bearer%20<key>`; the shortcut
   saves the key to `Shortcuts/smsledger/key.txt`, says hello to `/ingest?source=connect` and shows
   the server's reply. The setup page turns green by itself when the hello arrives. Keys that were
   made but never used are revoked on the next tap.
3. **Bank contacts** and the **Message automation** (Apple doesn't let automations be shared):
   sender = bank contact(s), contains the bank's balance word → Run Shortcut *SMS to Ledger* with
   *Shortcut Input*. One automation per word: `موجودی` for Blu, `مانده` for Saman, Middle East
   Bank, Pasargad and Melli. iOS 17+: *Run Immediately*, *Notify When Run* off. **iOS 16** has no
   *New Blank Automation* / *Run Immediately*: Automation → **Create Personal Automation** → Message
   → *Message Contains* / *Sender* → Next → **Add Action** → Run Shortcut (expand it to set *Input*
   = Shortcut Input) → Next → turn off *Ask Before Running* → Done. Each SMS then shows a
   notification to tap. The setup page opens the guide for the phone's iOS version.
4. **Nightly automation** (recommended): Time of Day 03:00 → Run Shortcut *SMS to Ledger*, no input.
5. **Old SMS**: copy them from Messages and paste into **وارد کردن پیامک**. Duplicates are ignored.

### The shortcut

[`ledger/shortcut.py`](ledger/shortcut.py) generates it (download: `/setup/shortcut/`). It holds
**no key**, so one copy serves everyone. What it does depends on its input:

| Input | From | Does |
|---|---|---|
| `Bearer sml_…` | the Connect button | save the key, `POST /ingest?source=connect`, show the reply |
| an SMS | Message automation | drop OTP / login codes on the phone (regex), append to `smsledger/queue.txt`, `POST /ingest?source=iphone` |
| nothing | nightly automation, or a manual run | `POST` the queue to `/ingest?split=1&source=queue`; empty it only if the reply has a `status` key (errors never do) |

The queue is written before the request, so an SMS that arrives with no internet is sent by the
next nightly run. Resending is harmless (per-user dedupe). The tests run the generated file
through a small interpreter against `/ingest` (connect, SMS, OTP, offline, failed sync).

### Publishing it once (admin)

Since iOS 15 an iPhone only imports shortcut **files** signed with an Apple ID, and only a Mac or
an iPhone can sign one, so the server can't hand out a ready file. Do this once:

1. Get *SMS to Ledger* onto your iPhone, either
   - **with a Mac**: download it from the staff page (it has your server's address in it), then
     `shortcuts sign --mode anyone --input "SMS to Ledger.shortcut" --output "SMS to Ledger signed.shortcut"`,
     and open the signed file (or AirDrop it to the iPhone) → *Add Shortcut*; or
   - **without a Mac**: build it by hand once from *ساختن دستی میان‌بر* on the setup page.
2. Shortcuts → long-press it → **Share** → **Copy iCloud Link**.
3. Set `SHORTCUT_URL=<that link>` in `.env` and `docker compose up -d`. Every setup page now has
   the one-tap install button.

A hand-built shortcut from before this version (key in its own header, plus *Sync SMS Queue*)
keeps working; to move to the new one, delete both and follow steps 1–2 above.

---

## Upgrading from v1 (single-user SQLite version)

v1 ran as compose project `deploy` on the same ports 80/443: stop it first (its data volume is
kept), set up v2 as above, then import the old SMS into your account (dedupe makes it safe to
run twice). Finally give your Shortcut a new device key and add the OTP step above; v1's
`INGEST_TOKEN` / `DASH_*` settings are no longer used.

```bash
docker compose -p deploy down                         # stops v1; volumes are not deleted
docker volume ls | grep ledger                        # find the old volume, e.g. deploy_ledger
docker run --rm -v deploy_ledger:/data alpine cat /data/ledger.db > ledger.db
# the app container is read-only, so stream the file into its /tmp (docker cp can't write there)
docker compose exec -T app sh -c 'cat > /tmp/ledger.db' < ledger.db
docker compose exec app python manage.py import_legacy /tmp/ledger.db --user <your-username>
```

---

## Operations

**Updates**: `git pull && docker compose up -d --build`. On start the app runs migrations. Each
user's unparsed SMS are re-read with the new parsers at their next visit (the server has no key
before that), so a new bank template fixes old SMS too.

**Backups** (daily `pg_dump` → [restic](https://restic.net): encrypted, deduplicated, keeps
7 daily / 4 weekly / 12 monthly, spot-checks 10% of the data each run):

```bash
cp backup.env.example backup.env && vi backup.env     # S3-compatible bucket on ANOTHER provider
sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=backup/' .env
docker compose up -d                                  # first backup runs immediately
docker compose exec backup backup list                # snapshots
docker compose ps backup                              # "unhealthy" = no good backup in 26 h
```

Keep `RESTIC_PASSWORD` somewhere off the server: without it the backups can't be read.
**Restore** (test it once a month into a scratch database):

```bash
docker compose exec -T backup backup dump > smsledger.dump
docker compose exec -T db createdb -U smsledger restore_test
docker compose exec -T db pg_restore --no-owner --no-privileges -U smsledger -d restore_test < smsledger.dump
docker compose exec -T db psql -U smsledger -d restore_test -c "select count(*) from ledger_transaction"
# real restore: stop app, restore into "smsledger" with --clean --if-exists, start app
```

**Monitoring**: set `METRICS_TOKEN` and scrape `http://app:8000/metrics` from the
`smsledger_web` docker network (Caddy returns 404 for `/metrics` publicly). Metrics are
counts only, with no per-user labels:

```yaml
# vmagent / Prometheus scrape job
- job_name: smsledger
  authorization: {credentials: <METRICS_TOKEN>}
  static_configs: [{targets: ["app:8000"]}]
```

```yaml
# alert rules
- alert: SmsLedgerDown
  expr: up{job="smsledger"} == 0
  for: 5m
- alert: SmsLedgerUnparsed            # a bank changed its template, or a new bank appeared
  expr: smsledger_messages{status="unparsed"} > 0
  for: 1h
- alert: SmsLedgerUserSilent          # someone's iPhone automation probably broke
  expr: smsledger_users_silent > 0
  for: 6h
- alert: SmsLedgerNoIngest            # nobody's SMS arrive: server-side or network problem
  expr: time() - smsledger_last_ingest_timestamp_seconds > 2 * 86400
```

Users also see their own problems in the app: a "no SMS for 3 days" warning, balance gaps
and unparsed SMS, all on the home page.

---

## Adding a bank

1. When a friend's SMS aren't recognised, they tap **ارسال برای مدیر** on the unparsed SMS
   (after editing out personal details). You'll see it on the staff page.
2. Add a `BankParser` subclass in `ledger/parsers.py` (see `BluParser`), append it to
   `PARSERS`, and put 2–3 real, anonymized samples (withdrawal, deposit, transfer) in
   `ledger/tests/test_parsers.py`. If the SMS carries a masked account/card number, return it
   as `Tx.account` (only its last 4 digits, via `last4`) so each account gets its own balance
   chain. If the bank sends no year, use `infer_datetime(month, day, h, m, ref)`.
   If the bank's name line may be missing from the text (it is sometimes only the sender),
   implement `sniff()` to recognise the bank by its layout.
3. The parser's `name` must be a key in `ledger/banks.py` (logo + colour). For a bank not
   listed there: add a `Bank(...)`, put its square SVG logo in `ledger/static/ledger/banks/<key>.svg`,
   and run `python -m ledger.banks` to regenerate `banks.css` (a test checks it's current).
4. Deploy. Each user's unparsed SMS are re-parsed automatically at their next visit.

---

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.txt && pip install ruff
export DEBUG=1                      # SQLite in ./dev.sqlite3, no HTTPS redirect
python manage.py migrate && python manage.py createcachetable && python manage.py createsuperuser
python manage.py runserver
python manage.py test ledger        # set POSTGRES_* to run against PostgreSQL, as CI does
ruff check .
```

Dependencies: edit `requirements.in`, then
`pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.txt requirements.in`.

Layout: `ledger/parsers.py` + `jalali.py` (pure Python, no Django), `ingest.py` (SMS → transactions,
dedupe, gaps), `rules.py`, `reports.py`, `views/`, `templates/`, `deploy/` (compose, Caddy, backups).

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
       2. append SMS to iCloud queue file ───── offline? sent by the nightly "Sync" shortcut
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

Built so friends can use one server without seeing each other, and without the operator
browsing their money by accident.

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
- **Browser.** Strict CSP (no inline scripts or styles), HSTS, `__Host-` cookies,
  `SameSite=Lax`, CSRF on every form, `Cache-Control: no-store` on every page, frame-busting,
  CSV-injection escaping in exports, no third-party requests (the font is self-hosted).
- **Operator minimization.** Staff pages show only counts and health (e.g. "3 unparsed SMS").
  Transactions, SMS and budgets are not in Django admin. The only SMS text staff see is what
  a user explicitly shares from the app after editing out personal details.
- **Logs** hold counts and request paths without query strings: no SMS text, amounts or tokens.
- **Infra.** App container is non-root, read-only, all capabilities dropped; Postgres sits on an
  internal network with no internet; dependencies are hash-pinned and audited in CI.

**What this is not:** end-to-end encrypted. The server has to read an SMS to parse it, so
whoever controls the server (you) can technically read the database. Only invite people
who trust you with that, and protect the server accordingly (SSH keys only, updates,
encrypted offsite backups whose password lives somewhere else).

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
**بیشتر → راه‌اندازی آیفون**. In short:

1. **Device key**: create one on the setup page (shown once).
2. **Contact**: save the bank's SMS sender as a contact (e.g. `Blu`).
3. **Shortcut "SMS to Ledger"**:
   - **Match Text** on *Shortcut Input* with pattern
     `رمز(?!\s*ارز)|یک.?بار|کد.?(تایید|تأیید|ورود|فعال|پویا)|OTP` → **If** *Matches* has any
     value → **Stop This Shortcut**
   - **Text**: *Shortcut Input* + a line `---` → **Append to Text File** `smsledger/queue.txt`
   - **Get Contents of URL**: `POST https://tx.yourdomain.ir/ingest?source=iphone`,
     header `Authorization: Bearer <device key>`, JSON body `{"sms": Shortcut Input}`
4. **Automation**: Message → sender = bank contact(s), contains the bank's balance word →
   Run Shortcut (iOS 17+: *Run Immediately*, *Notify When Run* off). One automation per word:
   `موجودی` for Blu, `مانده` for Saman, Middle East Bank, Pasargad and Melli.
   **iOS 16** has no *New Blank Automation* / *Run Immediately*: Automation → **Create Personal
   Automation** → Message → *Message Contains* / *Sender* → Next → **Add Action** → Run Shortcut
   (expand it to set *Input* = Shortcut Input) → Next → turn off *Ask Before Running* → Done.
   Each SMS then shows a notification to tap. The setup page opens the guide for the
   phone's iOS version (from Safari's User-Agent).
5. **Shortcut "Sync SMS Queue"** + a daily 03:00 automation: posts `queue.txt` to
   `/ingest?split=1&source=queue` and deletes the file only if the response has a `status` key
   (errors never contain one, so a failed sync keeps the queue).
6. **Old SMS**: copy them from Messages and paste into **وارد کردن پیامک**. Duplicates are ignored.

To save your friends the typing, build the shortcut once, share it as an iCloud link with
*Import Questions* for the URL and the key, and set `SHORTCUT_URL` in `.env`: the setup page
then shows an "add shortcut" button. (The Message automation can't be shared; each person
creates it.)

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

**Updates**: `git pull && docker compose up -d --build`. On start the app runs migrations and
re-parses every unparsed SMS with the new parsers, so a new bank template fixes old SMS too.

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
4. Deploy. Existing unparsed SMS of every user are re-parsed automatically.

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

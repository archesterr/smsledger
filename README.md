# smsledger

Bank SMS → your own server → SQLite ledger with a dashboard and CSV export.
Pure Python stdlib, with no dependencies. Works with **every iOS version that has Shortcuts automations (iOS 14.x+)**.

```
iPhone (Blu SMS arrives)
  └─ Shortcut automation
       ├─ 1. append SMS to iCloud queue file   ← nothing is lost if the network is down
       └─ 2. POST /ingest ───────────► smsledger (VPS, behind Caddy TLS)
                                          ├─ normalize (Persian digits, RLM marks)
Nightly "Sync" automation                 ├─ sha256 dedupe (resending is always safe)
  └─ POST whole queue file ──────────►    ├─ parse (bank template) or keep as "unparsed"
                                          └─ SQLite → dashboard / CSV / /metrics
```

## What "fully automatic" means on each iOS version

| iOS | On each new SMS | Nightly sync | Result |
|---|---|---|---|
| 17+ | Runs silently (Run Immediately) | Silent | Fully automatic |
| 15.4 – 16.x | iOS shows a notification; **tap it** | Silent (Time of Day automations can run without asking) | One tap per SMS; missed taps are caught by balance-gap detection + Import SMS |
| 14 – 15.3 | Notification to tap | Notification to tap | Semi-automatic |

> **iOS 15/16 note:** Apple blocks Message automations from running without a tap. That's an OS rule and no Shortcut can bypass it.
> The only fully tap-free path on iOS ≤16 is a Mac relay (Messages sync to a Mac, then a script reads `chat.db`). If you ever get a Mac, the same `/ingest` endpoint accepts it.
> If you miss a tap, use the **Import SMS** shortcut (step 6): copy the message, run the shortcut, and dedupe makes it safe.

---

## Step 1: Server (Iran VPS recommended, so the phone reaches it without a VPN)

```bash
git clone <this repo> smsledger && cd smsledger/deploy
cp .env.example .env
sed -i "s/^INGEST_TOKEN=.*/INGEST_TOKEN=$(openssl rand -hex 24)/" .env
sed -i "s/^DASH_PASS=.*/DASH_PASS=$(openssl rand -base64 18)/" .env
vi .env                                   # set DOMAIN=tx.yourdomain.ir  (A record → VPS IP)
docker compose up -d --build
docker compose logs -f smsledger
```

Smoke test:

```bash
TOKEN=$(grep ^INGEST_TOKEN .env | cut -d= -f2)
curl -s https://tx.yourdomain.ir/ingest -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sms":"بلو\nبرداشت پول\nآرمین عزیز، 1,000,000 ریال از حساب شما پرید.\nموجودی: 2,887,139 ریال\n۱۴:۰۳\n۱۴۰۵.۰۶.۳۱","source":"curl"}'
# → {"status":"created","tx":{"bank":"blu","direction":"OUT","amount":1000000,...}}
# same command again → {"status":"duplicate"}
```

Dashboard: `https://tx.yourdomain.ir/` (basic auth `DASH_USER` / `DASH_PASS`).

## Step 2: Save the Blu sender as a contact

The Message trigger filters by contact. Open a Blu SMS, tap the sender, choose **Create New Contact**, and name it `Blu`.

## Step 3: Shortcut "SMS → Ledger" (the worker)

Shortcuts → **Shortcuts** tab → **+** → name it `SMS → Ledger`:

1. **Text**: `[Shortcut Input]`, then a new line, then `---`
   *(tap the variable and pick Shortcut Input; if it offers properties, pick **Content**)*
2. **Append to Text File**
   - File path: `smsledger/queue.txt` (in the Shortcuts iCloud folder)
   - Text: the Text from step 1
   - Turn **Make New Line** on
3. **Get Contents of URL**
   - URL: `https://tx.yourdomain.ir/ingest?source=iphone`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <INGEST_TOKEN>`
   - Request Body: **JSON**, with one key `sms` (Text) = `Shortcut Input`

Step 2 runs before step 3 on purpose: if there's no internet, the SMS is already in the queue.

## Step 4: Automation (fires on every Blu SMS)

Shortcuts → **Automation** → **+** → (Create Personal Automation) → **Message**:

- Sender: `Blu`
- Message Contains: `ریال` (skips OTP and ad SMS)
- Next → action **Run Shortcut** → `SMS → Ledger`, with Input = *Shortcut Input* (the message)
- **iOS 17+:** choose **Run Immediately** and turn *Notify When Run* off
- **iOS ≤16:** you'll get a notification on each SMS; tap it and it runs

## Step 5: Shortcut "Sync SMS Queue" plus a nightly automation

This covers failed POSTs. Create shortcut `Sync SMS Queue`:

1. **Get File** `smsledger/queue.txt` (turn *Error If Not Found* off)
2. **If** File *has any value*:
   - **Get Contents of URL**
     - URL: `https://tx.yourdomain.ir/ingest?split=1&source=queue`
     - Method: POST
     - Header: `Authorization: Bearer <INGEST_TOKEN>`
     - Request Body: **File**, set to the File from step 1
   - **If** Contents of URL *contains* `status` → **Delete Files** `queue.txt` (turn *Confirm* off)
3. End If

A successful response always contains `status`, so a 401/5xx never deletes the queue. If there's no network, Shortcuts stops at the POST and the queue is kept.

Automation: **Time of Day** 03:00 daily → Run Shortcut `Sync SMS Queue` → turn **Ask Before Running** off (allowed for Time of Day on iOS 15.4+).

## Step 6: Shortcut "Import SMS" (manual catch-up, any iOS)

1. **Get Clipboard**
2. **Get Contents of URL** `https://tx.yourdomain.ir/ingest?split=1&source=manual`, POST, Bearer header, Request Body **File** = Clipboard
3. **Show Result**

In Messages, long-press the SMS → **Copy** → run `Import SMS` from the widget or Back Tap. To import several at once, paste them into Notes with a `---` line between each, copy all, and run the shortcut.
Use this once to backfill your old Blu SMS history. Duplicates are ignored.

---

## Operations

**Backup** (cron on the VPS):

```bash
0 4 * * * cd /root/smsledger/deploy && docker compose exec -T smsledger python -c "import sqlite3,datetime;sqlite3.connect('/data/ledger.db').backup(sqlite3.connect(f'/data/backup-{datetime.date.today()}.db'))"
```

**Monitoring:** scrape `smsledger:8080/metrics` from inside the docker network (Caddy hides it publicly).

```yaml
- alert: SmsLedgerUnparsed
  expr: smsledger_unparsed > 0
  for: 10m
- alert: SmsLedgerSilent            # no bank SMS for 3 days = automation probably broke
  expr: time() - smsledger_last_ingest_timestamp_seconds > 3*86400
```

**Balance gaps:** each SMS carries `موجودی`. If `prev_balance ± amount ≠ balance`, the dashboard highlights that row: an SMS was missed before it. Fix it with **Import SMS**.

**API:**

| Endpoint | Auth | |
|---|---|---|
| `POST /ingest` | Bearer | JSON `{"sms": "..." \| [...], "source": "..."}`, or text/plain (`?split=1` splits on `---` lines) |
| `POST /admin/reparse` | Bearer | re-parse `unparsed` rows after adding a template |
| `GET /api/tx?status=&limit=` | Bearer/Basic | JSON |
| `GET /export.csv` | Bearer/Basic | UTF-8 BOM, so Excel shows Persian correctly |
| `GET /healthz`, `/metrics` | none | |

## Adding a bank

1. Paste 2–3 real SMS (withdrawal, deposit, transfer) into `tests/test_parsers.py`.
2. Add a `class XParser(BankParser)` in `app/parsers.py` and append it to `PARSERS`.
3. `python3 -m unittest -v`
4. `docker compose up -d --build && curl -XPOST -H "Authorization: Bearer $TOKEN" https://tx.yourdomain.ir/admin/reparse`
5. Add the bank's sender to the automation (one automation per sender).

Unknown SMS are never dropped. They're stored as `unparsed` with the raw text and show up on the dashboard and in the metric.

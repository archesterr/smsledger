"""smsledger: bank SMS -> SQLite ledger. Stdlib only.

POST /ingest            Bearer token. JSON {"sms": "..."} | {"sms": ["...", "..."]} | text/plain
                        ?split=1 on text/plain -> split on lines that are exactly "---" (queue/batch import)
POST /admin/reparse     Bearer token. Re-run parsers on unparsed rows (after adding a template)
GET  /api/tx            Basic/Bearer. ?limit=200&status=parsed|unparsed
GET  /export.csv        Basic/Bearer.
GET  /                  Basic auth. Dashboard.
GET  /healthz, /metrics Open.
"""
from __future__ import annotations

import base64
import csv
import hmac
import html
import io
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import parsers

DB_PATH = os.environ.get("DB_PATH", "/data/ledger.db")
TOKEN = os.environ.get("INGEST_TOKEN", "")
DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS", "")
LISTEN = os.environ.get("LISTEN", "0.0.0.0:8080")
MAX_BODY = 1024 * 1024
SPLIT_RE = re.compile(r"^\s*---\s*$", re.M)

log = logging.getLogger("smsledger")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sms (
  id          INTEGER PRIMARY KEY,
  hash        TEXT UNIQUE NOT NULL,
  raw         TEXT NOT NULL,
  source      TEXT,
  received_at TEXT NOT NULL,
  status      TEXT NOT NULL,          -- parsed | unparsed
  error       TEXT,
  bank        TEXT,
  direction   TEXT,                   -- IN | OUT
  amount      INTEGER,                -- rial
  balance     INTEGER,
  occurred_at TEXT,
  jdate       TEXT,
  title       TEXT
);
CREATE INDEX IF NOT EXISTS sms_occurred ON sms(occurred_at);
CREATE INDEX IF NOT EXISTS sms_status ON sms(status);
"""

TX_COLS = ("bank", "direction", "amount", "balance", "occurred_at", "jdate", "title")

_metrics = {"created": 0, "duplicate": 0, "unparsed": 0, "unauthorized": 0}
_mlock = threading.Lock()


def inc(k):
    with _mlock:
        _metrics[k] += 1


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)


def ingest_one(raw: str, source: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {"status": "empty"}
    h = parsers.fingerprint(raw)
    tx, err = parsers.parse(raw)
    row = {
        "hash": h, "raw": raw, "source": source,
        "received_at": datetime.now(parsers.TEHRAN).isoformat(timespec="seconds"),
        "status": "parsed" if tx else "unparsed", "error": err,
        **(tx.dict() if tx else dict.fromkeys(TX_COLS)),
    }
    with db() as c:
        cur = c.execute(
            f"INSERT OR IGNORE INTO sms ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
            tuple(row.values()),
        )
        if cur.rowcount == 0:
            inc("duplicate")
            return {"status": "duplicate", "hash": h}
    if tx:
        inc("created")
        log.info("tx %s %s %s bal=%s", tx.bank, tx.direction, tx.amount, tx.balance)
        return {"status": "created", "tx": tx.dict()}
    inc("unparsed")
    log.warning("unparsed (%s): %r", err, raw[:120])
    return {"status": "unparsed", "error": err}


def reparse() -> dict:
    fixed = 0
    with db() as c:
        rows = c.execute("SELECT id, raw FROM sms WHERE status='unparsed'").fetchall()
        for r in rows:
            tx, err = parsers.parse(r["raw"])
            if tx:
                d = tx.dict()
                c.execute(
                    f"UPDATE sms SET status='parsed', error=NULL, {', '.join(k + '=?' for k in d)} WHERE id=?",
                    (*d.values(), r["id"]),
                )
                fixed += 1
            else:
                c.execute("UPDATE sms SET error=? WHERE id=?", (err, r["id"]))
    return {"checked": len(rows), "fixed": fixed}


def gaps(rows) -> set[int]:
    """Rows whose balance doesn't follow from the previous one -> a missed SMS in between."""
    bad, prev = set(), {}
    for r in rows:
        if r["balance"] is None:
            continue
        p = prev.get(r["bank"])
        if p is not None:
            delta = r["amount"] if r["direction"] == "IN" else -r["amount"]
            if p + delta != r["balance"]:
                bad.add(r["id"])
        prev[r["bank"]] = r["balance"]
    return bad


class Handler(BaseHTTPRequestHandler):
    server_version = "smsledger"

    # ---- auth ----
    def _bearer_ok(self) -> bool:
        a = self.headers.get("Authorization", "")
        return bool(TOKEN) and a.startswith("Bearer ") and hmac.compare_digest(a[7:].strip(), TOKEN)

    def _basic_ok(self) -> bool:
        a = self.headers.get("Authorization", "")
        if not (DASH_PASS and a.startswith("Basic ")):
            return False
        try:
            u, _, p = base64.b64decode(a[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, DASH_USER) and hmac.compare_digest(p, DASH_PASS)

    def _deny(self, basic=False):
        inc("unauthorized")
        self.send_response(401)
        if basic:
            self.send_header("WWW-Authenticate", 'Basic realm="smsledger"')
        self.end_headers()

    # ---- io ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> str:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n).decode("utf-8", "replace")

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- routes ----
    def do_POST(self):
        u = urlparse(self.path)
        if not self._bearer_ok():
            return self._deny()
        if u.path == "/ingest":
            try:
                body = self._body()
            except ValueError as e:
                return self._send(413, {"error": str(e)})
            q = parse_qs(u.query)
            source = q.get("source", ["unknown"])[0]
            ctype = self.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return self._send(400, {"error": "bad json"})
                sms = data.get("sms")
                source = data.get("source", source)
                items = sms if isinstance(sms, list) else [sms or ""]
            elif q.get("split", ["0"])[0] == "1":
                items = [b for b in SPLIT_RE.split(body.replace("\r", "")) if b.strip()]
            else:
                items = [body]
            results = [ingest_one(str(s), source) for s in items]
            return self._send(200, results[0] if len(results) == 1 else {"results": results})
        if u.path == "/admin/reparse":
            return self._send(200, reparse())
        self._send(404, {"error": "not found"})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if u.path == "/metrics":
            return self._send(200, self._metrics(), "text/plain; version=0.0.4")
        if not (self._basic_ok() or self._bearer_ok()):
            return self._deny(basic=True)
        q = parse_qs(u.query)
        if u.path == "/api/tx":
            limit = min(int(q.get("limit", ["200"])[0]), 5000)
            status = q.get("status", [None])[0]
            sql = "SELECT * FROM sms" + (" WHERE status=?" if status else "") + \
                  " ORDER BY COALESCE(occurred_at, received_at) DESC, id DESC LIMIT ?"
            with db() as c:
                rows = c.execute(sql, (status, limit) if status else (limit,)).fetchall()
            return self._send(200, [dict(r) for r in rows])
        if u.path == "/export.csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            cols = ("id", "jdate", "occurred_at", "bank", "direction", "amount", "balance", "title", "status", "raw")
            w.writerow(cols)
            with db() as c:
                for r in c.execute(f"SELECT {','.join(cols)} FROM sms ORDER BY COALESCE(occurred_at, received_at)"):
                    w.writerow(tuple(r))
            return self._send(200, "﻿" + buf.getvalue(), "text/csv; charset=utf-8")
        if u.path == "/":
            return self._send(200, self._dashboard(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def _metrics(self) -> str:
        with db() as c:
            unparsed = c.execute("SELECT COUNT(*) FROM sms WHERE status='unparsed'").fetchone()[0]
            last = c.execute("SELECT MAX(received_at) FROM sms").fetchone()[0]
        out = ["# TYPE smsledger_ingest_total counter"]
        with _mlock:
            out += [f'smsledger_ingest_total{{result="{k}"}} {v}' for k, v in _metrics.items()]
        out += ["# TYPE smsledger_unparsed gauge", f"smsledger_unparsed {unparsed}"]
        if last:
            ts = datetime.fromisoformat(last).timestamp()
            out += ["# TYPE smsledger_last_ingest_timestamp_seconds gauge",
                    f"smsledger_last_ingest_timestamp_seconds {ts:.0f}"]
        return "\n".join(out) + "\n"

    def _dashboard(self) -> str:
        with db() as c:
            rows = c.execute("SELECT * FROM sms WHERE status='parsed' ORDER BY occurred_at, id").fetchall()
            unparsed = c.execute("SELECT * FROM sms WHERE status='unparsed' ORDER BY id DESC LIMIT 50").fetchall()
        bad = gaps(rows)
        months: dict[str, list[int]] = {}
        for r in rows:
            m = (r["jdate"] or "????-??")[:7]
            v = months.setdefault(m, [0, 0])
            v[0 if r["direction"] == "IN" else 1] += r["amount"]
        e = html.escape
        GAP_ATTR = ' class=gap title="balance jump: SMS missed before this one"'
        fmt = lambda n: "" if n is None else f"{n:,}"
        month_rows = "".join(
            f"<tr><td>{e(m)}</td><td class=in>{fmt(i)}</td><td class=out>{fmt(o)}</td><td>{fmt(i - o)}</td></tr>"
            for m, (i, o) in sorted(months.items(), reverse=True))
        tx_rows = "".join(
            f"<tr{GAP_ATTR if r['id'] in bad else ''}>"
            f"<td>{e(r['jdate'] or '')} {e((r['occurred_at'] or '')[11:16])}</td><td>{e(r['bank'])}</td>"
            f"<td class={'in' if r['direction'] == 'IN' else 'out'}>{'+' if r['direction'] == 'IN' else '−'}{fmt(r['amount'])}</td>"
            f"<td>{fmt(r['balance'])}</td><td>{e(r['title'] or '')}</td></tr>"
            for r in reversed(rows[-300:]))
        un_rows = "".join(f"<tr><td>{r['id']}</td><td>{e(r['error'] or '')}</td><td><pre>{e(r['raw'])}</pre></td></tr>"
                          for r in unparsed)
        return f"""<!doctype html><html lang=fa><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>smsledger</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--mute:#666;--line:#e5e5e5;--in:#0a7d33;--out:#b42318;--gap:#fff4d6}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mute:#999;--line:#2a2a2a;--in:#4ade80;--out:#f87171;--gap:#3a2f10}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Vazirmatn,sans-serif;margin:0 auto;max-width:960px;padding:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:start;font-variant-numeric:tabular-nums}}
th{{color:var(--mute);font-weight:500}}.in{{color:var(--in)}}.out{{color:var(--out)}}tr.gap{{background:var(--gap)}}
pre{{margin:0;white-space:pre-wrap;font:12px ui-monospace,monospace}}h2{{font-size:16px;margin:24px 0 4px}}a{{color:inherit}}
</style>
<h1 style="font-size:20px">smsledger</h1><p style="color:var(--mute)">{len(rows)} tx · {len(unparsed)} unparsed · {len(bad)} balance gaps · <a href=/export.csv>CSV</a></p>
<h2>Monthly (Jalali)</h2><table><tr><th>Month</th><th>In</th><th>Out</th><th>Net</th></tr>{month_rows}</table>
<h2>Transactions</h2><table><tr><th>When</th><th>Bank</th><th>Amount (IRR)</th><th>Balance</th><th>Title</th></tr>{tx_rows}</table>
{f'<h2>Unparsed</h2><table><tr><th>ID</th><th>Error</th><th>Raw</th></tr>{un_rows}</table>' if un_rows else ''}
</html>"""


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if len(TOKEN) < 24:
        raise SystemExit("INGEST_TOKEN must be set (>=24 chars): openssl rand -hex 24")
    init_db()
    host, port = LISTEN.rsplit(":", 1)
    log.info("listening on %s, db=%s", LISTEN, DB_PATH)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()

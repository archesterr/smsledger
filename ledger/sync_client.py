#!/usr/bin/env python3
"""SMS Ledger: send the bank SMS already on an iPhone to the ledger, from a Linux or Mac computer.

The app's import page («وارد کردن پیامک») gives the command, with a one-off key in it:
    python3 -c "import urllib.request as u; exec(u.urlopen('https://SERVER/sync.py').read())" sml_...

Everything below happens on this computer:
  1. pymobiledevice3 is installed in its own folder (first run only).
  2. The iPhone is backed up keeping only its Messages database (sms.db); nothing else from the
     phone is written to disk. The first run still reads the whole phone (iOS offers no other way
     to reach old SMS), so it takes a while; later runs only fetch what changed.
  3. Bank SMS are picked out: SMS (not iMessage) that mention a balance («موجودی» / «مانده»),
     never one-time codes. You choose the senders and the period.
  4. Only those are sent, with a key that can only add SMS. The key is revoked at the end.
Personal messages never leave this computer.

--db PATH reads an existing sms.db, a Mac's ~/Library/Messages/chat.db or a backup folder instead.
Standard library only, Python 3.9+: it runs on the system python3 of Ubuntu 22.04+ and macOS.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Filled in when the server sends this file (ledger.views.api.sync_script).
CONFIG = {"server": "http://127.0.0.1:8000/", "otp": "OTP", "periods": [{"key": "all", "en": "All", "start": 0}]}

TOOL = "pymobiledevice3==11.19.1"
HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "smsledger-sync"
SMS_DB = "3d0d7e5fb2ce288813306e4d4636395e047a3d28"  # HomeDomain/Library/SMS/sms.db in a backup
APPLE_EPOCH = 978307200  # 2001-01-01, Apple's time zero
BANK = re.compile("موجود[یي]|مانده")  # what the Message automation looks for: a balance line
MOBILE = re.compile(r"^(\+?98|0098|0)?9\d{9}$")  # Iranian mobile numbers are people, not banks
BATCH = 200  # SMS per request: well under the server's 1 MB body limit
MAX_CHARS = 2000


class Stop(Exception):
    """A problem the user can fix: shown without a traceback."""


def say(text: str = "") -> None:
    print(text, flush=True)


def step(text: str) -> None:
    say(f"\n==> {text}")


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise Stop("There's no terminal to answer questions in. Run the command exactly as the import page\n"
                   "shows it, in a terminal, or add --yes to send with the default choices.") from None


def yes(prompt: str, default: bool = True) -> bool:
    answer = ask(f"{prompt} [{'Y/n' if default else 'y/N'}] ").lower()
    return default if not answer else answer.startswith("y")


def num(n: int) -> str:
    return f"{n:,}"


# ---- the backup tool ------------------------------------------------------------------------------
def apt_install(packages: list[str]) -> bool:
    if not shutil.which("apt-get"):
        return False
    say(f"This needs the system package(s): {' '.join(packages)}")
    if not yes("Install with sudo apt-get now?"):
        return False
    return subprocess.run(["sudo", "apt-get", "install", "-y", *packages]).returncode == 0


def tool() -> Path:
    """pymobiledevice3 in its own virtualenv under HOME, installed on the first run."""
    venv = HOME / "venv"
    exe, stamp = venv / "bin" / "pymobiledevice3", HOME / "tool.txt"
    if exe.exists() and stamp.exists() and stamp.read_text().strip() == TOOL:
        return exe
    step("Installing the iPhone backup tool (first run only: about 350 MB, a few minutes)")
    if not (venv / "bin" / "python").exists():
        HOME.mkdir(parents=True, exist_ok=True)
        if subprocess.run([sys.executable, "-m", "venv", str(venv)]).returncode != 0:
            shutil.rmtree(venv, ignore_errors=True)  # Debian/Ubuntu ship venv separately
            if not apt_install(["python3-venv"]) or \
                    subprocess.run([sys.executable, "-m", "venv", str(venv)]).returncode != 0:
                raise Stop("Could not create a Python virtualenv. On Ubuntu: sudo apt install python3-venv")
    pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    if subprocess.run([*pip, TOOL]).returncode != 0:
        raise Stop(f"Installing {TOOL} failed (see above). Check the internet connection and run the command again.")
    stamp.write_text(TOOL)
    return exe


def usbmuxd_missing() -> bool:
    return sys.platform.startswith("linux") and not any(
        Path(p).exists() for p in ("/usr/sbin/usbmuxd", "/usr/bin/usbmuxd", "/sbin/usbmuxd"))


def wait_for_iphone(exe: Path) -> str:
    """The UDID of the first iPhone on USB."""
    step("Connect the iPhone with a cable and unlock it")
    waited = 0
    while True:
        r = subprocess.run([str(exe), "usbmux", "list", "--usb", "--simple"], capture_output=True, text=True)
        try:
            found = json.loads(r.stdout) if r.returncode == 0 else []
        except ValueError:
            found = []
        if found:
            say(f"Found the iPhone ({found[0]}).")
            return found[0]
        if r.returncode != 0 and usbmuxd_missing():
            if not apt_install(["usbmuxd"]):
                raise Stop("usbmuxd is needed to talk to the iPhone. On Ubuntu: sudo apt install usbmuxd")
            continue
        if waited == 0:
            say("Waiting for the iPhone… (Ctrl+C to stop)")
        elif waited == 30 and sys.platform.startswith("linux"):
            say("Still nothing. Try another cable or port; if it's connected, try: sudo systemctl restart usbmuxd")
        time.sleep(2)
        waited += 2


def backup(exe: Path, udid: str) -> Path:
    folder = HOME / "backup"
    first = not (folder / udid / "Manifest.db").exists()
    step("Backing up the iPhone's messages")
    say("If the iPhone asks \"Trust This Computer?\", tap Trust and enter the iPhone passcode.")
    if first:
        say("First time: the iPhone goes through all its data, so this can take 10-60 minutes.\n"
            "Only the messages file is kept on this computer. Next time it takes a minute or two.")
    say("Keep the iPhone connected and unlocked until it finishes.\n")
    if subprocess.run([str(exe), "backup2", "backup", "--only", "sms", "--udid", udid, str(folder)]).returncode:
        raise Stop("The backup didn't finish. Keep the iPhone unlocked and connected, then run the same command again.")
    return folder / udid


# ---- finding the Messages database ----------------------------------------------------------------
def backup_dir(path: Path) -> Path | None:
    """The folder of one device's backup: `path` itself or its newest subfolder with a Manifest.plist."""
    if (path / "Manifest.plist").exists():
        return path
    found = sorted((p.parent for p in path.glob("*/Manifest.plist")), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def decrypt_sms_db(folder: Path) -> Path:
    """An encrypted backup's sms.db, decrypted to a temporary file with pyiosbackup (installed with
    the tool). The password goes to it on stdin, never on a command line."""
    python = tool().parent / "python"
    code = ("import sys; from pyiosbackup import Backup; b = Backup.from_path(sys.argv[1], sys.stdin.readline()[:-1]); "
            "open(sys.argv[2], 'wb').write(b.get_entry_by_domain_and_path('HomeDomain', 'Library/SMS/sms.db')"
            ".read_bytes())")
    say("\nThis iPhone's backups are encrypted (a password was set for them in iTunes/Finder at some point).")
    for _ in range(3):
        password = getpass.getpass("Backup password (used here only): ")
        fd, out = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        r = subprocess.run([str(python), "-c", code, str(folder), out], input=password + "\n", text=True,
                           capture_output=True)
        if r.returncode == 0:
            return Path(out)
        os.unlink(out)
        say("That didn't work: wrong password?")
    raise Stop("Could not open the encrypted backup. Forgot the password? On the iPhone: Settings > General >\n"
               "Transfer or Reset iPhone > Reset > Reset All Settings clears it (your data stays).")


def sms_db(path: Path) -> tuple[Path, bool]:
    """(the database, whether it's a temporary copy to delete afterwards)."""
    if path.is_file():
        return path, False
    folder = backup_dir(path)
    if folder is None:
        raise Stop(f"No iPhone backup or Messages database at {path}")
    with open(folder / "Manifest.plist", "rb") as f:
        encrypted = plistlib.load(f).get("IsEncrypted", False)
    if encrypted:
        return decrypt_sms_db(folder), True
    for p in (folder / SMS_DB[:2] / SMS_DB, folder / SMS_DB):
        if p.exists():
            return p, False
    raise Stop(f"The backup at {folder} has no Messages database.")


# ---- reading it -----------------------------------------------------------------------------------
def from_attributed(blob: bytes | None) -> str:
    """iOS 16+ keeps many texts only in attributedBody (an archived NSAttributedString): the text
    follows the "NSString" class name, after 5 marker bytes and a length."""
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    p = i + len(b"NSString") + 5
    n = blob[p]
    if n == 0x81:
        n, p = int.from_bytes(blob[p + 1:p + 3], "little"), p + 3
    elif n == 0x82:
        n, p = int.from_bytes(blob[p + 1:p + 4], "little"), p + 4
    else:
        p += 1
    return blob[p:p + n].decode("utf-8", "replace")


def arrival_ms(date) -> int:
    if not date:
        return 0
    seconds = date / 1e9 if date > 1e11 else date  # nanoseconds since iOS 11, seconds before
    return int((seconds + APPLE_EPOCH) * 1000)


def read_bank_sms(path: Path, otp: re.Pattern) -> tuple[int, list[dict]]:
    """(messages scanned, bank SMS as {"sender", "text", "at"}) oldest first."""
    live = Path(f"{path}-wal").exists()  # a Mac's chat.db in use; a backup's copy is a plain file
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro{'' if live else '&immutable=1'}", uri=True)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(message)")}
        if "text" not in cols:
            raise Stop(f"{path} is not a Messages database.")
        body = "m.attributedBody" if "attributedBody" in cols else "NULL"
        rows = con.execute(f"SELECT h.id, m.text, {body}, m.date, m.service FROM message m "
                           "LEFT JOIN handle h ON h.ROWID = m.handle_id WHERE m.is_from_me = 0 ORDER BY m.date")
        scanned, found = 0, []
        for sender, text, attributed, date, service in rows:
            scanned += 1
            text = (text or from_attributed(attributed) or "").strip()
            if service == "iMessage" or not text or not BANK.search(text) or otp.search(text):
                continue
            found.append({"sender": sender or "?", "text": text[:MAX_CHARS], "at": arrival_ms(date)})
    except sqlite3.DatabaseError as e:
        raise Stop(f"Could not read {path}: {e}") from e
    finally:
        con.close()
    return scanned, found


# ---- choosing -------------------------------------------------------------------------------------
def senders_of(sms: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in sms:
        s = out.setdefault(m["sender"], {"n": 0, "on": not MOBILE.match(re.sub(r"[\s-]", "", m["sender"]))
                                         and "@" not in m["sender"]})
        s["n"] += 1
        s["sample"] = m["text"].split("\n")[0][:40]  # the newest wins
    return out


def pick_senders(senders: dict[str, dict]) -> None:
    names = sorted(senders, key=lambda s: -senders[s]["n"])
    while True:
        say("\nSenders (only ticked ones are sent; mobile numbers start unticked):")
        for i, s in enumerate(names, 1):
            info = senders[s]
            say(f"  [{'x' if info['on'] else ' '}] {i:>2}  {s:<16} {num(info['n']):>7}   {info['sample']}")
        answer = ask("Type numbers to tick/untick (e.g. 2 5), or press Enter to continue: ")
        if not answer:
            return
        for token in re.split(r"[\s,]+", answer):
            if token.isdigit() and 1 <= int(token) <= len(names):
                s = senders[names[int(token) - 1]]
                s["on"] = not s["on"]


def pick_period(sms: list[dict], periods: list[dict]) -> dict:
    say("\nWhich SMS?")
    for i, p in enumerate(periods, 1):
        say(f"  {i}) {p['en']:<36} {num(sum(m['at'] >= p['start'] for m in sms)):>7}")
    while True:
        answer = ask(f"Choose 1-{len(periods)} [{len(periods)}]: ") or str(len(periods))
        if answer.isdigit() and 1 <= int(answer) <= len(periods):
            return periods[int(answer) - 1]


# ---- sending --------------------------------------------------------------------------------------
def post(url: str, key: str, data: dict | None) -> dict:
    body = json.dumps(data or {}, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "smsledger-sync"})
    for wait in (5, 15, 30, 60, None):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise Stop("The key in this command has expired or was already used.\n"
                           "Make a new command on the import page and run it again.") from e
            if e.code not in (429, 500, 502, 503, 504) or wait is None:
                raise Stop(f"The server said {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from e
        except (urllib.error.URLError, OSError) as e:
            if wait is None:
                raise Stop(f"Could not reach {url}: {e}") from e
        say(f"  (server busy or unreachable; trying again in {wait} s)")
        time.sleep(wait)
    raise AssertionError("unreachable")


def send(sms: list[dict], key: str) -> dict:
    url = CONFIG["server"].rstrip("/") + "/ingest"
    counts: dict[str, int] = {}
    for i in range(0, len(sms), BATCH):
        chunk = [{"text": m["text"], "at": m["at"]} for m in sms[i:i + BATCH]]
        res = post(url, key, {"items": chunk, "source": "backup"})
        for k, v in res.get("count", {}).items():
            counts[k] = counts.get(k, 0) + v
        done = min(i + BATCH, len(sms))
        bar = "#" * (30 * done // len(sms))
        print(f"\r  [{bar:<30}] {num(done)} / {num(len(sms))}", end="", flush=True)
    say()
    post(url + "?source=done", key, None)  # the key has done its job: the server revokes it
    return counts


# ---- main -----------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sync.py", description="Send the bank SMS on an iPhone to SMS Ledger.")
    # run as `python3 -c "…exec(…)" KEY` (the import page's command) or as a file: argv[1:] either way
    ap.add_argument("key", help="the one-off key from the app's import page (sml_...)")
    ap.add_argument("--db", type=Path, help="an existing sms.db, chat.db or iPhone backup folder (no iPhone needed)")
    ap.add_argument("--period", choices=[p["key"] for p in CONFIG["periods"]], help="skip the period question")
    ap.add_argument("--yes", action="store_true", help="don't ask: default senders, send at once")
    args = ap.parse_args(argv)
    if sys.version_info < (3, 9):
        raise Stop("This needs Python 3.9 or newer (Ubuntu 22.04 or later).")
    if not args.key.startswith("sml_"):
        ap.error("the key starts with sml_ (copy the whole command from the import page)")

    say(f"SMS Ledger: old SMS from an iPhone -> {CONFIG['server']}")
    say("Personal messages stay on this computer; only the bank SMS you choose are sent.")
    temp = False
    try:
        if args.db:
            path, temp = sms_db(args.db.expanduser())
        else:
            exe = tool()
            path, temp = sms_db(backup(exe, wait_for_iphone(exe)))
        step("Looking for bank SMS (on this computer)")
        scanned, sms = read_bank_sms(path, re.compile(CONFIG["otp"], re.I))
    finally:
        if temp:
            os.unlink(path)
    say(f"{num(scanned)} messages read; {num(len(sms))} bank SMS found.")
    if not sms:
        say("Nothing to send: no SMS with «موجودی» or «مانده» in them.")
        return 0

    senders = senders_of(sms)
    if not args.yes:
        pick_senders(senders)
    periods = {p["key"]: p for p in CONFIG["periods"]}
    period = periods[args.period] if args.period else periods["all"] if args.yes else \
        pick_period(sms, CONFIG["periods"])
    chosen = [m for m in sms if m["at"] >= period["start"] and senders[m["sender"]]["on"]]
    if not chosen:
        say("No SMS from the ticked senders in that period.")
        return 0
    if not args.yes and not yes(f"\nSend {num(len(chosen))} SMS ({period['en']})?"):
        return 0

    step("Sending")
    counts = send(chosen, args.key)
    new = counts.get("received", 0) + counts.get("created", 0) + counts.get("unparsed", 0)
    say(f"Done: {num(new)} new, {num(counts.get('duplicate', 0))} already there, "
        f"{num(counts.get('ignored', 0))} skipped (one-time codes).")
    say(f"\nOpen {CONFIG['server']} and sign in: the SMS become transactions when the app opens.")
    if not args.db:
        say(f"The messages-only backup stays in {HOME / 'backup'} so the next run is quick.\n"
            f"To remove everything this command installed: rm -rf {HOME}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as e:
        say(f"\n{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        say("\nStopped.")
        sys.exit(130)

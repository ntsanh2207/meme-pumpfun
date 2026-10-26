#!/usr/bin/env python3
"""Tiny web server for the pump.fun graduated-tokens page.

Serves index.html plus the files the other processes write into the data directory.
Usage: python3 server.py [--port 8090] [--data data]
"""
import argparse
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import notify
import radar
import recorder
import rules
import wallets

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_DAYS = 30           # a login lasts this long
COOKIE = "pg_session"
_sessions = {}              # token -> {exp, created, last (unix seconds), ip, ua}; also kept in settings/sessions.json
_sessions_lock = threading.Lock()

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Sign in · Pumpfun Graduates</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@700;800&display=swap">
<style>
:root{color-scheme:light;--bg:#F3F5FA;--surface:#FFFFFF;--surface-2:#F7F8FC;--ink:#0E1525;--muted:#5A6478;--line:#E4E8F0;--accent:#5146E5;--accent-2:#7C3AED;--accent-soft:#EEEDFE;--crit:#E0404A;
--shadow:0 2px 4px rgba(16,24,40,.06),0 18px 48px rgba(16,24,40,.10)}
@media (prefers-color-scheme: dark){:root{color-scheme:dark;--bg:#0A0E17;--surface:#121827;--surface-2:#161E30;--ink:#E9EDF5;--muted:#9AA3B8;--line:#222B3D;--accent:#8B83FF;--accent-2:#192ee5;--accent-soft:#1E1F45;--crit:#FF6B72;
--shadow:0 2px 6px rgba(0,0,0,.45),0 18px 48px rgba(0,0,0,.4);--on-accent:#0A0E17}}
*{box-sizing:border-box}
body{margin:0;color:var(--ink);font:15px/1.5 "Inter",system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased;
display:grid;place-items:center;min-height:100vh;padding:16px;background:var(--bg);
background-image:radial-gradient(900px 480px at 80% -10%,color-mix(in srgb,var(--accent) 16%,transparent),transparent 70%),
radial-gradient(800px 460px at 0% 110%,color-mix(in srgb,var(--accent-2) 12%,transparent),transparent 70%)}
form{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:34px 32px 30px;width:100%;max-width:400px;box-shadow:var(--shadow)}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:26px}
.brand::before{content:"";width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent-2));
box-shadow:0 6px 18px color-mix(in srgb,var(--accent) 40%,transparent)}
.brand b{font-family:"Plus Jakarta Sans","Inter",sans-serif;font-size:17px;font-weight:800;letter-spacing:.04em}
.brand span{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;padding:2px 9px;border-radius:999px;background:var(--accent-soft);color:var(--accent)}
h1{font-family:"Plus Jakarta Sans","Inter",sans-serif;font-size:24px;font-weight:800;letter-spacing:-.02em;margin:0 0 6px}
p{color:var(--muted);margin:0 0 22px;font-size:14px}
input,button{font:inherit;width:100%;border-radius:11px;padding:12px 14px}
input{border:1px solid var(--line);background:var(--surface-2);color:var(--ink);margin-bottom:14px}
input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
button{border:0;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:var(--on-accent,#fff);font-weight:600;cursor:pointer;
box-shadow:0 6px 18px color-mix(in srgb,var(--accent) 38%,transparent)}
button:hover{filter:brightness(1.07)}
.err{color:var(--crit);font-size:13px;margin:-6px 0 14px}
</style></head><body><form method="post" action="/login">
<div class="brand"><b>PUMPFUN</b><span>Graduates</span></div>
<h1>Welcome back</h1><p>Sign in to follow graduated pump.fun tokens, strategies and trades.</p>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required aria-label="Password">
%ERROR%<button type="submit">Sign in</button></form></body></html>"""


def _sessions_file(settings):
    return os.path.join(settings, "sessions.json")


def load_sessions(settings):
    try:
        with open(_sessions_file(settings), encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not isinstance(v, dict):    # a file from before sessions carried details: just the expiry
                    v = {"exp": v, "created": v - SESSION_DAYS * 86400, "last": None, "ip": "", "ua": ""}
                if v["exp"] > time.time():
                    _sessions[k] = v
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass


def save_sessions(settings):
    tmp = _sessions_file(settings) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in _sessions.items() if v["exp"] > time.time()}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _sessions_file(settings))


# A guest (settings/guest.json, same format as login.json) may look at the Lab and Trading pages (these reads, nothing that changes
# anything) and use the Coins Review and Wallets Review pages in full (radar.GUEST_GET / GUEST_POST). Everything else answers 403,
# whatever the page asks for.
GUEST_GET = {"/", "/radar.js", "/radar2.js", "/api/rules", "/api/trading", "/api/backtest/schedule"}


def guest_stamp(settings):
    """A fingerprint of the guest password in force: a guest session from another (or a removed) one is signed out."""
    try:
        with open(os.path.join(settings, "guest.json"), encoding="utf-8") as f:
            return hashlib.sha256(json.load(f)["hash"].encode()).hexdigest()[:16]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def role_for(settings, given):
    """'admin' for the account's password, 'guest' for the guest password, else None."""
    if password_ok(settings, given):
        return "admin"
    if password_ok(settings, given, "guest.json"):
        return "guest"
    return None


def password_ok(settings, given, name="login.json"):
    """Checks the password against settings/<name>: {"salt": hex, "hash": hex of PBKDF2-SHA256}."""
    try:
        with open(os.path.join(settings, name), encoding="utf-8") as f:
            cfg = json.load(f)
        want = bytes.fromhex(cfg["hash"])
        got = hashlib.pbkdf2_hmac("sha256", given.encode(), bytes.fromhex(cfg["salt"]), int(cfg.get("rounds", 200000)))
        return hmac.compare_digest(want, got)
    except (OSError, ValueError, KeyError, TypeError):
        return False
def browser_of(ua):
    """A short name for a User-Agent: "Chrome on macOS"."""
    ua = ua or ""
    name = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "A browser")
    system = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "macOS"), ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "")
    return f"{name} on {system}" if system else name


def saved_rpc(settings):
    """The RPC address the trader uses (the one saved from this page or the Trading tab, else the free public one)."""
    try:
        with open(os.path.join(settings, "trading.json"), encoding="utf-8") as f:
            url = json.load(f).get("rpc_url")
        if isinstance(url, str) and url.startswith("https://"):
            return url, True
    except (OSError, ValueError, AttributeError):
        pass
    return "https://api.mainnet-beta.solana.com", False


def test_rpc(url):
    """Asks an RPC for its newest slot and times it. Returns (result, error); an error never contains the address, which may hold a key.
    The address must be https and resolve only to public addresses: this server makes the request."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or len(url) > 300:
            return None, "The RPC address must start with https://"
        for info in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return None, "That address points at a private network."
    except (ValueError, OSError):
        return None, "That address could not be looked up."
    req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getSlot"}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "pumpgrad"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read(65536))
    except urllib.error.HTTPError as e:
        return None, f"The provider answered HTTP {e.code}" + (": the key was refused." if e.code in (401, 403) else ".")
    except (urllib.error.URLError, OSError, ValueError):
        return None, "It did not answer in 8 seconds, or the answer was not JSON-RPC."
    slot = body.get("result") if isinstance(body, dict) else None
    if not isinstance(slot, int):
        return None, "It answered, but not with a slot number."
    return {"ms": round((time.time() - t0) * 1000), "slot": slot}, None


MAX_BODY = 20000   # bytes accepted for a settings save
_save_lock = threading.Lock()

# path -> (file, content type, download name)
ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8", None),
    "/api/report": ("report.json", "application/json; charset=utf-8", None),
    "/api/history": ("history.json", "application/json; charset=utf-8", None),
    "/radar.js": ("radar.js", "application/javascript; charset=utf-8", None),
    "/radar2.js": ("radar2.js", "application/javascript; charset=utf-8", None),
    "/api/trading": ("trading_state.json", "application/json; charset=utf-8", None),
    "/api/dipwatch": ("dipwatch.json", "application/json; charset=utf-8", None),   # the Dip watch page (the account only)
    "/api/roundtrips.csv": ("roundtrips.csv", "text/csv; charset=utf-8", "pump_rule_round_trips.csv"),
    "/api/study.csv": ("study.csv", "text/csv; charset=utf-8", "pump_entry_study.csv"),
}
_gz_cache = {}  # file path -> (mtime, gzipped bytes)
MINT = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")   # a Solana address, and nothing else
STRATEGY_ID = re.compile(r"[a-z0-9-]{1,40}")         # a rule id (rules.validate), and nothing else


# The databases the Recorder tab lists: file, what it holds, and the tables whose rows are counted.
STORAGE_DBS = [
    ("watch.db", "The live record: what was measured on every new graduate and every strategy's decision",
     [("tokens", "tokens recorded"), ("measures", "measurements"), ("decisions", "decisions")]),
    ("pump.db", "Coin details and the 1-minute price history the backtest reads",
     [("tokens", "coins"), ("candles", "price bars")]),
    ("holders.db", "Holder snapshots of each new graduate's first hour",
     [("holder_watch", "graduates"), ("holder_snaps", "snapshots")]),
    ("trades.db", "The trader's orders", [("trades", "orders")]),
]
_storage_cache = [0.0, None]


def storage_report(data):
    """Sizes of the databases (with their temporary write-ahead files), what is in them, and the disk. Kept for 30 s:
    counting rows and walking the data folder is not free."""
    now = time.time()
    if _storage_cache[1] is not None and now - _storage_cache[0] < 30:
        return _storage_cache[1]
    size = lambda p: os.path.getsize(p) if os.path.exists(p) else 0   # noqa: E731
    dbs, in_dbs = [], 0
    for name, what, tables in STORAGE_DBS:
        path = os.path.join(data, name)
        if not os.path.exists(path):
            continue
        temp = size(path + "-wal") + size(path + "-shm")
        rows = {}
        # (a database nobody has open has no temporary files, and this service may not create them: read it as it lies)
        for flags in ("mode=ro", "mode=ro&immutable=1"):
            try:
                with sqlite3.connect(f"file:{path}?{flags}", uri=True, timeout=2) as db:
                    rows = {label: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table, label in tables}
                break
            except sqlite3.Error:
                rows = {}
        dbs.append({"name": name, "what": what, "bytes": size(path), "temp_bytes": temp, "rows": rows})
        in_dbs += size(path) + temp
    total = 0
    for d, _, fs in os.walk(data):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(d, f))
            except OSError:   # gone since the folder was listed
                pass
    disk = shutil.disk_usage(data)
    doc = {"generated_at": int(now * 1000), "databases": dbs, "databases_bytes": in_dbs, "data_bytes": total,
           "disk": {"total": disk.total, "used": disk.used, "free": disk.free}}
    _storage_cache[0], _storage_cache[1] = now, doc
    return doc


def file_key(st):
    """What identifies one version of a file: a rewrite through os.replace is a new inode even within one clock tick."""
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def gzipped(path):
    key = file_key(os.stat(path))
    hit = _gz_cache.get(path)
    if not hit or hit[0] != key:
        with open(path, "rb") as f:
            hit = (key, gzip.compress(f.read(), 6))
        _gz_cache[path] = hit
    return hit[1]


BACKTEST_EVERY = 24 * 3600  # pump-graduated-analyze.timer: OnCalendar=*-*-* 01:00:00 UTC (once a day)
BACKTEST_AT_UTC_HOUR = 1


def backtest_schedule(data_dir):
    """When the full backtest last ran, when it runs next, and whether a backtest is running now.
    Asks systemd (the units may not exist on a development machine); falls back to report.json's age."""
    def show(unit, *props):
        try:
            out = subprocess.run(["systemctl", "show", unit, "--timestamp=unix"] + ["-p" + p for p in props],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    def ts(v):
        return int(v[1:]) * 1000 if v and v.startswith("@") and v[1:].isdigit() else None
    full = show("pump-graduated-analyze.service", "ActiveState", "InactiveEnterTimestamp", "ExecMainStartTimestamp")
    saved = show("pump-graduated-rules.service", "ActiveState")
    last = ts(full.get("InactiveEnterTimestamp"))
    if last is None:
        try:
            last = int(os.path.getmtime(os.path.join(data_dir, "report.json")) * 1000)
        except OSError:
            pass
    running = full.get("ActiveState") in ("active", "activating")
    now = time.time()
    nxt = (now - now % 86400) + BACKTEST_AT_UTC_HOUR * 3600     # today's 01:00 UTC, or tomorrow's once it has passed
    if nxt <= now:
        nxt += 86400
    return {"last": last, "next": int(nxt * 1000), "every": BACKTEST_EVERY,
            "running": running, "running_since": ts(full.get("ExecMainStartTimestamp")) if running else None,
            "rules_running": saved.get("ActiveState") in ("active", "activating")}


HELD_STATES = ("buying", "buy_sent", "open", "selling", "sell_sent", "check", "stuck")   # trader.py's states of a position still held


class Handler(BaseHTTPRequestHandler):
    server_version = "pumpgrad"
    data_dir = "data"

    def session(self):
        """The session token of a signed-in browser, or None."""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                with _sessions_lock:
                    sess = _sessions.get(v)
                    if sess and sess["exp"] > time.time():
                        sess["last"] = time.time()
                        return v
        return None

    def role(self):
        """'admin', 'guest' or None (not signed in). Sessions from before guests existed are the account's."""
        token = self.session()
        if not token:
            return None
        with _sessions_lock:
            sess = dict(_sessions.get(token) or {})
        if sess.get("role") == "guest" and sess.get("guest") != guest_stamp(self.settings_dir()):
            with _sessions_lock:
                _sessions.pop(token, None)
                save_sessions(self.settings_dir())
            return None
        return sess.get("role", "admin")

    def require_login(self, path, write=False):
        """True when the request may go on; otherwise the answer (a redirect, a 401 or a 403) is sent."""
        role = self._role = self.role()          # (read once: the handlers below use this value, not a second read)
        radar_part = path[len("/api/radar/"):] if path.startswith("/api/radar/") else None
        guest_ok = (radar_part in (radar.GUEST_POST if write else radar.GUEST_GET)) if radar_part is not None else (not write and (path in GUEST_GET or path.startswith("/api/strategy/")))
        if role == "guest" and not guest_ok:
            if path.startswith("/api/") or write:
                self.json(403, {"error": "The guest account can use the Lab, Trading, Coins Review and Wallets Review pages only."})
            else:
                self.reply(303, b"", "text/plain", {"Location": "/#lab"})
            return False
        if role:
            return True
        if path.startswith("/api/"):
            self.json(401, {"error": "Not signed in. Reload the page to sign in."})
        else:
            self.reply(303, b"", "text/plain", {"Location": "/login"})
        return False

    def login_page(self, error=""):
        body = LOGIN_PAGE.replace("%ERROR%", f'<div class="err">{error}</div>' if error else "").encode()
        self.reply(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})


    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.data_dir = Handler.data_dir
        if path == "/healthz":
            return self.reply(200, b"ok\n", "text/plain")
        if path == "/login":
            if self.session():
                return self.reply(303, b"", "text/plain", {"Location": "/"})
            return self.login_page()
        if path == "/logout":
            token = self.session()
            if token:
                with _sessions_lock:
                    _sessions.pop(token, None)
                    save_sessions(self.settings_dir())
            return self.reply(303, b"", "text/plain", {"Location": "/login",
                                                        "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        if not self.require_login(path):
            return
        if path.startswith("/api/radar/"):      # the Coins, Wallets and Monitor pages
            query = self.path.partition("?")[2]
            peek = urllib.parse.parse_qs(query).get("peek") == ["1"]
            if path in ("/api/radar/traders", "/api/radar/wallet", "/api/radar/coins/review") and not peek and not self.same_site():     # a GET that starts a job or a lookup
                return self.json(403, {"error": "This request came from another site."})
            status, obj = radar.handle("GET", path[len("/api/radar/"):], query, None, self.data_dir, _save_lock, guest=self._role == "guest")
            return self.json(status, obj)
        if path == "/api/rules":     # the saved rules (the starter ones until something is saved) and the built-in ones
            saved = rules.load(self.settings_dir())
            gone = set(rules.removed(self.settings_dir()))
            built = rules.validate(rules.WATCH_EXTRA)
            added = [r for r in saved if r["id"].startswith("pattern-") and r["id"][8:] in rules.PATTERNS]      # patterns added from the Pattern Discover page are listed in the Lab like the built-in rules
            return self.json(200, {"rules": saved, "builtin": [b for b in built if b["id"] not in gone] + added, "version": version(saved), "limits": rules.LIMITS,
                                   "removed": [{"id": b["id"], "name": b["name"]} for b in built if b["id"] in gone],
                                   "saved_file": os.path.exists(os.path.join(self.settings_dir(), "rules.json")),
                                   "features": {k: {"label": v[0], "kind": v[1]}
                                                for k, v in list(rules.FEATURES.items())}})
        if path == "/api/backtest/schedule":
            return self.json(200, backtest_schedule(self.data_dir))
        if path == "/api/patterns":     # the Pattern Discover page: the file the pattern pass wrote, without the patterns taken off the list since
            try:
                with open(os.path.join(self.data_dir, "patterns.json"), "rb") as f:
                    doc = json.loads(f.read())
            except (OSError, ValueError):
                return self.json(404, {"error": "The pattern pass has not run yet."})
            try:
                with open(os.path.join(self.settings_dir(), "patterns_removed.json"), encoding="utf-8") as f:
                    gone = set(json.load(f))
            except (OSError, ValueError, TypeError):
                gone = set()
            doc["patterns"] = [p for p in doc.get("patterns", []) if p.get("id") not in gone]
            try:
                with open(os.path.join(self.settings_dir(), "lab_patterns.json"), encoding="utf-8") as f:
                    doc["lab"] = sorted(k for k in json.load(f) if isinstance(k, str))      # the patterns that are in the Lab
            except (OSError, ValueError, TypeError):
                doc["lab"] = []
            return self.reply(200, json.dumps(doc).encode(), "application/json; charset=utf-8", {"Cache-Control": "no-store"})
        if path == "/api/lab/draft":    # the answer to the newest "Backtest without saving"
            try:
                with open(os.path.join(self.data_dir, "draft-result.json"), "rb") as f:
                    return self.reply(200, f.read(), "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            except OSError:
                return self.json(404, {"error": "No draft has been backtested yet."})
        if path == "/api/account":      # the browsers signed in (never their tokens)
            cur = self.session()
            with _sessions_lock:
                items = [(t, dict(v)) for t, v in _sessions.items() if v["exp"] > time.time()]
            out = [{"id": hashlib.sha256(t.encode()).hexdigest()[:12], "current": t == cur, "created": int(v["created"] * 1000),
                    "last": int((v["last"] or v["created"]) * 1000), "ip": v.get("ip", ""), "browser": browser_of(v.get("ua", ""))} for t, v in items]
            return self.json(200, {"sessions": sorted(out, key=lambda x: -x["last"])})
        if path == "/api/wallets":      # addresses and labels only: a private key never leaves the settings folder
            return self.json(200, {**wallets.listing(self.settings_dir()), "secure": self.secure()})
        if path == "/api/rpc":          # which RPC the trader uses: its host only, never the path or key
            url, custom = saved_rpc(self.settings_dir())
            host = urllib.parse.urlsplit(url).hostname or ""
            return self.json(200, {"host": host, "custom": custom, "public": host == "api.mainnet-beta.solana.com"})
        if path == "/api/storage":
            return self.json(200, storage_report(self.data_dir))
        if path == "/api/telegram":
            return self.json(200, notify.public(notify.load(self.settings_dir())))
        if path == "/api/recorder":     # the Recorder page: coins being recorded now, the recorded days
            return self.recorder_reply(lambda: recorder.status(self.data_dir))
        if path == "/api/recorder/coins":
            day = urllib.parse.parse_qs(self.path.partition("?")[2]).get("day", [""])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return self.json(400, {"error": "day must look like 2026-10-25"})
            return self.recorder_reply(lambda: {"day": day, "coins": recorder.coins(self.data_dir, day)})
        if path == "/api/recorder/coin":
            mint = urllib.parse.parse_qs(self.path.partition("?")[2]).get("mint", [""])[0]
            if not MINT.fullmatch(mint):
                return self.json(400, {"error": "not a Solana address"})
            return self.recorder_reply(lambda: recorder.coin(self.data_dir, mint))
        if path.startswith("/api/strategy/"):
            # /api/strategy/<id>: its backtest (analyze.py); /api/strategy/<id>/live: its live record (watch.py)
            sid, _, part = path[len("/api/strategy/"):].partition("/")
            if not STRATEGY_ID.fullmatch(sid) or part not in ("", "live"):   # the name goes into a file path
                return self.reply(404, b'{"error":"no such strategy"}\n', "application/json")
            return self.send_file(os.path.join(self.data_dir, "strategies", sid + (".live.json" if part else ".json")),
                                  "application/json; charset=utf-8", None,
                                  b'{"error":"not recorded yet"}\n' if part else b'{"error":"not backtested yet"}\n')
        if path == "/" and self.role() == "guest":    # the page, told it is a guest's: only Lab and Trading, nothing to change
            with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                page = f.read().replace("<head>", "<head><script>window.GUEST = true;</script>", 1)
            return self.reply(200, page.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
        route = ROUTES.get(path)
        if not route:
            return self.reply(404, b"not found\n", "text/plain")
        name, ctype, download = route
        self.send_file(os.path.join(HERE if name in ("index.html", "radar.js", "radar2.js") else self.data_dir, name), ctype, download,
                       b'{"error":"data not collected yet"}\n')

    def recorder_reply(self, make):
        """JSON for a Recorder page request; the databases are opened read-only, so the answer can only be a read error."""
        try:
            out = make()
        except (sqlite3.Error, OSError) as e:
            return self.json(503, {"error": f"The databases could not be read: {e}"})
        if out is None:
            return self.json(404, {"error": "no such coin"})
        return self.json(200, out)

    def send_file(self, full, ctype, download, missing):
        """A file, gzipped when the browser takes it; a 503 with `missing` when it is not written yet. An unchanged
        file (the browser sends back the tag it got) is answered 304 with no body: pages refresh every few seconds."""
        try:
            st = os.stat(full)
        except OSError:
            return self.reply(503, missing, "application/json")
        etag = '"%x-%x-%x"' % file_key(st)
        # (a proxy that re-compresses turns the tag into a weak one, W/"…"; a list of tags may come back)
        sent = [t.strip().removeprefix("W/") for t in self.headers.get("If-None-Match", "").split(",")]
        if etag in sent and not download:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache")
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return
        try:
            body = gzipped(full)
        except OSError:
            return self.reply(503, missing, "application/json")
        headers = {"Content-Encoding": "gzip", "Cache-Control": "private, no-cache", "ETag": etag, "Vary": "Accept-Encoding"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{download}"'
        if "gzip" not in self.headers.get("Accept-Encoding", ""):
            body = gzip.decompress(body)
            del headers["Content-Encoding"]
        self.reply(200, body, ctype, headers)

    do_HEAD = do_GET

    def secure(self):
        """True when the browser reached us over HTTPS (nginx and Cloudflare terminate it and say so)."""
        # only believed from the local proxy: a client reaching port 8090 directly could send the header itself
        return self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"

    def same_site(self):
        """False for a request sent by a page of another site. Browsers say so in Sec-Fetch-Site; the Origin header is
        the fallback, where "null" (our pages send no referrer, so their forms post with Origin: null) proves nothing."""
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site:
            return fetch_site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        return not origin or origin == "null" or urllib.parse.urlsplit(origin).netloc.lower() == self.headers.get("Host", "").lower()

    def settings_dir(self):
        return os.path.join(self.data_dir, "settings")

    def json(self, status, obj):
        self.reply(status, json.dumps(obj).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def do_POST(self):
        """Save the rules or the Telegram settings, or send a Telegram test message.
        Needs a signed-in session (see require_login)."""
        path = self.path.split("?", 1)[0]
        self.data_dir = Handler.data_dir
        if not self.same_site():                 # a page of another site must not post here (signing in included)
            return self.json(403, {"error": "This request came from another site."})
        if path == "/login":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
            except ValueError:
                length = 0
            given = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace")).get("password", [""])[0]
            role = role_for(self.settings_dir(), given)
            if not role:
                time.sleep(1.5)     # slows down guessing
                return self.login_page("Wrong password.")
            token = secrets.token_urlsafe(32)
            with _sessions_lock:
                now = time.time()
                _sessions[token] = {"exp": now + SESSION_DAYS * 86400, "created": now, "last": now, "role": role,
                                    **({"guest": guest_stamp(self.settings_dir())} if role == "guest" else {}),
                                    "ip": self.client_address[0], "ua": self.headers.get("User-Agent", "")[:300]}
                save_sessions(self.settings_dir())
            return self.reply(303, b"", "text/plain", {"Location": "/", "Set-Cookie":
                              f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax{'; Secure' if self.secure() else ''}"})
        if not self.require_login(path, write=True):
            return
        if path.startswith("/api/radar/"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self.json(400 if length <= 0 else 413, {"error": "Empty request." if length <= 0 else "Request too large."})
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return self.json(400, {"error": "Could not read the request."})
            status, obj = radar.handle("POST", path[len("/api/radar/"):], "", body, self.data_dir, _save_lock, guest=self._role == "guest")
            return self.json(status, obj)
        if path not in ("/api/rules", "/api/rules/builtin", "/api/telegram", "/api/telegram/test", "/api/trading", "/api/trading/sell",
                        "/api/password", "/api/sessions/others", "/api/rpc/test", "/api/lab/draft", "/api/patterns/run", "/api/patterns/remove", "/api/patterns/lab",
                        "/api/wallets", "/api/wallets/active", "/api/wallets/remove"):
            return self.reply(404, b"not found\n", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            # take the upload before answering, or the browser sees a dropped connection
            left = min(max(length, 0), 1 << 20)
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
            return self.json(400 if length == 0 else 413, {"error": "Empty request." if length == 0 else "Request too large."})
        raw = self.rfile.read(length)
        if path in ("/api/wallets", "/api/wallets/active", "/api/wallets/remove"):
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, RecursionError):
                return self.json(400, {"error": "Could not read the request."})
            settings = self.settings_dir()
            # the key in use must not change while real positions are open: the trader would lose the key that sells them
            held, state = False, {}
            try:
                with open(os.path.join(self.data_dir, "trading_state.json"), encoding="utf-8") as f:
                    state = json.load(f)
                held = any(t.get("mode") == "live" and t.get("status") in HELD_STATES for t in state.get("trades", []))
            except (OSError, ValueError, AttributeError):
                pass

            def holds(address):     # positions still open with this wallet (or with the key the trader has loaded)
                return any(t.get("mode") == "live" and t.get("status") in HELD_STATES and
                           (t.get("wallet") == address or (not t.get("wallet") and state.get("wallet") == address))
                           for t in state.get("trades", []))
            try:
                with _save_lock:
                    if path == "/api/wallets":
                        if not self.secure():     # a private key must not cross the network unencrypted
                            raise ValueError("Open this site through its https:// address to add a wallet: over plain HTTP the key would travel unencrypted.")
                        activate = bool(body.get("activate"))
                        if activate and held:
                            raise ValueError("Real positions are open, so the wallet in use cannot change now. Add it without switching, and switch when they are closed.")
                        _, address = wallets.parse_key(body.get("key"))
                        wallets.add(settings, body.get("key"), body.get("label"), activate)
                    elif path == "/api/wallets/active":
                        if held:
                            raise ValueError("Real positions are open, so the wallet in use cannot change now. Switch when they are closed.")
                        wallets.activate_wallet(settings, body.get("address"))
                    else:
                        address = body.get("address")
                        if holds(address) or (address == wallets.listing(settings)["active"] and held):
                            raise ValueError("Real positions are open with this wallet: sell them or wait until they close, then delete it.")
                        if wallets.remove(settings, address):
                            # no wallet left to trade with: trading is switched off (the other settings stay)
                            path_ = os.path.join(settings, "trading.json")
                            try:
                                with open(path_, encoding="utf-8") as f:
                                    tcfg = json.load(f)
                            except (OSError, ValueError):
                                tcfg = {}
                            if isinstance(tcfg, dict) and tcfg.get("mode") != "off":
                                tcfg["mode"] = "off"
                                tmp = path_ + ".tmp"
                                with open(tmp, "w", encoding="utf-8") as f:
                                    json.dump(tcfg, f)
                                os.chmod(tmp, 0o644)
                                os.replace(tmp, path_)
                        wallets.queue_purge(settings, address)   # and its trade history goes too
            except (ValueError, OSError) as e:
                return self.json(400, {"error": str(e) if isinstance(e, ValueError) else "The wallet could not be saved on the server."})
            return self.json(200, wallets.listing(settings))
        if path == "/api/patterns/run":
            # a Pattern Discover pass now, besides the one every 2 hours: a systemd path unit watches this file (data/jobs is the one folder the web service may write besides settings)
            jobs = os.path.join(self.data_dir, "jobs")
            os.makedirs(jobs, exist_ok=True)
            with _save_lock:
                tmp = os.path.join(jobs, "patterns_run.json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"asked": int(time.time() * 1000)}, f)
                os.replace(tmp, os.path.join(jobs, "patterns_run.json"))
            return self.json(200, {"asked": True})
        if path == "/api/patterns/lab":
            # adds a watch-list pattern to the Lab as a rule: the pattern goes into settings/lab_patterns.json (a feature "pattern:<id>") and its rule into the saved rules
            try:
                pid = json.loads(raw)["id"]
                if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{10}", pid):
                    raise ValueError
                with open(os.path.join(self.data_dir, "patterns.json"), encoding="utf-8") as f:
                    p = next(x for x in json.load(f)["patterns"] if x.get("id") == pid)
                rule = p["lab_rule"]
            except (ValueError, KeyError, TypeError, RecursionError, StopIteration, OSError):
                return self.json(400, {"error": "That pattern is not on the watch list (run the pattern pass again)."})
            settings = self.settings_dir()
            with _save_lock:
                reg = dict(rules.load_patterns(settings))
                reg[pid] = {"name": p["name"], "spec": p["spec"]}
                rules.save_patterns(settings, reg)
                try:
                    saved = rules.load(settings)
                    if len([r for r in saved if r["id"] != rule["id"]]) + 1 > rules.LIMITS["max_rules"]:
                        raise ValueError("There are too many rules already: delete one in the Lab first.")
                    cleaned = rules.validate([r for r in saved if r["id"] != rule["id"]] + [rule])
                except ValueError as e:
                    return self.json(400, {"error": str(e) or "The rule could not be added."})
                rules.save(settings, cleaned)
            return self.json(200, {"added": rule["id"]})
        if path == "/api/patterns/remove":
            # takes a pattern off the watch list for good: its id goes into settings/patterns_removed.json, which the pattern pass reads
            try:
                pid = json.loads(raw)["id"]
                if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{10}", pid):
                    raise ValueError
            except (ValueError, KeyError, TypeError, RecursionError):
                return self.json(400, {"error": "Say which pattern."})
            p_ = os.path.join(self.settings_dir(), "patterns_removed.json")
            with _save_lock:
                try:
                    with open(p_, encoding="utf-8") as f:
                        gone = json.load(f)
                    gone = gone if isinstance(gone, list) else []
                except (OSError, ValueError):
                    gone = []
                if pid not in gone:
                    gone.append(pid)
                tmp = p_ + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(gone[-500:], f)
                os.replace(tmp, p_)
            return self.json(200, {"removed": pid})
        if path == "/api/lab/draft":
            # "Backtest without saving": the rule is written for the backtest to pick up (a systemd path unit watches the file),
            # exactly like saved rules, and the answer is read back from /api/lab/draft
            try:
                body = json.loads(raw)
                rules.load_patterns(self.settings_dir())
                rule = rules.validate([{**body["rule"], "id": "draft", "enabled": True}])[0]
            except (ValueError, TypeError, KeyError, RecursionError) as e:
                return self.json(400, {"error": str(e) or "Could not read the rule."})
            requested = int(time.time() * 1000)
            path_ = os.path.join(self.settings_dir(), "draft.json")
            with _save_lock:
                tmp = path_ + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"rule": rule, "requested": requested}, f)
                os.chmod(tmp, 0o644)
                os.replace(tmp, path_)
            return self.json(200, {"requested": requested})
        if path == "/api/password":
            try:
                body = json.loads(raw)
                cur, new = body.get("current"), body.get("new")
                if not isinstance(cur, str) or not isinstance(new, str):
                    raise ValueError
            except (ValueError, TypeError, AttributeError, RecursionError):
                return self.json(400, {"error": "Give the current and the new password."})
            if not 8 <= len(new) <= 200:
                return self.json(400, {"error": "The new password must be 8 to 200 characters."})
            if not password_ok(self.settings_dir(), cur):
                time.sleep(1.5)     # slows down guessing
                return self.json(400, {"error": "The current password is wrong."})
            salt = os.urandom(16)
            doc = {"salt": salt.hex(), "hash": hashlib.pbkdf2_hmac("sha256", new.encode(), salt, 200000).hex(), "rounds": 200000}
            path_ = os.path.join(self.settings_dir(), "login.json")
            with _save_lock:
                tmp = path_ + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(doc, f)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path_)
            keep, gone = self.session(), 0      # every other browser has to sign in again
            with _sessions_lock:
                for t in [t for t in _sessions if t != keep]:
                    del _sessions[t]
                    gone += 1
                save_sessions(self.settings_dir())
            return self.json(200, {"ok": True, "signed_out": gone})
        if path == "/api/sessions/others":
            keep, gone = self.session(), 0
            with _sessions_lock:
                for t in [t for t in _sessions if t != keep]:
                    del _sessions[t]
                    gone += 1
                save_sessions(self.settings_dir())
            return self.json(200, {"signed_out": gone})
        if path == "/api/rpc/test":
            try:
                body = json.loads(raw)
                url = body.get("url") if isinstance(body, dict) else None
                if url is not None and not isinstance(url, str):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                return self.json(400, {"error": "Could not read the request."})
            result, error = test_rpc(url.strip() if url and url.strip() else saved_rpc(self.settings_dir())[0])
            return self.json(400 if error else 200, {"error": error} if error else result)
        if path == "/api/trading":
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("expected an object")
                if body.get("mode") not in ("off", "live"):
                    raise ValueError("Mode must be off or live.")
                new = {"mode": body["mode"]}
                if "strategies" in body:   # ids to trade (switched ON on the Trading tab); an empty list trades nothing
                    chosen = body["strategies"]
                    if not isinstance(chosen, list) or len(chosen) > 40 or not all(
                            isinstance(x, str) and 0 < len(x) <= 100 for x in chosen):
                        raise ValueError("Choose between 1 and 40 strategies from the list.")
                    new["strategies"] = list(dict.fromkeys(chosen))
                for k, lo, hi in (("sol_per_trade", 0.01, 1.0), ("max_open", 1, 5), ("daily_loss_limit_sol", 0.01, 10.0), ("max_buy_impact_pct", 1.0, 20.0),
                                  ("slippage_bps", 50, 3000), ("sell_slippage_bps", 50, 5000), ("stop_slippage_bps", 50, 5000),
                                  ("priority_fee_lamports", 0, 5_000_000)):
                    if body.get(k) is None:
                        continue
                    if isinstance(body[k], bool):
                        raise ValueError(f"{k} must be a number")
                    v = float(body.get(k))
                    if not lo <= v <= hi:
                        raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
                    new[k] = v if k in ("sol_per_trade", "daily_loss_limit_sol", "max_buy_impact_pct") else int(v)
                if "trade_hours" in body:     # the time-of-day filter: UTC hours in which new automatic buys are allowed (empty = every hour)
                    th = body["trade_hours"]
                    if not isinstance(th, list) or len(th) > 24 or not all(isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23 for h in th):
                        raise ValueError("Trading hours must be a list of UTC hours from 0 to 23.")
                    new["trade_hours"] = sorted(set(th))
                if body.get("rpc_url"):
                    if not str(body["rpc_url"]).startswith("https://") or len(body["rpc_url"]) > 300:
                        raise ValueError("The RPC address must start with https://")
                    new["rpc_url"] = str(body["rpc_url"])
            except (ValueError, TypeError, RecursionError) as e:
                return self.json(400, {"error": str(e) or "Could not read the settings."})
            path_ = os.path.join(self.settings_dir(), "trading.json")
            with _save_lock:
                try:
                    with open(path_, encoding="utf-8") as f:
                        old = json.load(f)
                except (OSError, ValueError):
                    old = {}
                if not isinstance(old, dict):
                    old = {}
                # switching to live needs LIVE typed; saving other settings while live does not
                if new["mode"] == "live" and old.get("mode") != "live" and body.get("confirm") != "LIVE":
                    return self.json(400, {"error": "Type LIVE in the confirmation box to trade with real SOL."})
                if "strategies" in new:     # a built-in rule deleted from the Lab cannot be switched ON
                    gone_ids = set(rules.removed(self.settings_dir()))
                    new["strategies"] = [x for x in new["strategies"] if x not in gone_ids]
                tmp = path_ + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({**old, **new}, f)
                os.replace(tmp, path_)
            return self.json(200, {"saved": True, **{k: v for k, v in new.items() if k != "rpc_url"}})
        if path == "/api/trading/sell":
            # sell one position ({"id": n}) or all of them ({"all": true}); the settings are not touched,
            # so this works in live mode without typing LIVE again
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError
                one, everything = body.get("id"), body.get("all") is True
                if not everything and not (isinstance(one, int) and not isinstance(one, bool) and 0 < one < 2 ** 62):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                return self.json(400, {"error": "Say which position to sell."})
            path_ = os.path.join(self.settings_dir(), "trading.json")
            now_ms = int(time.time() * 1000)
            with _save_lock:
                try:
                    with open(path_, encoding="utf-8") as f:
                        old = json.load(f)
                except (OSError, ValueError):
                    old = {}
                if not isinstance(old, dict):   # a damaged file: rebuilt around this request
                    old = {}
                if everything:
                    old["sell_all"] = now_ms
                else:   # requests of the last day; the trader acts on each once
                    kept = old.get("sell_ids") if isinstance(old.get("sell_ids"), list) else []
                    asks = [a for a in kept if isinstance(a, list) and len(a) == 2
                            and isinstance(a[1], int) and now_ms - a[1] < 86400000][-50:]
                    old["sell_ids"] = asks + [[one, now_ms]]
                tmp = path_ + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(old, f)
                os.replace(tmp, path_)
            return self.json(200, {"asked": True})
        if path == "/api/telegram/test":
            sent, why = notify.send(notify.load(self.settings_dir()),
                                    "✅ <b>Test message</b>\nThe pump.fun watch can reach you here. "
                                    "You will get a message when a real trade is bought or sold.")
            return self.json(200 if sent else 502, {"sent": sent, "error": why})
        if path == "/api/telegram":
            try:
                with _save_lock:
                    cfg = notify.update(self.settings_dir(), json.loads(raw))
            except (ValueError, RecursionError) as e:
                return self.json(400, {"error": str(e) or "Could not read the settings."})
            return self.json(200, notify.public(cfg))
        if path == "/api/rules/builtin":
            # delete a built-in rule from the Lab ({"id", "removed": true}) or bring it back ({"id", "removed": false})
            try:
                body = json.loads(raw)
                rid, gone = body["id"], body["removed"]
                builtin = {b["id"]: b for b in rules.validate(rules.WATCH_EXTRA)}
                added = {r["id"]: r for r in rules.load(self.settings_dir()) if r["id"].startswith("pattern-") and r["id"][8:] in rules.PATTERNS}     # (also loads the patterns' features)
                if not isinstance(gone, bool) or (rid not in builtin and not (rid in added and gone)):
                    raise ValueError
                builtin.update(added)
            except (ValueError, KeyError, TypeError, RecursionError):
                return self.json(400, {"error": "Say which built-in rule."})
            settings = self.settings_dir()
            with _save_lock:    # (the Trading tab's save takes the same lock, so a strategy cannot be switched ON between the check and the delete)
                if gone:
                    # the trader trades nothing at all when every strategy it was told to trade is missing, so a strategy that
                    # is switched ON, or still holds a real position, is not deleted
                    try:
                        with open(os.path.join(settings, "trading.json"), encoding="utf-8") as f:
                            on = (json.load(f) or {}).get("strategies") or []
                    except (OSError, ValueError, AttributeError):
                        on = []
                    try:
                        with open(os.path.join(self.data_dir, "trading_state.json"), encoding="utf-8") as f:
                            trades = json.load(f).get("trades", [])
                    except (OSError, ValueError, AttributeError):
                        trades = []
                    if rid in on:
                        return self.json(409, {"error": f"{builtin[rid]['name']} is switched ON on the Trading tab. Switch it off there first, then delete it."})
                    if any(t.get("strategy") == rid and t.get("status") in HELD_STATES for t in trades):
                        return self.json(409, {"error": f"{builtin[rid]['name']} still holds a position. Delete it after the position is sold."})
                ids = set(rules.removed(settings))
                if rid in added:        # a pattern added from Pattern Discover is deleted outright (add it again from that page)
                    reg = dict(rules.load_patterns(settings))
                    reg.pop(rid[len("pattern-"):], None)
                    kept = [r for r in rules.load(settings) if r["id"] != rid]
                    rules.save(settings, kept)
                    rules.save_patterns(settings, reg)
                    rules.load_patterns(settings)
                    saved = rules.load(settings)
                    return self.json(200, {"removed": sorted(ids), "rules": saved, "version": version(saved)})
                ids = ids | {rid} if gone else ids - {rid}
                rules.set_removed(settings, ids)
                saved = rules.load(settings)
                kept = [r for r in saved if r["id"] != rid] if gone else saved      # a saved copy of a deleted built-in goes too
                rules.save(settings, kept)      # (always: it starts the backtest, which clears or rebuilds the rule's pages)
                saved = rules.load(settings)
            return self.json(200, {"removed": sorted(ids), "rules": saved, "version": version(saved)})
        try:
            rules.load_patterns(self.settings_dir())      # (a rule may use a pattern added from Pattern Discover)
            cleaned = rules.validate(json.loads(raw))
        except (ValueError, RecursionError) as e:   # includes malformed or absurdly nested JSON
            return self.json(400, {"error": str(e) or "Could not read the rules."})
        with _save_lock:
            # a page opened before someone else saved must not write its older list over theirs
            saved = rules.load(self.settings_dir())
            if self.headers.get("X-Rules-Version", "") != version(saved):
                return self.json(409, {"error": "The rules were changed somewhere else after this page was "
                                                "opened. Reload the page, then make your change again.",
                                       "rules": saved, "version": version(saved)})
            rules.save(self.settings_dir(), cleaned)
        self.json(200, {"rules": cleaned, "version": version(cleaned), "saved_at": int(time.time() * 1000)})

    def reply(self, status, body, ctype, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def version(saved):
    """Changes whenever the saved rules change."""
    return hashlib.sha1(json.dumps(saved, sort_keys=True).encode()).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    args = ap.parse_args()
    Handler.data_dir = args.data
    load_sessions(os.path.join(args.data, "settings"))
    print(f"serving on http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

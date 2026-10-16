#!/usr/bin/env python3
"""Tiny web server for the pump.fun graduated-tokens page.

Serves index.html plus the files the collector writes into the data directory.
Usage: python3 server.py [--port 8090] [--data data]
"""
import argparse
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import notify
import rules

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_DAYS = 30           # a login lasts this long
COOKIE = "pg_session"
_sessions = {}              # token -> expiry (unix seconds); also kept in settings/sessions.json
_sessions_lock = threading.Lock()

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Sign in</title><style>
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--line:#e4e3df;--accent:#2a78d6;--neg:#eb6834}
@media (prefers-color-scheme: dark){:root{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink-2:#c3c2b7;--line:#2e2e2c;--accent:#3987e5;--neg:#d95926}}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
form{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:28px;width:100%;max-width:340px}
h1{font-size:19px;margin:0 0 4px} p{color:var(--ink-2);margin:0 0 18px;font-size:13px}
input,button{font:inherit;width:100%;box-sizing:border-box;border-radius:8px;padding:10px 12px}
input{border:1px solid var(--line);background:var(--page);color:var(--ink);margin-bottom:12px}
button{border:0;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
.err{color:var(--neg);font-size:13px;margin:-4px 0 12px}
</style></head><body><form method="post" action="/login">
<h1>Pump.fun graduated tokens</h1><p>Sign in to continue.</p>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>
%ERROR%<button type="submit">Sign in</button></form></body></html>"""


def _sessions_file(settings):
    return os.path.join(settings, "sessions.json")


def load_sessions(settings):
    try:
        with open(_sessions_file(settings), encoding="utf-8") as f:
            _sessions.update({k: v for k, v in json.load(f).items() if v > time.time()})
    except (OSError, ValueError, AttributeError):
        pass


def save_sessions(settings):
    tmp = _sessions_file(settings) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in _sessions.items() if v > time.time()}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _sessions_file(settings))


def password_ok(settings, given):
    """Checks the password against settings/login.json: {"salt": hex, "hash": hex of PBKDF2-SHA256}."""
    try:
        with open(os.path.join(settings, "login.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        want = bytes.fromhex(cfg["hash"])
        got = hashlib.pbkdf2_hmac("sha256", given.encode(), bytes.fromhex(cfg["salt"]), int(cfg.get("rounds", 200000)))
        return hmac.compare_digest(want, got)
    except (OSError, ValueError, KeyError, TypeError):
        return False
MAX_BODY = 20000   # bytes accepted for a settings save
_save_lock = threading.Lock()

# path -> (file, content type, download name)
ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8", None),
    "/api/tokens": ("latest.json", "application/json; charset=utf-8", None),
    "/api/report": ("report.json", "application/json; charset=utf-8", None),
    "/api/history": ("history.json", "application/json; charset=utf-8", None),
    "/api/watch": ("watch.json", "application/json; charset=utf-8", None),
    "/api/trading": ("trading_state.json", "application/json; charset=utf-8", None),
    "/api/roundtrips.csv": ("roundtrips.csv", "text/csv; charset=utf-8", "pump_rule_round_trips.csv"),
    "/api/study.csv": ("study.csv", "text/csv; charset=utf-8", "pump_entry_study.csv"),
    "/api/tokens.csv": ("latest.csv", "text/csv; charset=utf-8", "pump_graduated_7d.csv"),
}
_gz_cache = {}  # file path -> (mtime, gzipped bytes)
DAY_FILE = re.compile(r"\d{4}-\d{2}-\d{2}\.csv")
MINT = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")   # a Solana address, and nothing else


def gzipped(path):
    mtime = os.stat(path).st_mtime_ns
    hit = _gz_cache.get(path)
    if not hit or hit[0] != mtime:
        with open(path, "rb") as f:
            hit = (mtime, gzip.compress(f.read(), 6))
        _gz_cache[path] = hit
    return hit[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "pumpgrad"
    data_dir = "data"

    def session(self):
        """The session token of a signed-in browser, or None."""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                with _sessions_lock:
                    if _sessions.get(v, 0) > time.time():
                        return v
        return None

    def require_login(self, path):
        """True when the request may go on; otherwise the answer (a redirect or a 401) is sent."""
        if self.session():
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
        if path == "/api/rules":
            saved = rules.load(self.settings_dir())
            return self.json(200, {"rules": saved, "version": version(saved), "limits": rules.LIMITS,
                                   "features": {k: {"label": v[0], "kind": v[1]}
                                                for k, v in rules.FEATURES.items()}})
        if path == "/api/telegram":
            return self.json(200, notify.public(notify.load(self.settings_dir())))
        if path == "/api/reports":
            try:
                with open(os.path.join(self.data_dir, "reports", "index.json"), "rb") as f:
                    return self.reply(200, f.read(), "application/json; charset=utf-8", {"Cache-Control": "no-cache"})
            except OSError:
                return self.json(200, {})
        if path.startswith("/api/reports/"):
            name = path.rsplit("/", 1)[1]
            if not DAY_FILE.fullmatch(name):    # the name goes into a file path
                return self.reply(404, b"not found\n", "text/plain")
            try:
                with open(os.path.join(self.data_dir, "reports", name), "rb") as f:
                    body = f.read()
            except OSError:
                return self.reply(404, b"no report for that day\n", "text/plain")
            headers = {"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-cache"}
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body, headers["Content-Encoding"] = gzip.compress(body, 4), "gzip"
            return self.reply(200, body, "text/csv; charset=utf-8", headers)
        if path.startswith("/api/watch/token/"):
            mint = path.rsplit("/", 1)[1]
            if not MINT.fullmatch(mint):        # the name goes into a file path
                return self.reply(404, b"not found\n", "text/plain")
            try:    # thousands of small files: compressed on the way out, not kept in memory
                with open(os.path.join(self.data_dir, "watch_tokens", mint + ".json"), "rb") as f:
                    body = gzip.compress(f.read(), 4)
            except OSError:
                return self.reply(404, b'{"error":"no such token"}\n', "application/json")
            if "gzip" not in self.headers.get("Accept-Encoding", ""):
                return self.reply(200, gzip.decompress(body), "application/json; charset=utf-8", {"Cache-Control": "no-cache"})
            return self.reply(200, body, "application/json; charset=utf-8",
                              {"Content-Encoding": "gzip", "Cache-Control": "no-cache"})
        route = ROUTES.get(path)
        if not route:
            return self.reply(404, b"not found\n", "text/plain")
        name, ctype, download = route
        full = os.path.join(HERE if name == "index.html" else self.data_dir, name)
        try:
            body = gzipped(full)
        except OSError:
            return self.reply(503, b'{"error":"data not collected yet"}\n', "application/json")
        headers = {"Content-Encoding": "gzip", "Cache-Control": "no-cache"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{download}"'
        if "gzip" not in self.headers.get("Accept-Encoding", ""):
            body = gzip.decompress(body)
            del headers["Content-Encoding"]
        self.reply(200, body, ctype, headers)

    do_HEAD = do_GET

    def settings_dir(self):
        return os.path.join(self.data_dir, "settings")

    def json(self, status, obj):
        self.reply(status, json.dumps(obj).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def do_POST(self):
        """Save the rules or the Telegram settings, or send a Telegram test message.
        Needs a signed-in session (see require_login)."""
        path = self.path.split("?", 1)[0]
        if path == "/login":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
            except ValueError:
                length = 0
            given = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace")).get("password", [""])[0]
            if not password_ok(self.settings_dir(), given):
                time.sleep(1.5)     # slows down guessing
                return self.login_page("Wrong password.")
            token = secrets.token_urlsafe(32)
            with _sessions_lock:
                _sessions[token] = time.time() + SESSION_DAYS * 86400
                save_sessions(self.settings_dir())
            return self.reply(303, b"", "text/plain", {"Location": "/", "Set-Cookie":
                              f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"})
        if not self.require_login(path):
            return
        if path not in ("/api/rules", "/api/telegram", "/api/telegram/test", "/api/telegram/report", "/api/trading", "/api/trading/sell"):
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
        if path == "/api/trading":
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("expected an object")
                if body.get("mode") not in ("off", "live"):
                    raise ValueError("Mode must be off or live.")
                new = {"mode": body["mode"]}
                if "strategies" in body:   # ids to trade; an empty list follows the rules ticked "Record live"
                    chosen = body["strategies"]
                    if not isinstance(chosen, list) or len(chosen) > 40 or not all(
                            isinstance(x, str) and 0 < len(x) <= 100 for x in chosen):
                        raise ValueError("Choose between 1 and 40 strategies from the list, or follow Record live.")
                    new["strategies"] = list(dict.fromkeys(chosen))
                for k, lo, hi in (("sol_per_trade", 0.01, 1.0), ("max_open", 1, 5), ("daily_loss_limit_sol", 0.01, 10.0),
                                  ("slippage_bps", 50, 3000), ("sell_slippage_bps", 50, 5000), ("stop_slippage_bps", 50, 5000),
                                  ("priority_fee_lamports", 0, 5_000_000)):
                    if body.get(k) is None:
                        continue
                    if isinstance(body[k], bool):
                        raise ValueError(f"{k} must be a number")
                    v = float(body.get(k))
                    if not lo <= v <= hi:
                        raise ValueError(f"{k} must be between {lo:g} and {hi:g}")
                    new[k] = v if k in ("sol_per_trade", "daily_loss_limit_sol") else int(v)
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
        if path == "/api/telegram/report":
            folder = os.path.join(self.data_dir, "reports")
            days = sorted(f[:-12] for f in os.listdir(folder) if f.endswith("-summary.txt")) if os.path.isdir(folder) else []
            if not days:
                return self.json(404, {"sent": False, "error": "No daily report has been written yet; the first one "
                                                               "appears a little after midnight UTC."})
            cfg = notify.load(self.settings_dir())
            with open(os.path.join(folder, days[-1] + "-summary.txt"), encoding="utf-8") as f:
                sent, why = notify.send(cfg, f.read())
            if sent and os.path.exists(os.path.join(folder, days[-1] + "-shown.csv")):
                with open(os.path.join(folder, days[-1] + "-shown.csv"), "rb") as f:
                    notify.send_file(cfg, f"{days[-1]}-strategy-decisions.csv", f.read(), f"Decisions of {days[-1]} (UTC)")
            return self.json(200 if sent else 502, {"sent": sent, "error": why, "day": days[-1]})
        if path == "/api/telegram/test":
            sent, why = notify.send(notify.load(self.settings_dir()),
                                    "✅ <b>Test message</b>\nThe pump.fun watch can reach you here. "
                                    "You will get a message when a strategy shown on the Record live tab buys a token.")
            return self.json(200 if sent else 502, {"sent": sent, "error": why})
        if path == "/api/telegram":
            try:
                with _save_lock:
                    cfg = notify.update(self.settings_dir(), json.loads(raw))
            except (ValueError, RecursionError) as e:
                return self.json(400, {"error": str(e) or "Could not read the settings."})
            return self.json(200, notify.public(cfg))
        try:
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

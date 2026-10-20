"""Solana RPC access for the Coins, Wallets and Monitor pages (Rug Radar's src/wallet/rpc.js, cut down).

These pages read many transactions, so they use their own pool of RPC keys, saved on the Settings page into
settings/rpc_pool.json (see below); the trader's RPC is never shared with them. Without keys the free public RPC
is used, which is very slow. Every transaction read is kept in data/tx_cache/<signature>.json (a confirmed transaction
never changes), so repeat analyses are fast. The address can hold a key: it is never put in an error message.
"""
import json
import math
import os
import random
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "pumpgrad/1.0", "Content-Type": "application/json"}
CACHE_DIR = "tx_cache"
CACHE_MAX_AGE = 24 * 3600
CACHE_MAX_BYTES = 1 << 30


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class RPCError(Exception):
    """An RPC call that failed. The message never contains the RPC address."""


def settings_dir(data):
    return os.path.join(data, "settings")


# ---- the RPC keys ------------------------------------------------------------------------------------------------------
# settings/rpc_pool.json: {"mode": "failover" | "roundrobin", "endpoints": [{"id", "label", "url", "enabled"}]}, mode 600 (the
# addresses hold the keys). Like Rug Radar's pool: a key that is rate-limited or unavailable rests and the next one takes over, and
# a key that is refused (401/403) rests for 10 minutes. Failover uses the first key that is ready and under its learned limit;
# round-robin spreads the load. The older single address (analysis_rpc.json) still works when the pool is empty.
ALCHEMY = "https://solana-mainnet.g.alchemy.com/v2/"
MODES = ("failover", "roundrobin")
MAX_KEYS = 20
REST = {"rate-limited": (1.5, 60.0), "unavailable": (5.0, 60.0), "rejected": (600.0, 600.0)}
MAX_WAIT = 120.0
_pool_lock = threading.Lock()
_state = {}      # endpoint id -> {"until", "strikes", "reason", "used": [(time, cost)], "inflight", "cap", "cap_at", "limited_at"}
_rr = [0]


def pool_path(data):
    return os.path.join(settings_dir(data), "rpc_pool.json")


def load_pool(data):
    """(mode, endpoints): the saved keys, or the older single address, or the public RPC as the only (marked) entry."""
    doc = {}
    try:
        with open(pool_path(data), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        pass
    eps = [e for e in (doc.get("endpoints") or []) if isinstance(e, dict) and str(e.get("url", "")).startswith("https://") and e.get("id")]
    mode = doc.get("mode") if doc.get("mode") in MODES else "failover"
    if not eps:
        try:
            with open(os.path.join(settings_dir(data), "analysis_rpc.json"), encoding="utf-8") as f:
                url = json.load(f).get("url")
            if isinstance(url, str) and url.startswith("https://"):
                eps = [{"id": "single", "label": "Saved address", "url": url, "enabled": True}]
        except (OSError, ValueError, AttributeError):
            pass
    return mode, eps


def active_pool(data):
    """The usable endpoints in priority order and whether they are the user's own keys."""
    _, eps = load_pool(data)
    use = [e for e in eps if e.get("enabled", True)]
    if use:
        return use, True
    return [{"id": "public", "label": "Public Solana RPC", "url": PUBLIC_RPC, "enabled": True}], False


def rpc_url(data):
    """(address, custom): the first usable endpoint (kept for callers that only need to know whether a key is set)."""
    use, custom = active_pool(data)
    return use[0]["url"], custom


def normalize_key(text):
    """A bare Alchemy key or a full https RPC address -> the address."""
    s = str(text or "").strip()
    if s.lower().startswith("https://"):
        return s
    if re.fullmatch(r"[A-Za-z0-9_-]{16,100}", s):
        return ALCHEMY + s
    raise ValueError("Paste an Alchemy API key (letters, numbers, - and _) or a full RPC address starting with https://")


def mask(url):
    """Provider host and a masked key, never the whole address."""
    p = urllib.parse.urlsplit(url)
    secret = "?".join(x for x in ((p.path.rstrip("/").split("/")[-1] if p.path.strip("/") else ""), p.query) if x)
    shown = secret[:6] + "…" + secret[-4:] if len(secret) > 10 else ("…" + secret[-2:] if secret else "")
    return (p.hostname or "") + (" · " + shown if shown else "")


def save_pool(data, mode, endpoints):
    os.makedirs(settings_dir(data), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=settings_dir(data), prefix="rpc_pool.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mode": mode, "endpoints": endpoints}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, pool_path(data))


def clear_rpc_url(data):
    for n in ("analysis_rpc.json",):
        try:
            os.remove(os.path.join(settings_dir(data), n))
        except OSError:
            pass


def _st(eid):
    if eid not in _state:
        _state[eid] = {"until": 0.0, "strikes": 0, "reason": None, "used": [], "inflight": 0, "cap": math.inf, "cap_at": 0.0, "limited_at": 0.0}
    return _state[eid]


def _load_of(s, now, at=None):
    at = now if at is None else at
    while s["used"] and s["used"][0][0] <= now - 10:
        s["used"].pop(0)
    return sum(c for t, c in s["used"] if at - 1 < t <= at)


def _cap_of(s, now):
    if s["cap"] != math.inf and now - max(s["cap_at"], s["limited_at"]) >= 30:
        s["cap"] *= 1.1
        s["cap_at"] = now
        if s["cap"] > 500:
            s["cap"] = math.inf
    return s["cap"]


def _rest(ep, reason, hint=None, sent_at=None):
    s, now = _st(ep["id"]), time.time()
    already = s["until"] > now and s["reason"] == reason
    if not already:
        s["strikes"] += 1
    base, mx = REST[reason]
    s["reason"] = reason
    s["until"] = max(s["until"], now + max(hint or 0, min(mx, base * 2 ** (s["strikes"] - 1))))
    if reason == "rate-limited":
        load = _load_of(s, now, sent_at or now)
        if not already and load >= 3:
            s["cap"] = max(2.0, min(_cap_of(s, now), load) * 0.8)
            s["cap_at"] = now
        s["limited_at"] = now
        with _rate_lock:
            _rate["limited_at"], _rate["n"] = now, _rate["n"] + 1


def _pick(data):
    """The endpoint for the next request (counted on its key before any other thread can choose)."""
    started = time.time()
    while True:
        mode, _ = load_pool(data)
        use, custom = active_pool(data)
        with _pool_lock:
            now = time.time()
            ready = [e for e in use if _st(e["id"])["until"] <= now]
            if ready:
                room = [e for e in ready if _load_of(_st(e["id"]), now) == 0 or _load_of(_st(e["id"]), now) + 1 <= _cap_of(_st(e["id"]), now)]
                if not room and now - started < 5.0:
                    pass            # every ready key is at its limit: wait a moment for room (history downloads can wait)
                else:
                    frm = room or ready
                    best = frm[0]
                    if mode == "roundrobin" and len(frm) > 1:
                        start, best_load = _rr[0], math.inf
                        _rr[0] += 1
                        for i in range(len(frm)):
                            e = frm[(start + i) % len(frm)]
                            ld = _load_of(_st(e["id"]), now) + _st(e["id"])["inflight"]
                            if ld < best_load:
                                best, best_load = e, ld
                    s = _st(best["id"])
                    s["used"].append((now, 1))
                    s["inflight"] += 1
                    return best, now
            else:
                waitable = [e for e in use if _st(e["id"])["reason"] != "rejected"]
                if not waitable:
                    raise RPCError("Every RPC key was refused by its provider. Check the keys on the Settings page (they may be wrong or revoked).")
                wake = min(_st(e["id"])["until"] for e in waitable)
                if wake - started > MAX_WAIT:
                    raise RPCError("Every RPC key is resting after rate limits. Add more keys on the Settings page or try again in a minute.")
        time.sleep(0.1 if ready else min(2.0, max(0.1, wake - time.time() + 0.05)))


_rate = {"limited_at": 0.0, "n": 0}     # the last time a 429 was seen (the monitor slows down after one)
_rate_lock = threading.Lock()


def rate_limited_since(t):
    """How many keys were rate-limited since t."""
    n = sum(1 for s in _state.values() if s["limited_at"] >= t)
    return n or (_rate["n"] if _rate["limited_at"] >= t else 0)


def _clean(msg, urls):
    msg = str(msg)
    for u in urls:
        msg = msg.replace(u, "[rpc]")
    return re.sub(r"https?://\S+", "[url]", msg)


def call(data, method, params, tries=6, timeout=30, url=None):
    """One JSON-RPC call through the key pool (or through `url` alone). A rate limit or an outage makes the next key take over; an
    RPC `error` answer is raised. The message never contains an address."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    urls = [e["url"] for e in load_pool(data)[1]] if data else []
    last = "no answer"
    for attempt in range(tries):
        if url:
            ep, sent = {"id": "adhoc", "label": "RPC", "url": url}, time.time()
            urls = [url]
        else:
            ep, sent = _pick(data)
        s = _st(ep["id"])
        try:
            try:
                with _opener.open(urllib.request.Request(ep["url"], data=body, headers=UA), timeout=timeout) as r:
                    out = json.loads(r.read(32 << 20))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    hint = float(ra) if ra and re.fullmatch(r"\d+(\.\d+)?", ra.strip()) else None
                    _rest(ep, "rate-limited", min(hint, 300) if hint else None, sent)
                    last = "an RPC key is rate-limited (HTTP 429)"
                    if url:
                        time.sleep(min(8.0, 0.6 * 2 ** attempt) + random.random() * 0.3)
                    continue
                if e.code in (401, 403):
                    _rest(ep, "rejected")
                    last = "an RPC key was refused (HTTP %d)" % e.code
                    if url:
                        raise RPCError("The RPC refused the key (HTTP %d)." % e.code)
                    continue
                _rest(ep, "unavailable")
                last = "the RPC answered HTTP %d" % e.code
                if url:
                    time.sleep(0.5 * (attempt + 1))
                continue
            except (urllib.error.URLError, OSError, ValueError) as e:
                _rest(ep, "unavailable")
                last = "the RPC could not be reached (%s)" % type(e).__name__
                if url:
                    time.sleep(0.5 * (attempt + 1))
                continue
            if isinstance(out, dict) and out.get("error"):
                err = out["error"]
                msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                if re.search(r"rate.?limit|too many requests|exceeded|capacity|compute units", msg, re.I) or (isinstance(err, dict) and err.get("code") in (429, -32005)):
                    _rest(ep, "rate-limited", None, sent)
                    last = "an RPC key is rate-limited"
                    continue
                raise RPCError(_clean(msg, urls)[:300])
            s["strikes"] = 0
            return out.get("result") if isinstance(out, dict) else None
        finally:
            with _pool_lock:
                s["inflight"] = max(0, s["inflight"] - 1) if not url else 0
    raise RPCError(last)


def test(url):
    """Newest slot and the time it took: (result, error). The address must be https and resolve to public addresses only,
    because this server makes the request."""
    import ipaddress
    import socket
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or len(url) > 300:
            return None, "The RPC address must start with https://"
        if (parts.port or 443) not in (443, 8443):
            return None, "The RPC address must use port 443."
        for info in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return None, "That address points at a private network."
    except (ValueError, OSError):
        return None, "That address could not be looked up."
    t0 = time.time()
    try:
        slot = call(None, "getSlot", [], tries=1, timeout=8, url=url)
    except RPCError as e:
        return None, str(e)
    if not isinstance(slot, int):
        return None, "It answered, but not with a slot number."
    return {"ms": round((time.time() - t0) * 1000), "slot": slot}, None


# ---- transactions, cached on disk -------------------------------------------------------------------------------------

def _cache_path(data, sig):
    return os.path.join(data, CACHE_DIR, sig + ".json")


SIG = __import__("re").compile(r"[1-9A-HJ-NP-Za-km-z]{64,90}")


def get_transaction(data, sig, commitment="finalized", use_cache=True):
    """One parsed transaction, or None when the RPC does not have it (yet)."""
    if not SIG.fullmatch(str(sig)):
        return None
    if use_cache:
        try:
            with open(_cache_path(data, sig), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    tx = call(data, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": commitment}])
    if tx and commitment == "finalized":
        try:
            os.makedirs(os.path.join(data, CACHE_DIR), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.join(data, CACHE_DIR), prefix=".w", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tx, f)
            os.replace(tmp, _cache_path(data, sig))
        except OSError:
            pass
    return tx


def get_transactions(data, sigs, workers=4, progress=None, cancel=None):
    """{signature: transaction} for those the RPC has (finalized), a few at a time. `cancel` is a threading.Event."""
    out, todo = {}, []
    for s in sigs:
        try:
            with open(_cache_path(data, s), encoding="utf-8") as f:
                out[s] = json.load(f)
        except (OSError, ValueError):
            todo.append(s)
    done = [len(out)]

    def one(s):
        if cancel is not None and cancel.is_set():
            return s, None
        try:
            tx = get_transaction(data, s, use_cache=False)
        except RPCError:
            tx = None
        done[0] += 1
        if progress:
            progress(done[0], len(sigs))
        return s, tx
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for s, tx in ex.map(one, todo):
                if tx:
                    out[s] = tx
    return out


def signatures(data, address, before=None, until=None, limit=1000, commitment="confirmed"):
    """One page of signatures for an address, newest first."""
    opts = {"limit": limit, "commitment": commitment}
    if before:
        opts["before"] = before
    if until:
        opts["until"] = until
    return [x for x in (call(data, "getSignaturesForAddress", [address, opts]) or []) if isinstance(x, dict) and SIG.fullmatch(str(x.get("signature", "")))]


def prune_cache(data):
    """Delete cached transactions older than a day, and the oldest ones while the cache is above its size cap."""
    folder = os.path.join(data, CACHE_DIR)
    try:
        files = []
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    except OSError:
        return
    now, total = time.time(), sum(f[1] for f in files)
    for mtime, size, p in sorted(files):
        if now - mtime > CACHE_MAX_AGE or total > CACHE_MAX_BYTES:
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass


# ---- the SOL price -----------------------------------------------------------------------------------------------------

_sol = {"at": 0.0, "v": 0.0}


def sol_usd_now():
    """The current SOL price in USD (cached a minute); 0 when no source answers (callers must treat 0 as unknown)."""
    if _sol["v"] and time.time() - _sol["at"] < 60:
        return _sol["v"]
    for url, pick in (("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", lambda j: float(j["price"])),
                      ("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", lambda j: float(j["solana"]["usd"]))):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pumpgrad/1.0"}), timeout=8) as r:
                v = pick(json.loads(r.read()))
            if v > 0:
                _sol["at"], _sol["v"] = time.time(), v
                return v
        except Exception:
            continue
    return _sol["v"] if time.time() - _sol["at"] < 3600 else 0.0


def sol_usd_history(start_ts):
    """[(unix hour, close)] hourly SOL closes from start_ts on (Binance klines), oldest first; [] when unavailable."""
    out, t = [], int(start_ts // 3600 * 3600) * 1000
    for _ in range(12):
        url = f"https://api.binance.com/api/v3/klines?symbol=SOLUSDT&interval=1h&startTime={t}&limit=1000"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pumpgrad/1.0"}), timeout=15) as r:
                rows = json.loads(r.read())
        except Exception:
            break
        if not rows:
            break
        out += [(int(k[0]) // 1000, float(k[4])) for k in rows]
        if len(rows) < 1000:
            break
        t = int(rows[-1][0]) + 3600_000
    return out


# ---- hourly SOL prices for a window (reviews value every trade at its own hour's price) ---------------------------------

_hours = {}       # unix hour -> close
_hours_lock = threading.Lock()


def sol_prices(since):
    """(at, now, historical): at(t) is the SOL/USD close of the hour containing t, now the newest price. Without a usable
    history every trade is valued at today's price (historical False). Raises RPCError when no price can be had at all."""
    import bisect
    start = int(since // 3600 * 3600)
    now_h = int(time.time() // 3600 * 3600)
    with _hours_lock:
        missing = any(h not in _hours for h in range(start, now_h, 3600))
    if missing:
        for host, sym in (("https://api.binance.com", "SOLUSDT"), ("https://api.binance.us", "SOLUSD")):
            try:
                t = start * 1000
                for _ in range(40):
                    with urllib.request.urlopen(urllib.request.Request(f"{host}/api/v3/klines?symbol={sym}&interval=1h&startTime={t}&limit=1000", headers={"User-Agent": "pumpgrad/1.0"}), timeout=20) as r:
                        rows = json.loads(r.read())
                    with _hours_lock:
                        for k in rows:
                            _hours[int(k[0]) // 1000] = float(k[4])
                    if len(rows) < 1000:
                        break
                    t = int(rows[-1][0]) + 3600_000
                break
            except Exception:
                continue
    with _hours_lock:
        hs = sorted(_hours)
        covered = bool(hs) and hs[0] <= start + 3600 and hs[-1] >= now_h - 6 * 3600
        if covered:
            snap = {h: _hours[h] for h in hs}
    if not covered:
        price = sol_usd_now()
        if not price:
            raise RPCError("Couldn't load SOL prices from Binance or DexScreener. Try again in a minute.")
        return (lambda t: price), price, False

    def at(t):
        i = bisect.bisect_right(hs, t or 0) - 1
        return snap[hs[max(i, 0)]]
    return at, snap[hs[-1]], True


def get_signatures_all(data, address, since=0, max_n=20000, cancel=None, progress=None):
    """All successful signatures of an address newer than `since` (unix seconds), newest first: (signatures, truncated, oldest)."""
    out, before, truncated = [], None, False
    while True:
        if cancel is not None and cancel.is_set():
            break
        opts = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        page = [x for x in (call(data, "getSignaturesForAddress", [address, opts], tries=6) or []) if isinstance(x, dict) and SIG.fullmatch(str(x.get("signature", "")))]
        if not page:
            break
        out += [x for x in page if (x.get("blockTime") or 0) >= since and not x.get("err")]
        before = page[-1]["signature"]
        if progress:
            progress(len(out))
        if len(page) < 1000 or (page[-1].get("blockTime") or 0) < since:
            break
        if len(out) >= max_n:
            truncated = True
            break
    if len(out) > max_n:
        truncated = True
    kept = out[:max_n]
    return [x["signature"] for x in kept], truncated, (kept[-1].get("blockTime") if kept else since)


def get_transactions_stream(data, sigs, on_tx, workers=4, progress=None, cancel=None):
    """Fetch (cache first) and hand each transaction to on_tx(sig, tx) as it arrives, keeping none: -> failed count."""
    failed = [0]
    done = [0]
    lock = threading.Lock()

    def one(s):
        if cancel is not None and cancel.is_set():
            return
        try:
            tx = get_transaction(data, s)
        except RPCError:
            tx = None
        with lock:
            done[0] += 1
            if tx is None or not tx.get("blockTime"):
                failed[0] += 1
            else:
                try:
                    on_tx(s, tx)
                except Exception:
                    failed[0] += 1
            if progress:
                progress(done[0], len(sigs))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, sigs))
    return failed[0]

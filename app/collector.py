#!/usr/bin/env python3
"""Record every pump.fun token that graduated (bonding curve -> PumpSwap) in the last N days.

Everything goes into a SQLite database (data/pump.db) that grows run after run:
  tokens  : one row per graduated coin, with its graduation time
  candles : 1-minute bars from creation until ~5 hours after graduation

Graduation time comes from the candle API's own graduation marker (a Solana slot) while
pump.fun still serves it (the first hours of a coin's life). For older coins it is the
first minute the price reaches the bonding curve's completion price.

Sources (public APIs, no key needed):
  - pump.fun /coins/search-unrestricted : lists graduated pump coins, paged by coin age
  - pump.fun /coins                     : newest graduated coins, used as a cross-check
  - pump.fun /candles/{chain}/{mint}    : 1m bars and the graduation marker
  - Solana RPC getBlockTime             : turns the graduation slot into a time

Usage: python3 collector.py [--days 7] [--out data]
"""
import argparse
import bisect
import csv
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

API = "https://frontend-api-v3.pump.fun"
CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": "https://pump.fun",
    "Accept": "application/json",
}
PAGE = 200          # search-unrestricted max rows per page
DAY = 86400
# requests per second; /candles advertises 600/min, the coin lists sit behind a
# Cloudflare rule that blocks for 3 minutes after a burst of ~30 requests
RATES = {"/candles": 9.0, "/coins": 0.4}
BATCH = 200         # coins fetched between writes, so a run cut short still keeps its bars
_next_slot = {k: 0.0 for k in RATES}
_lock = threading.Lock()


def throttle(path):
    key = "/candles" if path.startswith("/candles") else "/coins"
    with _lock:
        slot = max(_next_slot[key], time.monotonic())
        _next_slot[key] = slot + 1 / RATES[key]
    time.sleep(max(slot - time.monotonic(), 0))


def get(path, params=None, retries=8):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(retries):
        throttle(path)
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After") or 10))
        except Exception as e:  # timeouts, resets, bad json
            err = e
        time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"GET {url} failed: {err}")


def search_from(min_age, max_age, extra):
    """Graduated pump coins aged min_age..max_age seconds, newest first.

    Deep offsets are clamped by the API, so page with the age of the last row instead.
    """
    out = []
    while min_age < max_age:
        rows = get("/coins/search-unrestricted", {
            "complete": "true", "includeNsfw": "true", "program": "pump",
            "pumpSwapGraduatedOnly": "true", "sort": "created_timestamp", "order": "DESC",
            "limit": PAGE, "minAgeSeconds": min_age, **extra,
        }) or []
        out += rows
        print(f"  listed {len(out)} coins (age {min_age / DAY:.2f}d)", flush=True)
        if len(rows) < PAGE:
            break
        oldest = min(r["created_timestamp"] for r in rows)
        next_age = int(time.time() - oldest / 1000) - 5  # small overlap, deduped by mint
        if next_age <= min_age:
            next_age = min_age + 1
        min_age = next_age
    return out


def list_candidates(days):
    """Graduated coins created inside the window."""
    return {c["mint"]: c for c in search_from(0, days * DAY, {})}


def cross_check(coins):
    """The plain /coins list is a different backend; add fresh coins search has not indexed."""
    added = 0
    for offset in range(0, 350, 50):
        rows = get("/coins", {"offset": offset, "limit": 50, "sort": "created_timestamp",
                              "order": "DESC", "includeNsfw": "true", "complete": "true"}) or []
        for c in rows:
            if c.get("program") == "pump" and c.get("pump_swap_pool") and c["mint"] not in coins:
                coins[c["mint"]] = c
                added += 1
        if not rows:
            break
    return added


HORIZON_MS = 295 * 60000   # 1m bars kept after graduation (the API returns 300 per call)
GIVE_UP_MS = 12 * 3600000  # stop looking for a graduation time on coins older than this
RPC = "https://api.mainnet-beta.solana.com"
# a classic curve completes at 115 virtual SOL against 279.9M tokens; the first bar of a coin
# whose creator bought the whole curve opens at that price, which anchors it in USD over time
ANCHOR_MIN, ANCHOR_MAX = 3.0e-5, 7.0e-5

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  mint TEXT PRIMARY KEY,
  name TEXT, symbol TEXT, creator TEXT, pump_swap_pool TEXT,
  created_ts INTEGER,            -- unix ms
  graduated_ts INTEGER,          -- unix ms; NULL until known
  graduated_src TEXT,            -- 'slot' (pump.fun's marker) or 'price' (completion price reached)
  graduated_slot INTEGER,        -- Solana slot of the graduation, when pump.fun served it
  classic INTEGER,               -- 1 = standard 85 SOL bonding curve
  first_bar_ts INTEGER,          -- first 1m bar we hold (about the creation minute)
  bars_to_ts INTEGER,            -- bars are stored up to this time
  candles_done INTEGER NOT NULL DEFAULT 0,       -- nothing left to fetch for this coin
  usd_market_cap REAL, ath_market_cap REAL, ath_ts INTEGER, last_trade_ts INTEGER,
  reply_count INTEGER, first_seen_ts INTEGER, updated_ts INTEGER,
  raw TEXT                       -- full coin JSON as last returned by pump.fun
);
CREATE INDEX IF NOT EXISTS tokens_graduated ON tokens(graduated_ts);
CREATE TABLE IF NOT EXISTS candles (
  mint TEXT NOT NULL,
  t INTEGER NOT NULL,            -- bar open time, unix ms
  open REAL, high REAL, low REAL, close REAL,   -- USD per token
  volume_usd REAL, trades INTEGER,
  PRIMARY KEY (mint, t)
) WITHOUT ROWID;
"""


def is_classic(coin):
    return int(abs((coin.get("virtual_sol_reserves") or 0) - 115e9) < 1e9
               or coin.get("boost_mode") == "COMPLETED")


def migrate(db):
    """Bring a database from the first collector version up to the current schema."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(tokens)")}
    if "first_bar_ts" in cols:
        return
    print("migrating database: graduation times are re-derived", flush=True)
    for col in ("graduated_src TEXT", "graduated_slot INTEGER", "classic INTEGER",
                "first_bar_ts INTEGER", "bars_to_ts INTEGER"):
        db.execute(f"ALTER TABLE tokens ADD COLUMN {col}")
    # coins that only got in because they still traded days later would bias any analysis
    db.execute("DELETE FROM candles WHERE mint IN "
               "(SELECT mint FROM tokens WHERE created_ts < first_seen_ts - 7 * 86400000)")
    db.execute("DELETE FROM tokens WHERE created_ts < first_seen_ts - 7 * 86400000")
    # the old graduated_ts was really the first bar, i.e. about the creation time
    # bars were fetched up to creation + horizon, or up to that moment for younger coins
    db.execute("UPDATE tokens SET first_bar_ts = graduated_ts, graduated_ts = NULL, candles_done = 0, "
               "bars_to_ts = CASE WHEN graduated_ts IS NOT NULL THEN MIN(created_ts + ?, updated_ts) END",
               (HORIZON_MS,))
    rows = [(is_classic(json.loads(raw)), mint) for mint, raw in db.execute("SELECT mint, raw FROM tokens")]
    db.executemany("UPDATE tokens SET classic = ? WHERE mint = ?", rows)
    db.commit()
    db.execute("VACUUM")


def fetch_bars(job):
    """job = (mint, pool, to_ms). Returns (bars, graduation slot or None, to_ms), or None on failure."""
    mint, pool, to_ms = job
    try:
        params = {"res": "1m", "count": 300, "to": to_ms}
        if pool:
            params["pool"] = pool
        r = get(f"/candles/{CHAIN}/{mint}", params) or {}
    except RuntimeError as e:   # one bad coin must not stop the run; it is retried next time
        print(f"  ! {mint}: {e}", flush=True)
        return None
    marker = r.get("graduatedAt")
    # slot 0 is pump.fun's placeholder for "not known", not a real graduation
    return r.get("candles") or [], (int(marker.split("-")[0]) or None) if marker else None, to_ms


def fetch_pass(db, ex, now_ms):
    """Fetch bars for every coin that can still gain some. Returns how many were fetched."""
    todo = []
    for mint, pool, created, grad, bars_to in db.execute(
            "SELECT mint, pump_swap_pool, created_ts, graduated_ts, bars_to_ts FROM tokens "
            "WHERE candles_done = 0 ORDER BY created_ts DESC"):
        # a future `to` is rejected by the API
        target = min((grad or created) + HORIZON_MS, now_ms)
        if bars_to is None or bars_to < target - 120000:
            todo.append((mint, pool, target))
    print(f"coins needing candles: {len(todo)}", flush=True)
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        # fetch first, then write in one short transaction so other writers get a turn
        results = list(ex.map(fetch_bars, batch))
        for (mint, _, _), res in zip(batch, results):
            if res is None:
                continue
            bars, slot, to_ms = res
            db.execute(
                "UPDATE tokens SET bars_to_ts = ?, graduated_slot = COALESCE(?, graduated_slot), "
                "first_bar_ts = COALESCE(first_bar_ts, ?) WHERE mint = ?",
                (to_ms, slot, bars[0]["openTimeMs"] if bars else None, mint))
            db.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                [(mint, b["openTimeMs"], float(b["open"]), float(b["high"]), float(b["low"]),
                  float(b["close"]), float(b["volumeUsd"]), b["trades"]) for b in bars])
        db.commit()
        print(f"  candles: {min(i + BATCH, len(todo))}/{len(todo)}", flush=True)
    return len(todo)


def resolve_slots(db):
    """Turn graduation slots into times with the Solana RPC."""
    todo = [r for r in db.execute(
        "SELECT mint, graduated_slot FROM tokens WHERE graduated_slot IS NOT NULL "
        "AND (graduated_src IS NULL OR graduated_src != 'slot')")]
    done = 0
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        body = json.dumps([{"jsonrpc": "2.0", "id": j, "method": "getBlockTime", "params": [slot]}
                           for j, (_, slot) in enumerate(batch)]).encode()
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                answers = {a["id"]: a.get("result") for a in json.load(r)}
        except Exception as e:   # the price fallback below still covers classic coins
            print(f"  ! Solana RPC: {e}", flush=True)
            break
        for j, (mint, _) in enumerate(batch):
            # a marker that lands before the coin existed is not a graduation; drop it
            cur = db.execute(
                "UPDATE tokens SET graduated_ts = ?, graduated_src = 'slot' WHERE mint = ? "
                "AND ? >= created_ts - 60000", (answers.get(j, 0) * 1000, mint, answers.get(j, 0) * 1000)
            ) if answers.get(j) else None
            if cur is not None and cur.rowcount:
                done += 1
            else:
                db.execute("UPDATE tokens SET graduated_slot = NULL WHERE mint = ?", (mint,))
        db.commit()
        time.sleep(1.2)
    return done


def completion_price(db):
    """Function t -> USD price at which a classic bonding curve completes, or None."""
    pts = db.execute(
        "SELECT k.first_bar_ts, c.open FROM tokens k JOIN candles c "
        "ON c.mint = k.mint AND c.t = k.first_bar_ts "
        "WHERE k.classic = 1 AND c.open BETWEEN ? AND ? ORDER BY 1", (ANCHOR_MIN, ANCHOR_MAX)).fetchall()
    if len(pts) < 20:
        return None
    ts = [p[0] for p in pts]
    # a median over neighbours ignores the odd coin whose first bar is not the full-curve buy
    px = [sorted(p[1] for p in pts[max(0, i - 7):i + 8])[len(pts[max(0, i - 7):i + 8]) // 2]
          for i in range(len(pts))]

    def at(t):
        i = min(max(bisect.bisect_left(ts, t), 0), len(ts) - 1)
        return px[i]
    return at


def derive_by_price(db):
    """Classic coins without a marker graduate the first minute the completion price trades."""
    price_at = completion_price(db)
    if price_at is None:
        print("  not enough full-curve launches yet to anchor the completion price", flush=True)
        return 0
    mints = [r[0] for r in db.execute(
        "SELECT mint FROM tokens WHERE classic = 1 AND first_bar_ts IS NOT NULL "
        "AND (graduated_ts IS NULL OR graduated_src = 'price')")]
    found = 0
    for mint in mints:
        grad = None
        for t, high in db.execute("SELECT t, high FROM candles WHERE mint = ? ORDER BY t", (mint,)):
            if high >= 0.97 * price_at(t):
                grad = t
                break
        if grad is not None:
            db.execute("UPDATE tokens SET graduated_ts = ?, graduated_src = 'price' WHERE mint = ?",
                       (grad, mint))
            found += 1
    db.commit()
    # how well the price rule agrees with pump.fun's own marker, where both exist
    diffs = []
    for mint, grad in db.execute("SELECT mint, graduated_ts FROM tokens WHERE graduated_src = 'slot' "
                                 "AND classic = 1 ORDER BY graduated_ts DESC LIMIT 500").fetchall():
        for t, high in db.execute("SELECT t, high FROM candles WHERE mint = ? ORDER BY t", (mint,)):
            if high >= 0.97 * price_at(t):
                diffs.append(abs(t - grad // 60000 * 60000) / 60000)
                break
    if diffs:
        diffs.sort()
        print(f"  price rule vs marker on {len(diffs)} coins: median gap {diffs[len(diffs) // 2]:.0f} min, "
              f"90% within {diffs[int(len(diffs) * 0.9)]:.0f} min", flush=True)
    return found


def finish_coins(db, now_ms):
    """Mark coins that have nothing left to fetch."""
    db.execute("UPDATE tokens SET candles_done = 1 WHERE candles_done = 0 AND ("
               "(graduated_ts IS NOT NULL AND bars_to_ts >= graduated_ts + ?) OR "
               "(graduated_ts IS NULL AND bars_to_ts IS NOT NULL AND created_ts < ?))",
               (HORIZON_MS, now_ms - GIVE_UP_MS))
    db.commit()


def iso(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


COLUMNS = [
    "mint", "name", "symbol", "graduated_at", "created_at", "minutes_to_graduate",
    "usd_market_cap", "ath_market_cap_usd", "ath_at", "volume_1h_usd", "last_trade_at",
    "reply_count", "creator", "pump_swap_pool", "bonding_curve", "twitter", "telegram",
    "website", "is_cashback_enabled", "nsfw", "is_banned", "image_uri", "metadata_uri",
    "description", "url",
]


def to_row(c):
    grad, created = c.get("graduated_timestamp"), c.get("created_timestamp")
    return {
        "mint": c["mint"],
        "name": c.get("name", ""),
        "symbol": c.get("symbol", ""),
        "graduated_at": iso(grad),
        "created_at": iso(created),
        "minutes_to_graduate": round((grad - created) / 60000, 1) if grad and created else "",
        "usd_market_cap": c.get("usd_market_cap", ""),
        "ath_market_cap_usd": c.get("ath_market_cap", ""),
        "ath_at": iso(c.get("ath_market_cap_timestamp")),
        "volume_1h_usd": c.get("volume_1h_usd", ""),
        "last_trade_at": iso(c.get("last_trade_timestamp")),
        "reply_count": c.get("reply_count", ""),
        "creator": c.get("creator", ""),
        "pump_swap_pool": c.get("pump_swap_pool", ""),
        "bonding_curve": c.get("bonding_curve", ""),
        "twitter": c.get("twitter") or "",
        "telegram": c.get("telegram") or "",
        "website": c.get("website") or "",
        "is_cashback_enabled": c.get("is_cashback_enabled", ""),
        "nsfw": c.get("nsfw", ""),
        "is_banned": c.get("is_banned", ""),
        "image_uri": c.get("image_uri") or "",
        "metadata_uri": c.get("metadata_uri") or "",
        "description": (c.get("description") or "").replace("\r", " ").replace("\n", " "),
        "url": f"https://pump.fun/coin/{c['mint']}",
    }


WEB_FIELDS = [
    "mint", "name", "symbol", "graduated_timestamp", "created_timestamp", "usd_market_cap",
    "ath_market_cap", "ath_market_cap_timestamp", "last_trade_timestamp", "reply_count",
    "creator", "pump_swap_pool", "twitter", "telegram", "website", "image_uri", "nsfw",
]


def write_atomic(path, write):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        write(f)
    os.replace(tmp, path)


def upsert_tokens(db, coins, now_ms):
    db.executemany(
        """INSERT INTO tokens (mint, name, symbol, creator, pump_swap_pool, created_ts, classic,
             usd_market_cap, ath_market_cap, ath_ts, last_trade_ts, reply_count,
             first_seen_ts, updated_ts, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(mint) DO UPDATE SET
             name=excluded.name, symbol=excluded.symbol, pump_swap_pool=excluded.pump_swap_pool,
             classic=MAX(classic, excluded.classic),
             usd_market_cap=excluded.usd_market_cap, ath_market_cap=excluded.ath_market_cap,
             ath_ts=excluded.ath_ts, last_trade_ts=excluded.last_trade_ts,
             reply_count=excluded.reply_count, updated_ts=excluded.updated_ts, raw=excluded.raw""",
        [(c["mint"], c.get("name"), c.get("symbol"), c.get("creator"), c.get("pump_swap_pool"),
          c.get("created_timestamp"), is_classic(c), c.get("usd_market_cap"), c.get("ath_market_cap"),
          c.get("ath_market_cap_timestamp"), c.get("last_trade_timestamp"), c.get("reply_count"),
          now_ms, now_ms, json.dumps(c, ensure_ascii=False)) for c in coins])
    db.commit()


def publish(db, out, days, now_ms, partial):
    """Write the web/CSV snapshots of coins that graduated inside the window."""
    # the window ends when the snapshot is written: graduation times resolved during a
    # run can be later than the run's start
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - days * DAY * 1000
    rows, pending = [], 0
    # coin dicts are large and the VPS has 1 GB of RAM: stream them, keep only the web rows
    tmp = os.path.join(out, "latest.csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for mint, raw, grad in db.execute(
                """SELECT mint, raw, graduated_ts FROM tokens
                   WHERE graduated_ts BETWEEN ? AND ? OR (graduated_ts IS NULL AND created_ts >= ?)
                   ORDER BY COALESCE(graduated_ts, created_ts) DESC""", (cutoff_ms, now_ms, cutoff_ms)):
            c = json.loads(raw)
            c.setdefault("mint", mint)      # coins saved by the live watch's archive carry a shorter record
            c["graduated_timestamp"] = grad
            pending += grad is None
            w.writerow(to_row(c))
            rows.append([c.get(k) for k in WEB_FIELDS])
    os.replace(tmp, os.path.join(out, "latest.csv"))
    web = {
        "meta": {"generated_at": now_ms, "window_start": cutoff_ms,
                 "window_end": now_ms, "days": days, "count": len(rows),
                 "without_graduation_time": pending, "partial": partial},
        "columns": WEB_FIELDS,
        "rows": rows,
    }
    write_atomic(os.path.join(out, "latest.json"),
                 lambda f: json.dump(web, f, ensure_ascii=False, separators=(",", ":")))
    return len(rows), pending


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7, help="graduation window in days")
    ap.add_argument("--out", default="data", help="output directory")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rate", type=float, default=RATES["/candles"],
                    help="candle requests per second; lower it where other services share the IP")
    ap.add_argument("--no-list", action="store_true",
                    help="skip the coin listing and only work on coins already stored")
    args = ap.parse_args()
    RATES["/candles"] = args.rate
    os.makedirs(args.out, exist_ok=True)
    db = sqlite3.connect(os.path.join(args.out, "pump.db"), timeout=120)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    migrate(db)

    now_ms = int(time.time() * 1000)
    print(f"window: {iso(now_ms - args.days * DAY * 1000)} -> {iso(now_ms)}", flush=True)

    if not args.no_list:
        coins = list_candidates(args.days)
        print(f"candidates from search: {len(coins)}", flush=True)
        print(f"added by /coins cross-check: {cross_check(coins)}", flush=True)
        upsert_tokens(db, coins.values(), now_ms)
        del coins
    publish(db, args.out, args.days, now_ms, partial=True)

    with ThreadPoolExecutor(args.workers) as ex:
        # pass 1 gets bars from creation; pass 2 tops up coins that graduated late in them
        for _ in range(2):
            fetched = fetch_pass(db, ex, now_ms)
            print(f"graduation times from pump.fun's marker: {resolve_slots(db)}", flush=True)
            print(f"graduation times from the completion price: {derive_by_price(db)}", flush=True)
            publish(db, args.out, args.days, now_ms, partial=True)
            if not fetched:
                break
    finish_coins(db, now_ms)

    kept, pending = publish(db, args.out, args.days, now_ms, partial=False)
    n_tok, n_bar = db.execute(
        "SELECT (SELECT COUNT(*) FROM tokens), (SELECT COUNT(*) FROM candles)").fetchone()
    print(f"in the window: {kept} coins ({pending} without a graduation time)")
    print(f"database: {n_tok} tokens, {n_bar} candles in {args.out}/pump.db")


if __name__ == "__main__":
    main()

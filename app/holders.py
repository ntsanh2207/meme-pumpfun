#!/usr/bin/env python3
"""Record holder snapshots of freshly graduated pump.fun coins, for the first hour of each.

pump.fun only tells who holds a coin right now, so the numbers a rule needs at the moment
of buying (holder count, share of the top 10 wallets, creator / sniper / bundler share) have
to be written down as they happen. Runs forever: once a minute it lists the newest graduated
coins from two pump.fun lists (each misses some coins for a long time), and every new coin
that traded in the last 10 minutes is polled every 2 minutes for 32 minutes, then every
5 minutes up to 62 minutes. Snapshots carry their own time, so a coin found late still
gets correct numbers for the minutes that follow.

Snapshots go to their own database (holders.db), so the collector's long writes to pump.db
never delay them.

Usage: python3 holders.py [--db data/holders.db]
"""
import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://frontend-api-v3.pump.fun"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": "https://pump.fun",
    "Accept": "application/json",
}
LIST_EVERY = 60          # seconds between listings; the list endpoint blocks bursts
GAP = 0.5                # seconds between holder requests
FAST, FAST_FOR = 120, 32 * 60      # poll every 2 minutes at first
SLOW, WATCH_FOR = 300, 62 * 60     # then every 5 minutes until the watch ends
ALIVE = 600              # a coin new to us is only worth watching if it traded this recently
INFO = ("name", "symbol", "image_uri", "twitter", "telegram", "website", "created_timestamp",
        "pump_swap_pool", "creator", "virtual_sol_reserves", "boost_mode")   # what watch.py needs to know about a newly listed coin
TOP_COLS = ("top1_pct REAL", "top5_pct REAL", "top50_pct REAL", "new_top_pct REAL",
            "top_snipers INTEGER", "top_bundlers INTEGER", "fees_sol REAL")   # added after the first version
SUPPLY = 1e9             # tokens of every pump.fun coin
NEW_FOR = 300            # a listed wallet that arrived within this many seconds counts as new

SCHEMA = """
CREATE TABLE IF NOT EXISTS holder_watch (
  mint TEXT PRIMARY KEY,
  first_seen_ts INTEGER NOT NULL,   -- unix ms the coin first showed up as graduated
  watched INTEGER NOT NULL,         -- 0: no recent trade when first seen, never polled
  info TEXT                         -- JSON: the coin as listed (name, links, creation time, pool), for watch.py
);
CREATE TABLE IF NOT EXISTS holder_snaps (
  mint TEXT NOT NULL,
  ts INTEGER NOT NULL,              -- unix ms the numbers were read
  holders INTEGER,
  top10_pct REAL, dev_pct REAL, snipers_pct REAL, bundlers_pct REAL,   -- percent of supply
  -- from the list of the 50 largest holders, which leaves out the trading pool
  top1_pct REAL, top5_pct REAL, top50_pct REAL,   -- percent of supply
  new_top_pct REAL,                 -- percent of supply held by listed wallets that arrived in the last 5 minutes
  top_snipers INTEGER, top_bundlers INTEGER,      -- how many of the listed wallets are flagged
  fees_sol REAL,                    -- trading fees paid on the coin so far
  PRIMARY KEY (mint, ts)
) WITHOUT ROWID;
"""


def get(path, params=None):
    """Parsed JSON, or None when the request failed. Returns the wait asked for on a 429."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20) as r:
            return json.load(r), 0
    except urllib.error.HTTPError as e:
        wait = 0
        if e.code in (403, 429):
            try:
                wait = float(e.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            wait = min(max(wait, 30), 120)
        return None, wait
    except Exception:  # timeouts, resets, bad json
        return None, 0


def newest_graduates():
    """Rows of both lists, or None when neither answered."""
    a, wait_a = get("/coins/search-unrestricted", {
        "complete": "true", "includeNsfw": "true", "program": "pump",
        "pumpSwapGraduatedOnly": "true", "sort": "created_timestamp", "order": "DESC", "limit": 200})
    time.sleep(GAP)
    b, wait_b = get("/coins", {"offset": 0, "limit": 50, "sort": "created_timestamp",
                               "order": "DESC", "includeNsfw": "true", "complete": "true"})
    rows = [r for part in (a, b) if isinstance(part, list) for r in part
            if isinstance(r, dict) and r.get("mint") and r.get("pump_swap_pool")]
    if not isinstance(a, list) and not isinstance(b, list):
        return None, max(wait_a, wait_b)
    return rows, 0


def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def top_holders(mint):
    """(top 1 %, top 5 %, top 50 %, % held by new arrivals, snipers, bundlers) from the list of
    the largest holders, or six Nones when it cannot be read. Second value: the wait asked for."""
    d, wait = get("/coins/top-holders/" + mint)
    rows = d.get("topHolders") if isinstance(d, dict) else None
    if not isinstance(rows, list):
        return (None,) * 6, wait
    rows = [r for r in rows if isinstance(r, dict) and num(r.get("amount")) is not None]
    amounts = sorted((r["amount"] for r in rows), reverse=True)
    now = time.time()
    new = [r["amount"] for r in rows if num(r.get("enteredAt")) and now - r["enteredAt"] <= NEW_FOR]
    pct = lambda a: sum(a) / SUPPLY * 100   # noqa: E731
    return (pct(amounts[:1]), pct(amounts[:5]), pct(amounts), pct(new),
            sum(bool(r.get("isSniper")) for r in rows), sum(bool(r.get("isBundler")) for r in rows)), 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="data/holders.db")
    args = ap.parse_args()
    db = sqlite3.connect(args.db, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    if "info" not in {r[1] for r in db.execute("PRAGMA table_info(holder_watch)")}:
        db.execute("ALTER TABLE holder_watch ADD COLUMN info TEXT")
    have = {r[1] for r in db.execute("PRAGMA table_info(holder_snaps)")}
    for col in TOP_COLS:
        if col.split()[0] not in have:
            db.execute(f"ALTER TABLE holder_snaps ADD COLUMN {col}")
    known = {r[0] for r in db.execute("SELECT mint FROM holder_watch")}
    now = time.time()
    # mint -> [first seen, next poll], for coins still inside their watch window
    watch = {m: [ts / 1000, now] for m, ts in db.execute(
        "SELECT mint, first_seen_ts FROM holder_watch WHERE watched = 1 AND first_seen_ts > ?",
        (int((now - WATCH_FOR) * 1000),))}
    next_list, pause_until = 0.0, 0.0   # nothing is ever deleted: pump.fun does not keep past holder numbers
    print(f"recording holders; {len(known)} coins known, {len(watch)} being watched", flush=True)
    while True:
        now = time.time()
        if now < pause_until:
            time.sleep(min(pause_until - now, 5))
            continue
        if now >= next_list:
            next_list = now + LIST_EVERY
            rows, wait = newest_graduates()
            if rows is None:
                pause_until = now + wait
                print(f"listing failed{f'; pausing {wait:.0f}s' if wait else ''}", flush=True)
            else:
                # a coin with no recent trade is dead or was found long after it graduated
                new = {r["mint"]: now - (num(r.get("last_trade_timestamp")) or 0) / 1000 <= ALIVE
                       for r in rows if r["mint"] not in known}
                listed = {r["mint"]: json.dumps({k: r.get(k) for k in INFO}, ensure_ascii=False) for r in rows}
                db.executemany("INSERT OR IGNORE INTO holder_watch VALUES (?,?,?,?)",
                               [(m, int(now * 1000), int(alive), listed[m]) for m, alive in new.items()])
                db.commit()
                known.update(new)
                watch.update({m: [now, now] for m, alive in new.items() if alive})
                if new:
                    print(f"{len(new)} new graduates, {sum(new.values())} trading; "
                          f"{len(watch)} watched", flush=True)
        due = sorted((v[1], m) for m, v in watch.items() if v[1] <= now)
        for _, mint in due:
            first = watch[mint][0]
            if now - first > WATCH_FOR:
                del watch[mint]
                continue
            s, wait = get("/coins/holder-stats/" + mint)
            t = time.time()
            if wait:
                pause_until = t + wait
                print(f"holder requests refused; pausing {wait:.0f}s", flush=True)
                break
            # 0 holders is pump.fun's answer before it has counted a new coin, not a real number
            if isinstance(s, dict) and num(s.get("totalHolders")):
                time.sleep(GAP)
                top, wait = top_holders(mint)
                db.execute("INSERT OR REPLACE INTO holder_snaps (mint, ts, holders, top10_pct, dev_pct, snipers_pct, "
                           "bundlers_pct, top1_pct, top5_pct, top50_pct, new_top_pct, top_snipers, top_bundlers, "
                           "fees_sol) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                               mint, int(t * 1000), int(s["totalHolders"]), num(s.get("top10HoldersPercent")),
                               num(s.get("devHoldingsPercent")), num(s.get("snipersHoldingsPercent")),
                               num(s.get("bundlersHoldingsPercent")), *top, num(s.get("totalFeesSol"))))
                db.commit()
                if wait:
                    pause_until = time.time() + wait
                    print(f"holder requests refused; pausing {wait:.0f}s", flush=True)
                    break
            watch[mint][1] = t + (FAST if t - first < FAST_FOR else SLOW)
            time.sleep(GAP)
            if time.time() >= next_list:
                break
        else:
            time.sleep(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Record each stored coin's first trade (usually the creator's launch buy) in pump.db.

How much the creator bought at launch is known before graduation and separates
"creator bought the whole curve" launches from the rest.

Usage: python3 first_trades.py [--db data/pump.db]
"""
import argparse
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": "https://pump.fun",
    "Accept": "application/json",
}
RATE = 5.0    # requests per second; swap-api advertises 1000/min but Cloudflare blocks bursts
_lock = threading.Lock()
_next = 0.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS first_trades (
  mint TEXT PRIMARY KEY,
  ts INTEGER,              -- unix ms; NULL when pump.fun has no first trade for the coin
  type TEXT, user TEXT, is_dev_buy INTEGER,
  amount_sol REAL, amount_usd REAL
);
"""


def fetch(mint, quick=False):
    """quick: one try with a short timeout and no pauses (a live decision is waiting)."""
    global _next
    for attempt in range(1 if quick else 6):
        with _lock:
            slot = max(_next, time.monotonic())
            _next = slot + 1 / RATE
        time.sleep(max(slot - time.monotonic(), 0))
        try:
            req = urllib.request.Request(
                f"https://swap-api.pump.fun/v1/coins/{mint}/first-trade", headers=HEADERS)
            with urllib.request.urlopen(req, timeout=5 if quick else 30) as r:
                return mint, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return mint, {}
            if e.code == 429 and not quick:
                time.sleep(float(e.headers.get("Retry-After") or 10))
        except Exception:
            pass
        if not quick:
            time.sleep(min(2 ** attempt, 20))
    return mint, None   # leave it for the next run


def to_ms(iso):
    return int(time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone) * 1000


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="data/pump.db")
    args = ap.parse_args()
    db = sqlite3.connect(args.db, timeout=600)   # the collector holds the write lock for minutes
    db.executescript(SCHEMA)
    mints = [r[0] for r in db.execute(
        "SELECT mint FROM tokens WHERE mint NOT IN (SELECT mint FROM first_trades) "
        "ORDER BY created_ts DESC")]
    print(f"first trades to fetch: {len(mints)}", flush=True)
    with ThreadPoolExecutor(8) as ex:
        for i in range(0, len(mints), 500):
            rows = []
            for mint, t in ex.map(fetch, mints[i:i + 500]):
                if t is None:
                    continue
                rows.append((mint, to_ms(t["timestamp"]) if t.get("timestamp") else None,
                             t.get("type"), t.get("userAddress"),
                             int(bool(t.get("isDevBuy"))) if t else None,
                             float(t["amountSol"]) if t.get("amountSol") else None,
                             float(t["amountUsd"]) if t.get("amountUsd") else None))
            db.executemany("INSERT OR REPLACE INTO first_trades VALUES (?,?,?,?,?,?,?)", rows)
            db.commit()
            print(f"  first trades: {min(i + 500, len(mints))}/{len(mints)}", flush=True)


if __name__ == "__main__":
    main()

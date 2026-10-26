"""pump.fun's public API (throttled GET), the Solana RPC address, the pump.db tables and the test for a standard
bonding curve. Shared by watch.py, dipwatch.py and analyze.py. (The collector that used to fill pump.db from
this API was removed on 25 Oct 2026; watch.py's archive loop fills it now.)"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://frontend-api-v3.pump.fun"
CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": "https://pump.fun",
    "Accept": "application/json",
}
# requests per second; /candles advertises 600/min, the coin lists sit behind a
# Cloudflare rule that blocks for 3 minutes after a burst of ~30 requests
RATES = {"/candles": 9.0, "/coins": 0.4}
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


RPC = "https://api.mainnet-beta.solana.com"
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

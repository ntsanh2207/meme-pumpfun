#!/usr/bin/env python3
"""Backtest "buy a pump.fun token 10-30 minutes after it graduates" on the collected candles.

Reads pump.db (written by collector.py), searches entry minute, entry conditions,
take-profit, stop-loss and time-stop, and writes:
  - report.json            : what the web app's Report tab shows
  - backtest_* tables      : every run, its rule ranking and the picked rule's trades,
                             kept in pump.db so later runs can be compared and improved

Rules are ranked on the earlier 70% of graduates (train) only. The single top-ranked rule
is then checked on the later 30% (test), which the search never saw.

Usage: python3 analyze.py [--db data/pump.db] [--out data/report.json]
"""
import argparse
import csv
import hashlib
import itertools
import json
import os
import sqlite3
import time
import warnings
from contextlib import closing

import numpy as np

import rules as rulebook

MIN = 60000
T = 296                      # minutes of post-graduation bars kept by the collector
ENTRIES = [5, 8, 10, 15, 20, 30]   # minutes after graduation
TPS = [0.25, 0.4, 0.6, 1.0, 1.5, 2.5, 4.0]
SLS = [0.15, 0.20, 0.30, 0.40, 0.50]
HOLDS = [30, 60, 120, 240]
FEE = 0.0135                 # per side: PumpSwap's top fee tier (1.25%) plus priority fee/tip
SL_SLIP = 0.03               # extra slippage when a stop fires into a falling market
STAKE_SOL = 0.1              # SOL per trade; turned into USD with the SOL price for price impact
CURVE_SOL = 115 / 279.9e6    # SOL per token at which a classic bonding curve completes
STAKE = 200.0                # what earlier runs used, in USD: only read for runs saved before stakes were in SOL
MAX_IMPACT = 0.05            # skip a coin when the buy alone would move the price more
POOL_TOKENS = 206.9e6        # tokens a pump.fun migration seeds into the PumpSwap pool
MIN_TRADES = 80              # an entry rule must leave at least this many trades in the ranking part
# the rule search: thresholds at these quantiles; rules ranked on the earlier RANK_SHARE of the train coins
# and checked on the rest of them, so the test coins only ever judge the one rule finally picked
QUANTILES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
RANK_SHARE = 0.7
BEAM = 150                   # the best pairs of each entry minute are extended to three conditions
CHECKED = 4000               # best-ranked rule sets checked in detail
MIN_CHECK_TRADES = 10        # trades a rule must make in the checking part
MIN_DAY_SHARE = 0.5          # share of days with trades that must end in profit (ranking + checking parts)
PASS_TEST_TRADES = 20        # test trades needed for a "pass"
SEARCH_SKIP = {"hour_utc"}   # features rules may use but the search does not try
# what separates this backtest from earlier saved runs; shown next to each run in the history
MODEL = "stops fill at the minute close; conditions need full data"
TRAIN_SHARE = 0.7

FEATURES = rulebook.FEATURES

SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ran_ts INTEGER, coins INTEGER, train_coins INTEGER, test_coins INTEGER,
  sample_from_ts INTEGER, sample_to_ts INTEGER,
  params TEXT,        -- JSON: grid, costs, stake
  picked_rule TEXT,   -- JSON: the top train-ranked rule
  train_stats TEXT, test_stats TEXT, verdict TEXT
);
CREATE TABLE IF NOT EXISTS backtest_rules (
  run_id INTEGER, rank INTEGER, entry_min INTEGER, conditions TEXT,
  tp REAL, sl REAL, hold_min INTEGER, score REAL,
  train_trades INTEGER, train_mean REAL, train_win REAL,
  test_trades INTEGER, test_mean REAL, test_win REAL,
  PRIMARY KEY (run_id, rank)
);
CREATE TABLE IF NOT EXISTS backtest_trades (
  run_id INTEGER, mint TEXT, split TEXT, entry_ts INTEGER, entry_price REAL,
  exit_min INTEGER, exit_reason TEXT, price_multiple REAL, net_return REAL,
  PRIMARY KEY (run_id, mint)
);
"""


def load(db):
    has_ft = db.execute("SELECT 1 FROM sqlite_master WHERE name='first_trades'").fetchone()
    dev = ("(SELECT CASE WHEN f.is_dev_buy = 1 THEN f.amount_sol ELSE 0 END FROM first_trades f "
           "WHERE f.mint = k.mint)") if has_ft else "NULL"
    # classic 85 SOL curves only: their pool depth at graduation is known, which the
    # price-impact model needs. candles_done = 1 means bars reach ~5 hours past graduation.
    rows = db.execute(
        f"""SELECT k.mint, k.symbol, k.name, k.graduated_ts / 60000 * 60000, k.raw, {dev},
                   k.created_ts
            FROM tokens k
            WHERE k.graduated_ts >= k.created_ts - 60000 AND k.candles_done = 1 AND k.classic = 1
            ORDER BY k.graduated_ts""").fetchall()
    n = len(rows)
    tok = {
        "mint": [r[0] for r in rows], "symbol": [r[1] for r in rows], "name": [r[2] for r in rows],
        "graduated_ts": np.array([r[3] for r in rows], dtype=np.int64),
        "dev_buy_sol": np.array([np.nan if r[5] is None else r[5] for r in rows]),
        "mins_to_grad": np.array([max(0.0, (r[3] - r[6]) / MIN) for r in rows]),
        "socials": np.zeros(n), "has_twitter": np.zeros(n), "has_website": np.zeros(n),
        "has_telegram": np.zeros(n),
    }
    for i, r in enumerate(rows):
        c = json.loads(r[4])
        for key in ("twitter", "website", "telegram"):
            tok["has_" + key][i] = bool((c.get(key) or "").strip())
        tok["socials"][i] = tok["has_twitter"][i] or tok["has_website"][i] or tok["has_telegram"][i]
    idx = {m: i for i, m in enumerate(tok["mint"])}
    del rows
    tok["holder_snaps"] = load_holders(db, idx)
    arr = {k: np.full((n, T), np.nan, dtype=np.float32) for k in ("open", "high", "low", "close")}
    vol = np.zeros((n, T), dtype=np.float32)
    trd = np.zeros((n, T), dtype=np.float32)
    for mint, t, o, h, low, c, v, tr in db.execute("SELECT * FROM candles"):
        i = idx.get(mint)
        if i is None:
            continue
        m = (t - tok["graduated_ts"][i]) // MIN
        if 0 <= m < T:
            arr["open"][i, m], arr["high"][i, m], arr["low"][i, m], arr["close"][i, m] = o, h, low, c
            vol[i, m], trd[i, m] = v, tr
    # a minute without trades keeps the last price
    close = arr["close"]
    for m in range(1, T):
        gap = np.isnan(close[:, m])
        close[gap, m] = close[gap, m - 1]
    prev = np.concatenate([close[:, :1], close[:, :-1]], axis=1)
    for k in ("open", "high", "low"):
        arr[k] = np.where(np.isnan(arr[k]), prev, arr[k])
    # every classic curve completes at the same price; coins whose creator bought the whole
    # curve at launch open right at it. USD depth of the constant-product pool at price p is
    # then POOL_TOKENS * sqrt(p * p_grad).
    o0 = arr["open"][:, 0]
    full = o0[(tok["mins_to_grad"] < 1) & (o0 > 3.0e-5) & (o0 < 7.0e-5)]
    arr["p_grad"] = float(np.median(full)) if len(full) else 4.5e-5
    arr["stake"] = stake_usd(arr["p_grad"])
    return tok, arr, vol, trd


# what holders.py stores per snapshot; the _pct ones are stored in percent and used as fractions
HOLDER_COLS = ("holders", "top10_pct", "dev_pct", "snipers_pct", "bundlers_pct",
               "top1_pct", "top5_pct", "top50_pct", "new_top_pct", "top_snipers", "top_bundlers", "fees_sol")
HOLDER_MAX_AGE = 6 * MIN     # a snapshot older than this at the moment of buying is not used


def holder_rows(h, mint=None):
    """(mint, [ts, one value per HOLDER_COLS]) for every snapshot, oldest first per coin, NaN
    where nothing was recorded. A holders.db from before a column existed reads as NaN there."""
    have = {r[1] for r in h.execute("PRAGMA table_info(holder_snaps)")}
    cols = ", ".join(c if c in have else "NULL" for c in HOLDER_COLS)
    where, args = ("WHERE mint = ? ORDER BY ts", (mint,)) if mint else ("ORDER BY mint, ts", ())
    for m, ts, *vals in h.execute(f"SELECT mint, ts, {cols} FROM holder_snaps {where}", args):
        yield m, [ts] + [np.nan if v is None else v / 100.0 if c.endswith("_pct") else v
                         for c, v in zip(HOLDER_COLS, vals)]


def load_holders(db, idx):
    """Holder snapshots recorded live by holders.py: coin index -> rows of (ts, then one
    value per HOLDER_COLS), oldest first, shares as fractions. Empty when nothing
    has been recorded; only coins that graduated while the recorder ran have any."""
    main_db = db.execute("PRAGMA database_list").fetchone()[2]
    path = os.path.join(os.path.dirname(main_db or ""), "holders.db")
    if not main_db or not os.path.exists(path):
        return {}
    by = {}
    try:
        # a plain open: a read-only one fails on a copied database that has lost its -wal file
        with closing(sqlite3.connect(path, timeout=60)) as h:
            for mint, row in holder_rows(h):
                i = idx.get(mint)
                if i is not None:
                    by.setdefault(i, []).append(row)
    except sqlite3.Error as e:
        print(f"holder snapshots could not be read ({e}); holder conditions will match nothing", flush=True)
        return {}
    print(f"holder snapshots: {len(by)} sample coins have some", flush=True)
    return {i: np.array(v, dtype=np.float64) for i, v in by.items()}


def holder_features(tok, entry):
    """The last snapshot taken before the moment of buying, per coin; NaN without one.
    The change in holders compares it with the snapshot taken 4 to 8 minutes earlier. The
    creator's share at graduation is the first one recorded, when that was within 6 minutes
    of the graduation minute: snapshots only start once a coin is listed as graduated."""
    n = len(tok["mint"])
    out = {k: np.full(n, np.nan) for k in HOLDER_COLS + ("holders_change_5m", "dev_pct_at_grad", "dev_pct_last")}
    dev = HOLDER_COLS.index("dev_pct") + 1
    for i, rows in tok.get("holder_snaps", {}).items():
        at = tok["graduated_ts"][i] + entry * MIN
        j = np.searchsorted(rows[:, 0], at, side="right") - 1
        early = rows[:j + 1][(rows[:j + 1, 0] <= tok["graduated_ts"][i] + 6 * MIN) & np.isfinite(rows[:j + 1, dev])]
        if len(early):
            out["dev_pct_at_grad"][i] = early[0, dev]
        known = rows[:j + 1][np.isfinite(rows[:j + 1, dev])]
        if len(known) and at - known[-1, 0] <= 30 * MIN:     # the latest non-blank reading, not older than 30 minutes
            out["dev_pct_last"][i] = known[-1, dev]
        if j >= 0 and at - rows[j, 0] <= HOLDER_MAX_AGE:
            for k, name in enumerate(HOLDER_COLS):
                out[name][i] = rows[j, k + 1]
            b = np.searchsorted(rows[:, 0], rows[j, 0] - 4 * MIN, side="right") - 1
            if b >= 0 and rows[j, 0] - rows[b, 0] <= 8 * MIN and rows[b, 1] > 0:
                out["holders_change_5m"][i] = rows[j, 1] / rows[b, 1] - 1
    return out


def depth(arr, price):
    return POOL_TOKENS * np.sqrt(price * arr["p_grad"])


def sol_usd(p_grad):
    """USD per SOL: every classic curve completes at the same SOL price, seen in the data in USD."""
    return p_grad / CURVE_SOL


def stake_usd(p_grad):
    return STAKE_SOL * sol_usd(p_grad)


def net_return(arr, trd, entry, gross, stake=None):
    """Turn a price multiple into a return after fees and the price impact of our own size.

    NaN when the coin is not tradable at entry: no trades in the last 5 minutes, or a
    pool so drained that the buy would move the price more than MAX_IMPACT.
    """
    p0 = arr["close"][:, entry - 1].astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        stake = arr["stake"] if stake is None else stake
        q_in = depth(arr, p0)
        q_out = depth(arr, p0 * gross)
        r = gross * (1 - FEE) ** 2 / ((1 + stake / q_in) * (1 + stake * gross / q_out)) - 1
        alive = trd[:, max(entry - 5, 0):entry].sum(axis=1) > 0
        return np.where(alive & (stake / q_in <= MAX_IMPACT) & np.isfinite(r), r, np.nan)


def simulate(arr, entry, tp, sl, holds, stop_fill="close", be=0.0, be_stop=0.0):
    """Price multiple per coin for: buy at minute `entry`, sell at +tp, -sl or at the time stop.

    One pass serves every time stop in `holds`. Within one bar the stop is assumed to fire
    before the target (the pessimistic order). A stop never fills better than the minute's
    close (stop_fill="close"): when a coin dumps inside one minute, a stop order chases the
    price down. stop_fill="low" fills at the minute's low, the worst case.
    be > 0: once a minute's high is that far above the entry, the stop moves up to the entry price plus be_stop from the next minute.
    Also returns exit reason and minute for the longest hold.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        p0 = arr["close"][:, entry - 1].astype(np.float64)
        n = len(p0)
        gross = np.full(n, np.nan)
        reason = np.zeros(n, dtype=np.int8)     # 1 stop, 2 target, 3 time
        exit_min = np.zeros(n, dtype=np.int16)
        live = np.isfinite(p0) & (p0 > 0)
        stop, target = 1 - sl, 1 + tp
        stops = np.full(n, stop)      # moved to the entry price (1.0) per coin once `be` has been reached
        out = {}
        last = min(entry + max(holds), T)
        for k in range(entry, last):
            o, h, low = arr["open"][:, k] / p0, arr["high"][:, k] / p0, arr["low"][:, k] / p0
            hit = live & (low <= stops)
            worst = low if stop_fill == "low" else arr["close"][:, k] / p0
            gross[hit] = np.minimum(np.minimum(o[hit], stops[hit]), worst[hit]) * (1 - SL_SLIP)
            reason[hit], exit_min[hit] = 1, k
            live &= ~hit
            hit = live & (h >= target)
            gross[hit] = target
            reason[hit], exit_min[hit] = 2, k
            live &= ~hit
            if be > 0:
                stops[live & (h >= 1 + be)] = 1.0 + be_stop
            held = k - entry + 1
            if held in holds or k == last - 1:
                g = gross.copy()
                g[live] = arr["close"][live, k] / p0[live]
                out[held if held in holds else max(holds)] = g
        reason[live], exit_min[live] = 3, last - 1
    return out, reason, exit_min


def features(tok, arr, vol, trd, entry):
    """Everything here is known at the moment of entry."""
    c = arr["close"].astype(np.float64)
    p0 = c[:, entry - 1]
    # the last 5 minutes; an entry earlier than minute 5 (a crossing in minute 2 or 3) has fewer minutes behind it: the minutes
    # so far, scaled to 5, so that the "last 5 minutes" figures stay comparable
    span = min(entry, 5)
    vol5 = vol[:, entry - span:entry].sum(axis=1).astype(np.float64) * (5 / span)
    trd5 = trd[:, entry - span:entry].sum(axis=1).astype(np.float64) * (5 / span)
    vol_all = vol[:, :entry].sum(axis=1).astype(np.float64)
    trd_all = trd[:, :entry].sum(axis=1).astype(np.float64)
    # minutes since the highest price so far (0 = the last whole minute set the high); no bars = unknown
    hi = arr["high"][:, :entry].astype(np.float64)
    known = np.isfinite(hi).any(axis=1)
    since_high = np.where(known, entry - 1 - np.argmax(np.where(np.isfinite(hi), hi, -np.inf), axis=1), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        return {
            "ret_since_grad": p0 / arr["p_grad"] - 1,
            "drawdown_from_high": p0 / np.nanmax(arr["high"][:, :entry], axis=1) - 1,
            # 5 minutes back, or since the graduation minute when buying earlier than that
            "ret_last_5m": p0 / c[:, max(entry - 6, 0)] - 1,
            "mcap_usd": p0 * 1e9,
            "vol_usd_total": vol[:, :entry].sum(axis=1).astype(np.float64),
            "vol_usd_last_5m": vol5,
            "trades_total": trd[:, :entry].sum(axis=1).astype(np.float64),
            "trades_last_5m": trd5,
            "avg_trade_usd_5m": vol5 / np.maximum(trd5, 1.0),
            # big trades on average = real buyers rather than bots (the idea behind Strategy B)
            "avg_trade_usd_total": vol_all / np.maximum(trd_all, 1.0),
            "mins_since_high": since_high,
            "hour_utc": (np.asarray(tok["graduated_ts"], dtype=np.int64) // 3600000 % 24).astype(np.float64),
            "dev_buy_sol": tok["dev_buy_sol"],
            "mins_to_grad": tok["mins_to_grad"],
            "socials": tok["socials"],
            "has_twitter": tok["has_twitter"],
            "has_website": tok["has_website"],
            "has_telegram": tok["has_telegram"],
            **holder_features(tok, entry),
        }


# ---- execution delay: replaying a rule's trades on 1-second bars ----
LATENCIES = (0, 1, 2, 3)     # seconds between seeing a signal and the transaction landing
SEC_WINDOW = 300             # 1-second bars per request (the API's maximum)
SEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_bars (
  mint TEXT NOT NULL, t INTEGER NOT NULL,      -- second the bar opens, unix ms
  open REAL, high REAL, low REAL, close REAL,  -- USD per token
  PRIMARY KEY (mint, t)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sec_windows (       -- 1-second windows already fetched, by their end
  mint TEXT NOT NULL, to_ms INTEGER NOT NULL, bars INTEGER, PRIMARY KEY (mint, to_ms)
) WITHOUT ROWID;
"""


# ---- the crossing pattern (research of 19 Oct 2026, rules of 20 Oct 2026) ----
# A coin that graduated at the standard price (the close of its graduation minute above CROSS_START_MCAP and not above CROSS_PRE_MCAP),
# then closed below 0.97x p_grad for at least CROSS_DIP of the minutes 1..m-1, with no low under CROSS_FLOOR_MCAP in them and no close
# above CROSS_PRE_MCAP before the dip started (the dip starts at the first minute that closes below 0.97x p_grad), and whose minute m is
# the first to close back at or above p_grad (minute m-1 closed below it). m runs from CROSS_FIRST to CROSS_LAST.
# The three market-cap limits are in US dollars (every coin has 1e9 tokens: market cap = price x SUPPLY).
# crossing.py watches live graduates with the same functions; c and lo are 1-D arrays by minute after the graduation minute.
CROSS_FIRST, CROSS_LAST, CROSS_DIP, CROSS_BELOW = 2, 12, 2, 0.97      # (CROSS_DIP was 1 until 20 Oct 2026 afternoon)
CROSS_LOWHIGH = 2      # and at least this many of the minutes 1..m-1 have a high no higher than the high of minute 1 (so a price that keeps making new highs is not a dip); added 20 Oct 2026
CROSS_FLOOR_MCAP, CROSS_PRE_MCAP, CROSS_START_MCAP, SUPPLY = 15000.0, 50000.0, 35000.0, 1e9


def cross_base(c, p0):
    """The graduation minute closed between CROSS_START_MCAP and the pre-dip limit (known once that minute has closed). p0 is not used."""
    return bool(CROSS_START_MCAP / SUPPLY < c[0] <= CROSS_PRE_MCAP / SUPPLY)


def cross_dip(c, lo, p0, m, hi=None):
    """Minutes 1..m-1 hold the dip: enough closes below p0 (the level to cross: the highest price of the graduation minute in crossing.py since 20 Oct 2026, the standard graduation price before), no collapse, and nothing above the pre-dip limit before it."""
    with np.errstate(invalid="ignore"):
        below = c[1:m] < CROSS_BELOW * p0
        if below.sum() < CROSS_DIP:
            return False
        if hi is not None and (hi[1:m] <= hi[1]).sum() < CROSS_LOWHIGH:      # (minute 1 counts itself)
            return False
        start = 1 + int(np.argmax(below))          # the dip starts at the first minute that closed below
        return bool(np.nanmin(lo[1:m]) >= CROSS_FLOOR_MCAP / SUPPLY and np.nanmax(c[:start]) <= CROSS_PRE_MCAP / SUPPLY)


def cross_at(c, lo, p0, m, hi=None):
    """Minute m is a crossing: the dip is there and minute m closes at/above p0 after m-1 closed below it."""
    return bool(CROSS_FIRST <= m <= CROSS_LAST and cross_base(c, p0) and cross_dip(c, lo, p0, m, hi) and c[m] >= p0 and c[m - 1] < p0)


def crossing_minute(c, lo, p0, hi=None):
    """The first crossing minute of a coin, or -1."""
    for m in range(CROSS_FIRST, CROSS_LAST + 1):
        if cross_at(c, lo, p0, m, hi):
            return m
    return -1


def sec_bars(db, mint, pool, to_ms):
    """1-second bars of the 300 seconds before to_ms, fetched once from pump.fun and kept in
    pump.db. A second without a trade has no bar. None when pump.fun could not be reached."""
    if not db.execute("SELECT 1 FROM sec_windows WHERE mint = ? AND to_ms = ?", (mint, to_ms)).fetchone():
        import collector    # only this replay talks to pump.fun
        params = {"res": "1s", "count": SEC_WINDOW, "to": to_ms}
        if pool:
            params["pool"] = pool
        time.sleep(2.5)     # this resolution is refused when asked for quickly; what fails is asked again next run
        try:
            r = collector.get(f"/candles/{collector.CHAIN}/{mint}", params, retries=6) or {}
        except RuntimeError:
            return None
        bars = r.get("candles") or []
        db.executemany("INSERT OR REPLACE INTO sec_bars VALUES (?,?,?,?,?,?)",
                       [(mint, int(b["openTimeMs"]), float(b["open"]), float(b["high"]), float(b["low"]),
                         float(b["close"])) for b in bars])
        db.execute("INSERT OR REPLACE INTO sec_windows VALUES (?,?,?)", (mint, to_ms, len(bars)))
        db.commit()
    return db.execute("SELECT t, open, high, low, close FROM sec_bars WHERE mint = ? AND t >= ? AND t < ? ORDER BY t",
                      (mint, to_ms - SEC_WINDOW * 1000, to_ms)).fetchall()


def replay(db, mint, pool, grad_min, e, tp, sl, hold, closes, lows, highs, lag):
    """One trade with `lag` seconds between each signal and its transaction, on 1-second bars
    where they matter (around the buy and around the sale) and 1-minute bars in between.

    The buy is decided when minute e-1 ends and fills at the last traded price `lag` seconds
    later; take profit and stop loss are set from that fill. A sale is decided at the second the
    price touches either level (the stop first when both are touched in the same second) and
    fills at the last traded price `lag` seconds later, so a falling price is sold lower and a
    spiking price can be sold above the target. A stop also loses SL_SLIP, as in the backtest.
    Returns (gross multiple, exit, seconds held) or None when the 1-second bars are missing."""
    t0 = grad_min + e * MIN

    def price_at(bars, t, fallback):
        """Last trade before t (bars open on the second, so a bar counts once it has ended)."""
        last = fallback
        for bt, o, h, low, c in bars:
            if bt + 1000 > t:
                break
            last = c
        return last

    first = sec_bars(db, mint, pool, t0 + SEC_WINDOW * 1000)
    if first is None or not first:
        return None
    buy = price_at(first, t0 + lag * 1000, closes[e - 1])
    target, stop = buy * (1 + tp), buy * (1 - sl)

    def touch(bars, start, end):
        for bt, o, h, low, c in bars:
            if bt < start or bt >= end:
                continue
            if low <= stop:
                return bt + 1000, "stop"
            if h >= target:
                return bt + 1000, "target"
        return None

    end = t0 + hold * MIN
    hit = touch(first, t0 + lag * 1000, min(end, t0 + SEC_WINDOW * 1000))
    if hit is None:
        # past the first 5 minutes: find the minute where a level is touched, then its exact second
        for k in range(e + SEC_WINDOW // 60, min(e + hold, len(closes))):
            if lows[k] <= stop or highs[k] >= target:
                bars = sec_bars(db, mint, pool, grad_min + (k + 1) * MIN + 60000)
                if bars is None:
                    return None
                hit = touch(bars, grad_min + k * MIN, grad_min + (k + 1) * MIN)
                if hit:
                    break
        if hit is None:
            k = min(e + hold, len(closes)) - 1
            return closes[k] / buy, "time", (k - e + 1) * 60
    when, reason = hit
    bars = sec_bars(db, mint, pool, when + SEC_WINDOW * 1000 - 60000) if when + lag * 1000 > t0 + SEC_WINDOW * 1000 else first
    sell = price_at(bars or [], when + lag * 1000, target if reason == "target" else stop)
    if lag == 0:
        sell = target if reason == "target" else min(stop, sell)    # no delay: the order sits at the level
    if reason == "stop":
        sell *= 1 - SL_SLIP
    return sell / buy, reason, (when - t0) // 1000 + lag


def latency_study(db, tok, arr, trd, mask, e, tp, sl, hold):
    """A rule's trades replayed with each delay of LATENCIES, next to the minute-bar backtest."""
    db.executescript(SEC_SCHEMA)
    pools = dict(db.execute("SELECT mint, pump_swap_pool FROM tokens"))
    gr_min, _, _ = simulate(arr, e, tp, sl, [hold])
    base = net_return(arr, trd, e, gr_min[hold])
    idx = np.flatnonzero(mask & np.isfinite(base))
    trades, missing = [], 0
    for i in idx:
        mint, grad_min = tok["mint"][i], int(tok["graduated_ts"][i])
        c, low, h = (arr[k][i].astype(np.float64) for k in ("close", "low", "high"))
        row = {"mint": mint, "symbol": tok["symbol"][i], "entry_ts": grad_min + e * MIN, "backtest": float(base[i]), "by_lag": {}}
        for lag in LATENCIES:
            got = replay(db, mint, pools.get(mint), grad_min, e, tp, sl, hold, c, low, h, lag)
            if got is None:
                break
            g, reason, secs = got
            # fees and the order's price impact as in the backtest (impact from the minute price)
            r = float(net_return(arr, trd, e, np.where(np.arange(len(base)) == i, g, np.nan))[i])
            row["by_lag"][str(lag)] = {"net": r, "exit": reason, "seconds": int(secs), "gross": float(g)}
        if len(row["by_lag"]) == len(LATENCIES):
            trades.append(row)
        else:
            missing += 1
    summary = []
    for lag in LATENCIES:
        x = np.array([t["by_lag"][str(lag)]["net"] for t in trades])
        summary.append({"lag": lag, **stats(x), "total": float(x.sum()) if len(x) else 0.0})
    bt = np.array([t["backtest"] for t in trades])
    return {"lags": list(LATENCIES), "trades": trades, "missing": missing, "summary": summary,
            "backtest": {**stats(bt), "total": float(bt.sum()) if len(bt) else 0.0}}


def features_at(tok, arr, vol, trd):
    """features(...) at any minute, each computed once: for conditions measured before the entry minute."""
    cache = {}

    def at(e):
        if e not in cache:
            cache[e] = features(tok, arr, vol, trd, e)
        return cache[e]
    return at


def matches(f, conditions, at=None):
    """Which coins satisfy every condition. A coin without data for a condition does not.
    A condition with "at" is measured at that earlier minute: at(minute) gives the features there."""
    mask = np.ones(len(f["mcap_usd"]), dtype=bool)
    with np.errstate(invalid="ignore"):
        for c in conditions:
            x = (at(c["at"]) if c.get("at") else f)[c["feature"]]
            mask &= (x >= c["value"]) if c["op"] == ">=" else (x <= c["value"]) if c["op"] == "<=" \
                else (x == c["value"])
    return mask


def cluster_labels(f):
    """Index into rulebook.CLUSTERS per coin: the first cluster whose conditions all hold."""
    lab = np.full(len(f["mcap_usd"]), -1, dtype=np.int8)
    for k, cl in enumerate(rulebook.CLUSTERS):
        lab[(lab < 0) & matches(f, cl["conditions"])] = k
    return lab


CLUSTER_ENTRIES = [10, 15, 30]
CLUSTER_EXIT = (0.4, 0.3, 60)    # the neutral exit every cluster is shown under
CLUSTER_MIN = 30                 # train trades an exit needs before it may be picked for a cluster


def cluster_study(tok, arr, vol, trd, net, train, test):
    """Every kind of token, bought 10, 15 and 30 minutes after graduation: how many there are,
    how often they collapse, what a neutral exit returns, on how many days that was positive,
    and what the best sell rule for that cluster (picked on the earlier 70%) did on the rest.

    `net` holds the net return of every exit in the search grid, keyed (entry, tp, sl, hold).
    """
    day = tok["graduated_ts"] // 86400000
    days = np.unique(day)
    with np.errstate(divide="ignore", invalid="ignore"):
        crash_bar = arr["close"] <= 0.3 * arr["open"]
    rng = np.random.default_rng(0)
    rows = [{"id": c["id"], "name": c["name"], "note": c["note"],
             "conditions": [{**x, "label": FEATURES[x["feature"]][0], "kind": FEATURES[x["feature"]][1]}
                            for x in c["conditions"]], "by_entry": []} for c in rulebook.CLUSTERS]
    for e in CLUSTER_ENTRIES:
        lab = cluster_labels(features(tok, arr, vol, trd, e))
        r = net[(e, *CLUSTER_EXIT)].astype(np.float64)
        ok = np.isfinite(r)
        grid = [k for k in net if k[0] == e]
        for k, row in enumerate(rows):
            m = ok & (lab == k)
            per_day = [{"day": int(d) * 86400000, "trades": int((m & (day == d)).sum()),
                        "mean": float(r[m & (day == d)].mean())} for d in days if (m & (day == d)).any()]
            best, verdict = None, "loses"
            if (m & train).sum() >= CLUSTER_MIN:
                key = max(grid, key=lambda g: np.nanmean(net[g][m & train]))
                rb = net[key].astype(np.float64)
                best = {"tp": key[1], "sl": key[2], "hold": key[3],
                        "train": stats(rb[m & train]), "test": stats(rb[m & test])}
                te = rb[m & test]
                if len(te) and best["train"]["mean"] > 0 and best["test"]["mean"] > 0:
                    lo = np.quantile(rng.choice(te, (2000, len(te))).mean(axis=1), 0.05)
                    verdict = "profit" if len(te) >= 40 and lo > 0 else "lead"
            row["by_entry"].append({
                "entry": e, "tradable": int(m.sum()), "share": float(m.sum() / max(ok.sum(), 1)),
                "collapse_30": float(crash_bar[m, e:e + 30].any(axis=1).mean()) if m.any() else None,
                "all": stats(r[m]), "train": stats(r[m & train]), "test": stats(r[m & test]), "per_day": per_day,
                "days_positive": sum(d["mean"] > 0 for d in per_day), "days": len(per_day),
                "best_exit": best, "verdict": verdict})
    return {"entries": CLUSTER_ENTRIES, "exit": CLUSTER_EXIT, "min_trades": CLUSTER_MIN,
            "exits_tried": len(TPS) * len(SLS) * len(HOLDS), "clusters": rows}


EARLY = [5, 10, 15]


def early_window(tok, arr, vol, trd):
    """Buying 5, 10 and 15 minutes after graduation: what tokens do next, which kinds of
    token lose least, and how a few plain rules fare. All tokens, no train/test split:
    this describes the sample, it does not pick anything."""
    slow, quick = (0.4, 0.3, 60), (0.15, 0.5, 5)
    kinds = [
        ("Creator bought the whole curve, or it graduated in its first minute",
         lambda f: (f["dev_buy_sol"] > 80) | (f["mins_to_grad"] < 1)),
        ("Organic launch", lambda f: ~((f["dev_buy_sol"] > 80) | (f["mins_to_grad"] < 1))),
        ("Has website and X", lambda f: (f["has_website"] == 1) & (f["has_twitter"] == 1)),
        ("No website and no X", lambda f: (f["has_website"] == 0) & (f["has_twitter"] == 0)),
        ("Within 3% of its high", lambda f: f["drawdown_from_high"] >= -0.03),
        ("3% to 30% below its high", lambda f: (f["drawdown_from_high"] < -0.03) & (f["drawdown_from_high"] >= -0.3)),
        ("More than 30% below its high", lambda f: f["drawdown_from_high"] < -0.3),
        ("Below the graduation price", lambda f: f["ret_since_grad"] < 0),
        ("Up more than 10x since graduation", lambda f: f["ret_since_grad"] >= 9),
        ("Falling over the last 5 minutes", lambda f: f["ret_last_5m"] < 0),
        ("Up 0% to 30% over the last 5 minutes", lambda f: (f["ret_last_5m"] >= 0) & (f["ret_last_5m"] <= 0.3)),
        ("Up more than 30% over the last 5 minutes", lambda f: f["ret_last_5m"] > 0.3),
        ("5-minute volume under $5,000", lambda f: f["vol_usd_last_5m"] < 5000),
        ("5-minute volume $20,000 or more", lambda f: f["vol_usd_last_5m"] >= 20000),
    ]
    calm = lambda f: (f["drawdown_from_high"] >= -0.03) & (f["ret_last_5m"] >= 0) & (f["ret_last_5m"] <= 0.3)  # noqa: E731
    plain = [
        ("Buy every tradable token", "No conditions", lambda f: np.ones(len(f["mcap_usd"]), dtype=bool), slow),
        ("Website and X with volume", "Website, X, 5-minute volume at least $20,000, market cap at least $60,000",
         lambda f: (f["has_website"] == 1) & (f["has_twitter"] == 1) & (f["vol_usd_last_5m"] >= 20000)
         & (f["mcap_usd"] >= 60000), slow),
        ("Calm climb", "Within 3% of its high and up 0% to 30% over the last 5 minutes", calm, slow),
        ("Calm climb, quick profit", "Within 3% of its high and up 0% to 30% over the last 5 minutes", calm, quick),
    ]
    c = arr["close"].astype(np.float64)
    overview, group_rows, rule_rows = [], {k: [] for k, _ in kinds}, {k[0]: [] for k in plain}
    for e in EARLY:
        f = features(tok, arr, vol, trd, e)
        res = {x: net_return(arr, trd, e, simulate(arr, e, x[0], x[1], [x[2]])[0][x[2]]) for x in (slow, quick)}
        ok = np.isfinite(res[slow])
        with np.errstate(divide="ignore", invalid="ignore"):
            p0 = c[:, e - 1]
            later = c[:, e + 29] / p0 - 1
            best = np.nanmax(arr["high"][:, e:e + 30], axis=1) / p0 - 1
            overview.append({"entry": e, "tradable": int(ok.sum()), "share": float(ok.mean()),
                             "median_30": float(np.median(later[ok])), "down_70": float((later[ok] <= -0.7).mean()),
                             "best_30": float(np.median(best[ok])),
                             "slow": stats(res[slow]), "quick": stats(res[quick])})
            for label, cond in kinds:
                group_rows[label].append(stats(res[slow][ok & cond(f)]))
            for name, text, cond, x in plain:
                rule_rows[name].append(stats(res[x][ok & cond(f)]))
    return {"entries": EARLY, "slow": slow, "quick": quick, "overview": overview,
            "groups": [{"label": k, "by_entry": v} for k, v in group_rows.items()],
            "rules": [{"name": name, "conditions": text, "exit": x, "by_entry": rule_rows[name]}
                      for name, text, _, x in plain]}


def stats(r):
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return {"trades": 0, "mean": None, "median": None, "win": None, "pf": None}
    gain, loss = r[r > 0].sum(), -r[r <= 0].sum()
    return {"trades": int(len(r)), "mean": float(r.mean()), "median": float(np.median(r)),
            "win": float((r > 0).mean()), "pf": float(gain / loss) if loss else None}


def clean(x):
    """Make numpy values and NaN safe for JSON."""
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return float(x) if np.isfinite(x) else None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def migrate(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(backtest_runs)")}
    for col in ("kind TEXT", "rule_id TEXT", "rule_name TEXT", "rule_hash TEXT", "study TEXT"):
        if col.split()[0] not in cols:
            db.execute(f"ALTER TABLE backtest_runs ADD COLUMN {col}")
    if "features" not in {r[1] for r in db.execute("PRAGMA table_info(backtest_trades)")}:
        db.execute("ALTER TABLE backtest_trades ADD COLUMN features TEXT")
    db.execute("UPDATE backtest_runs SET kind = 'search' WHERE kind IS NULL")
    db.commit()


STUDY_VERSION = 7   # bump to make every saved rule run again with new study data


def study_rule(f, r, took, train, test, reason):
    """What a finished rule run can teach: where the money went, how winners differed from
    losers at the moment of buying, and which single extra condition would have helped.

    Extra conditions are found on the earlier 70% of tokens and then checked on the most
    recent 30%, so one that only fits old tokens shows up as not holding.
    """
    names = {1: "stop", 2: "target", 3: "time"}
    idx = np.flatnonzero(took)
    exits = [{"exit": names[k], "trades": int((reason[idx] == k).sum()),
              "mean": float(r[idx][reason[idx] == k].mean()) if (reason[idx] == k).any() else None,
              "total": float(r[idx][reason[idx] == k].sum())} for k in names]
    crashed = r[idx] <= -0.7
    exits.append({"exit": "collapse", "trades": int(crashed.sum()),
                  "mean": float(r[idx][crashed].mean()) if crashed.any() else None,
                  "total": float(r[idx][crashed].sum())})
    lab = cluster_labels(f)
    by_cluster = [{"id": c["id"], "name": c["name"], "trades": int((took & (lab == k)).sum()),
                   "mean": float(r[took & (lab == k)].mean()), "total": float(r[took & (lab == k)].sum()),
                   "win": float((r[took & (lab == k)] > 0).mean()),
                   "collapsed": float((r[took & (lab == k)] <= -0.7).mean())}
                  for k, c in enumerate(rulebook.CLUSTERS) if (took & (lab == k)).any()]
    win, lose = took & (r > 0), took & (r <= 0)
    profile = []
    for name, (label, kind) in FEATURES.items():
        x = f[name]
        if not (win.any() and lose.any()):
            break
        agg = np.nanmean if kind == "flag" else np.nanmedian
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)   # a condition nobody has data for
            profile.append({"feature": name, "label": label, "kind": kind,
                            "winners": float(agg(x[win])), "losers": float(agg(x[lose]))})
    base_tr = float(r[took & train].mean()) if (took & train).any() else None
    base_te = float(r[took & test].mean()) if (took & test).any() else None
    ideas = []
    if base_tr is not None:
        for name, (label, kind) in FEATURES.items():
            x = f[name]
            with np.errstate(invalid="ignore"):
                if kind == "flag":
                    options = [("=", 1.0, x == 1)]
                else:
                    known = x[took & train]
                    known = known[np.isfinite(known)]
                    if len(known) < 30:
                        continue
                    options = []
                    for q in (0.2, 0.4, 0.6, 0.8):
                        thr = float(np.quantile(known, q))
                        options += [(">=", thr, x >= thr), ("<=", thr, x <= thr)]
            for op, value, cond in options:
                m = took & cond
                n_tr, n_te = int((m & train).sum()), int((m & test).sum())
                if n_tr < 30 or n_te < 10 or n_tr == int((took & train).sum()):
                    continue
                tr_mean, te_mean = float(r[m & train].mean()), float(r[m & test].mean())
                if tr_mean <= base_tr:
                    continue
                ideas.append({"feature": name, "label": label, "kind": kind, "op": op, "value": value,
                              "train_trades": n_tr, "train_mean": tr_mean, "test_trades": n_te,
                              "test_mean": te_mean, "held": bool(base_te is not None and te_mean > base_te)})
    # one idea per condition: its best threshold, ranked only by the earlier tokens, so the
    # "held up" column stays an honest check instead of deciding which rows are shown.
    # Two conditions that keep exactly the same round trips (market cap and price change
    # since graduation are the same thing in different units) count once.
    best, kept = {}, set()
    for i in sorted(ideas, key=lambda i: -i["train_mean"]):
        same_trades = (i["train_trades"], round(i["train_mean"], 12), i["test_trades"], round(i["test_mean"], 12))
        if (i["feature"], i["op"]) in best or same_trades in kept:
            continue
        best[(i["feature"], i["op"])] = i
        kept.add(same_trades)
    ideas = list(best.values())[:6]
    return {"exits": exits, "clusters": by_cluster, "profile": profile, "ideas": ideas,
            "base": {"train_mean": base_tr, "test_mean": base_te}}


def run_rules(db, user_rules, tok, arr, vol, trd, train, test):
    """Backtest each enabled saved rule on every sample coin and save it as a run.

    A rule is skipped when the same rule was already run on the same sample with the same
    model, so saving the settings twice does not fill the history with copies.
    """
    gts = tok["graduated_ts"]
    n = len(gts)
    names = {1: "stop", 2: "target", 3: "time"}
    params = {"model": MODEL, "fee": FEE, "sl_slip": SL_SLIP, "stake": arr["stake"], "stake_sol": STAKE_SOL,
              "sol_usd": sol_usd(arr["p_grad"]),
              "max_impact": MAX_IMPACT, "train_share": TRAIN_SHARE}
    done = 0
    for rule in user_rules:
        if not rule["enabled"]:
            continue
        core = {k: rule[k] for k in ("entry", "conditions", "tp", "sl", "hold")}
        if rule.get("be"):
            core["be_stop"] = rule.get("be_stop", 0.0)
            core["be"] = rule["be"]       # (left out when none, so the saved runs of other rules stay valid)
        digest = hashlib.sha1(json.dumps([core, MODEL, n, int(gts[-1]), STUDY_VERSION, STAKE_SOL],
                                         sort_keys=True).encode()).hexdigest()
        if db.execute("SELECT 1 FROM backtest_runs WHERE rule_id = ? AND rule_hash = ?",
                      (rule["id"], digest)).fetchone():
            db.execute("UPDATE backtest_runs SET rule_name = ? WHERE rule_id = ? AND rule_hash = ?",
                       (rule["name"], rule["id"], digest))
            continue
        e, hold = rule["entry"], rule["hold"]
        f = features(tok, arr, vol, trd, e)
        mask = matches(f, rule["conditions"], features_at(tok, arr, vol, trd))
        by_hold, reason, exit_min = simulate(arr, e, rule["tp"], rule["sl"], [hold], be=rule.get("be", 0.0), be_stop=rule.get("be_stop", 0.0))
        gr = by_hold[hold]
        r = net_return(arr, trd, e, gr)
        idx = np.flatnonzero(mask & np.isfinite(r))
        s_tr, s_te = stats(r[mask & train]), stats(r[mask & test])
        total = float(r[idx].sum()) if len(idx) else 0.0
        verdict = "no_trades" if not len(idx) else "profit" if total > 0 else "loss"
        shown = {"entry": e, "tp": rule["tp"], "sl": rule["sl"], "hold": hold, "be": rule.get("be", 0.0), "be_stop": rule.get("be_stop", 0.0),
                 "conditions": [{**c, "label": FEATURES[c["feature"]][0], "kind": FEATURES[c["feature"]][1]}
                                for c in rule["conditions"]]}
        took = mask & np.isfinite(r)
        study = study_rule(f, r, took, train, test, reason)
        cur = db.execute(
            "INSERT INTO backtest_runs (ran_ts, coins, train_coins, test_coins, sample_from_ts, "
            "sample_to_ts, params, picked_rule, train_stats, test_stats, verdict, kind, rule_id, "
            "rule_name, rule_hash, study) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time() * 1000), n, int(train.sum()), int(test.sum()), int(gts[0]), int(gts[-1]),
             json.dumps(params), json.dumps(clean(shown)), json.dumps(clean(s_tr)), json.dumps(clean(s_te)),
             verdict, "rule", rule["id"], rule["name"], digest, json.dumps(clean(study))))
        p0 = arr["close"][:, e - 1]
        # every round trip keeps what the token looked like when it was bought, for later study
        db.executemany(
            "INSERT INTO backtest_trades (run_id, mint, split, entry_ts, entry_price, exit_min, "
            "exit_reason, price_multiple, net_return, features) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(cur.lastrowid, tok["mint"][i], "test" if test[i] else "train", int(gts[i] + e * MIN),
              float(p0[i]), int(exit_min[i]), names[int(reason[i])], float(gr[i]), float(r[i]),
              json.dumps(clean({k: float(v[i]) for k, v in f.items()}))) for i in idx])
        done += 1
        print(f"rule '{rule['name']}': {len(idx)} trades, total {total:+.1f} stakes", flush=True)
    db.commit()
    return done


def strategy_doc(rule, tok, arr, vol, trd, train, test):
    """One rule's backtest on the current sample, for the Lab page: how many tokens each condition left, the result
    trade by trade and day by day, and every round trip with what its conditions measured. Returns (doc, total in stakes)."""
    gts, stake = tok["graduated_ts"], arr["stake"]
    names = {1: "stop", 2: "target", 3: "time"}
    e, hold = rule["entry"], rule["hold"]
    f = features(tok, arr, vol, trd, e)
    at = features_at(tok, arr, vol, trd)
    by_hold, reason, exit_min = simulate(arr, e, rule["tp"], rule["sl"], [hold], be=rule.get("be", 0.0), be_stop=rule.get("be_stop", 0.0))
    gr = by_hold[hold]
    r = net_return(arr, trd, e, gr)
    tradable = np.isfinite(r)
    took = tradable.copy()
    funnel = []
    for c in rule["conditions"]:
        alone = tradable & matches(f, [c], at)
        took &= alone
        known = tradable & np.isfinite((at(c["at"]) if c.get("at") else f)[c["feature"]])
        funnel.append({**c, "label": FEATURES[c["feature"]][0], "kind": FEATURES[c["feature"]][1],
                       "left": int(took.sum()), "alone": int(alone.sum()), "known": int(known.sum()),
                       # holder numbers exist only for tokens that graduated while the recorder ran
                       "known_from": int(gts[known].min()) if known.any() else None})
    idx = np.flatnonzero(took)
    equity, total, days = [], 0.0, {}
    for k, i in enumerate(idx, 1):
        total += float(r[i])
        ts = int(gts[i] + e * MIN)
        equity.append({"i": k, "ts": ts, "total": total, "is_test": bool(test[i])})
        days.setdefault(ts // 86400000 * 86400000, []).append(i)
    per_day = [{"day": d, "trades": len(v), "total": float(r[v].sum()), "mean": float(r[v].mean()),
                "is_test": bool(test[v].mean() > 0.5)} for d, v in sorted(days.items())]
    measured = rulebook.page_columns(rule["conditions"])
    reading = lambda k: at(int(k.split("@")[1]))[k.split("@")[0]] if "@" in k else f[k]   # noqa: E731
    p0 = arr["close"][:, e - 1]
    trades = [[tok["mint"][i], tok["symbol"][i], "test" if test[i] else "train", int(gts[i] + e * MIN),
               int(exit_min[i]) - e + 1, names[int(reason[i])], float(r[i]), float(r[i]) * stake,
               float(p0[i]), float(gr[i])] + [float(reading(k)[i]) for k in measured] for i in idx[::-1]]
    doc = {"generated_at": int(time.time() * 1000), "id": rule["id"], "name": rule["name"],
           "rule": {"entry": e, "tp": rule["tp"], "sl": rule["sl"], "hold": hold, "be": rule.get("be", 0.0), "be_stop": rule.get("be_stop", 0.0),
                    "conditions": [{**c, "label": FEATURES[c["feature"]][0], "kind": FEATURES[c["feature"]][1]}
                                   for c in rule["conditions"]]},
           "sample": {"coins": len(gts), "train": int(train.sum()), "test": int(test.sum()),
                      "from_ts": int(gts[0]), "to_ts": int(gts[-1]), "tradable": int(tradable.sum())},
           "model": MODEL, "fee": FEE, "sl_slip": SL_SLIP, "stake_sol": STAKE_SOL, "stake_usd": stake,
           "sol_usd": sol_usd(arr["p_grad"]),
           "stats": {"all": stats(r[took]), "train": stats(r[took & train]), "test": stats(r[took & test])},
           "funnel": funnel, "study": study_rule(f, r, took, train, test, reason) if len(idx) else None,
           "equity": equity, "per_day": per_day,
           "trade_columns": ["mint", "symbol", "split", "entry_ts", "minutes_held", "exit", "net_return",
                             "pnl_usd", "entry_price", "price_multiple"] + measured,
           "trades": trades}
    return doc, total


def write_strategy_pages(out_dir, rules, tok, arr, vol, trd, train, test):
    """strategies/<id>.json for every enabled rule (the saved ones, the starter ones and the built-in ones): what the Lab page
    shows as its backtest. A rule that no longer exists leaves no page behind."""
    folder = os.path.join(out_dir, "strategies")
    os.makedirs(folder, exist_ok=True)
    for rule in rules:
        if not rule["enabled"]:
            continue
        doc, total = strategy_doc(rule, tok, arr, vol, trd, train, test)
        path = os.path.join(folder, rule["id"] + ".json")
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(clean(doc), fh, separators=(",", ":"))
        os.replace(path + ".tmp", path)
        print(f"strategy page '{rule['name']}': {len(doc['trades'])} trades, total {total:+.1f} stakes", flush=True)
    known = {r["id"] for r in rules}
    for f in os.listdir(folder):
        if f.endswith(".json") and not f.endswith(".live.json") and f[:-5] not in known:
            os.remove(os.path.join(folder, f))


def write_draft(out_dir, tok, arr, vol, trd, train, test):
    """The Lab's "Backtest without saving": the rule the page put in settings/draft.json, run on the same sample as the
    saved rules and written to draft-result.json. Nothing is added to the saved runs."""
    request = {}
    try:
        with open(os.path.join(out_dir, "settings", "draft.json"), encoding="utf-8") as f:
            request = json.load(f)
        rule = rulebook.validate([{**request["rule"], "id": "draft", "enabled": True}])[0]
        doc, total = strategy_doc(rule, tok, arr, vol, trd, train, test)
        result = {"requested": request.get("requested"), "doc": doc}
        print(f"draft '{rule['name']}': {len(doc['trades'])} trades, total {total:+.1f} stakes", flush=True)
    except (OSError, ValueError, KeyError, TypeError) as e:
        result = {"requested": request.get("requested") if isinstance(request, dict) else None, "error": str(e) or "Could not read the draft rule."}
    path = os.path.join(out_dir, "draft-result.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(clean(result), fh, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def write_study(path, tok, arr, vol, trd, test):
    """study.csv: every token that was tradable 10, 15, 20 or 30 minutes after graduation,
    what it looked like at that moment, and what happened next. One row per token and minute.
    This is the raw material for finding better rules by hand or in a spreadsheet.
    """
    gts = tok["graduated_ts"]
    with np.errstate(divide="ignore", invalid="ignore"):
        crash_bar = arr["close"] <= 0.3 * arr["open"]
    with open(path + ".tmp", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mint", "symbol", "graduated_utc", "group", "entry_minute", "cluster"] + list(FEATURES) + [
            "result_pct_hold_30min", "best_pct_within_30min", "worst_pct_within_30min",
            "collapsed_within_30min", "collapsed_within_60min", "result_pct_tp40_sl30_30min"])
        for e in (10, 15, 20, 30):
            f = features(tok, arr, vol, trd, e)
            lab = cluster_labels(f)
            hold30 = net_return(arr, trd, e, arr["close"][:, e + 29].astype(np.float64) / arr["close"][:, e - 1])
            std = net_return(arr, trd, e, simulate(arr, e, 0.4, 0.3, [30])[0][30])
            with np.errstate(divide="ignore", invalid="ignore"):
                p0 = arr["close"][:, e - 1].astype(np.float64)
                best = np.nanmax(arr["high"][:, e:e + 30], axis=1) / p0 - 1
                worst = np.nanmin(arr["low"][:, e:e + 30], axis=1) / p0 - 1
            for i in np.flatnonzero(np.isfinite(std)):
                w.writerow([tok["mint"][i], tok["symbol"][i],
                            time.strftime("%Y-%m-%d %H:%M", time.gmtime(gts[i] / 1000)),
                            "recent 30%" if test[i] else "earlier 70%", e, rulebook.CLUSTERS[lab[i]]["id"]]
                           + [round(float(f[k][i]), 6) if np.isfinite(f[k][i]) else "" for k in FEATURES]
                           + [round(float(hold30[i]) * 100, 2) if np.isfinite(hold30[i]) else "",
                              round(float(best[i]) * 100, 2),
                              round(float(worst[i]) * 100, 2), int(crash_bar[i, e:e + 30].any()),
                              int(crash_bar[i, e:e + 60].any()), round(float(std[i]) * 100, 2)])
    os.replace(path + ".tmp", path)


def write_history(db, path, user_rules):
    """history.json for the web app: profit or loss of every saved run, with each run's rule.

    Trades are included for the latest run of every saved rule and for the latest automatic
    search, so the page can show every round trip next to the rule that produced it.
    """
    runs = []
    studies = {}
    sol_price = None    # of the newest run that recorded one: runs are read newest first
    for rid, ts, coins, params, rule, verdict, kind, rule_id, rule_name, study in db.execute(
            "SELECT run_id, ran_ts, coins, params, picked_rule, verdict, kind, rule_id, rule_name, study "
            "FROM backtest_runs ORDER BY run_id DESC").fetchall():
        studies[rid] = json.loads(study) if study else None
        params = json.loads(params)
        stake = params.get("stake", STAKE)
        sol_price = sol_price or params.get("sol_usd")
        run = {"run_id": rid, "ran_ts": ts, "coins": coins, "model": params.get("model"),
               "stake_usd": stake, "rule": json.loads(rule) if rule else None, "verdict": verdict,
               "kind": kind or "search", "rule_id": rule_id,
               "rule_name": rule_name if kind == "rule" else "Automatic search (best rule found)"}
        for split in ("all", "train", "test"):
            n, total, wins, best, worst = db.execute(
                "SELECT COUNT(*), SUM(net_return), SUM(net_return > 0), MAX(net_return), MIN(net_return) "
                "FROM backtest_trades WHERE run_id = ? AND (? = 'all' OR split = ?)",
                (rid, split, split)).fetchone()
            run[split] = {"trades": n, "wins": wins or 0, "losses": n - (wins or 0),
                          "pnl_usd": (total or 0.0) * stake, "avg_return": total / n if n else None,
                          "best_usd": best * stake if n else None, "worst_usd": worst * stake if n else None}
        runs.append(run)
    # the latest run of each saved rule, in the order of the settings page, then the search
    def same(run, rule):
        got = run["rule"] or {}
        return (got.get("entry"), got.get("tp"), got.get("sl"), got.get("hold"), got.get("be") or 0.0, got.get("be_stop") or 0.0) == \
            (rule["entry"], rule["tp"], rule["sl"], rule["hold"], rule.get("be") or 0.0, rule.get("be_stop") or 0.0) and \
            [rulebook.cond_core(c) for c in got.get("conditions", [])] == \
            [rulebook.cond_core(c) for c in rule["conditions"]]

    current = []
    for rule in user_rules:
        # newest run made with the rule as it is defined now (it may have been edited and reverted)
        hit = next((r for r in runs if r["kind"] == "rule" and r["rule_id"] == rule["id"]
                    and same(r, rule)), None)
        if hit and rule["enabled"]:
            current.append(hit["run_id"])
    search = next((r for r in runs if r["kind"] == "search"), None)
    if search:
        current.append(search["run_id"])
    trades = {}
    by_id = {r["run_id"]: r for r in runs}
    study = {}
    for rid in current:
        run = by_id[rid]
        if run["kind"] != "rule":
            continue
        # the same rule on earlier, smaller samples: is it getting better or worse as data grows?
        rule_now = next(u for u in user_rules if u["id"] == run["rule_id"])
        earlier = [{"run_id": r["run_id"], "ran_ts": r["ran_ts"], "coins": r["coins"],
                    "trades": r["all"]["trades"], "avg_return": r["all"]["avg_return"],
                    "pnl_usd": r["all"]["pnl_usd"]}
                   for r in runs if r["kind"] == "rule" and r["rule_id"] == run["rule_id"]
                   and same(r, rule_now) and not (r["model"] or "").count("superseded")]
        study[str(rid)] = {**(studies.get(rid) or {}), "over_time": earlier[::-1][-12:]}
    csv_path = os.path.join(os.path.dirname(path), "roundtrips.csv")
    with open(csv_path + ".tmp", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rule", "run_id", "mint", "symbol", "group", "bought_utc", "minutes_held", "sold_by",
                    "result_pct", "profit_usd"] + list(FEATURES))
        for rid in current:
            run = by_id[rid]
            for mint, symbol, split, entry_ts, exit_min, reason, net, feats in db.execute(
                    "SELECT b.mint, k.symbol, b.split, b.entry_ts, b.exit_min, b.exit_reason, b.net_return, "
                    "b.features FROM backtest_trades b LEFT JOIN tokens k USING (mint) "
                    "WHERE b.run_id = ? ORDER BY b.entry_ts", (rid,)):
                feats = json.loads(feats) if feats else {}
                w.writerow([run["rule_name"], rid, mint, symbol, "recent 30%" if split == "test" else "earlier 70%",
                            time.strftime("%Y-%m-%d %H:%M", time.gmtime(entry_ts / 1000)),
                            exit_min - run["rule"]["entry"] + 1, reason, round(net * 100, 2),
                            round(net * run["stake_usd"], 2)] + [feats.get(k, "") for k in FEATURES])
    os.replace(csv_path + ".tmp", csv_path)
    for rid in current:
        run, rows = by_id[rid], []
        for mint, symbol, split, entry_ts, exit_min, reason, net, price, multiple in db.execute(
                "SELECT b.mint, k.symbol, b.split, b.entry_ts, b.exit_min, b.exit_reason, b.net_return, "
                "b.entry_price, b.price_multiple FROM backtest_trades b LEFT JOIN tokens k USING (mint) "
                "WHERE b.run_id = ? ORDER BY b.entry_ts DESC", (rid,)):
            held = exit_min - run["rule"]["entry"] + 1 if run["rule"] else None
            # the buy price, and the sell price as a multiple of it (before fees and price impact)
            rows.append([mint, symbol, split, entry_ts, held, reason, net, net * run["stake_usd"], price, multiple])
        trades[str(rid)] = rows
    doc = {"generated_at": int(time.time() * 1000), "runs": runs, "current": current, "study": study,
           "sol_usd": sol_price, "stake_sol": STAKE_SOL,
           "trade_columns": ["mint", "symbol", "split", "entry_ts", "minutes_held", "exit", "net_return", "pnl_usd",
                             "entry_price", "price_multiple"],
           "trades": trades}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean(doc), fh, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="data/pump.db")
    ap.add_argument("--out", default="data/report.json")
    ap.add_argument("--history-only", action="store_true",
                    help="rewrite history.json from the saved runs without running a new backtest")
    ap.add_argument("--rules-only", action="store_true",
                    help="backtest the saved rules only, without the automatic rule search")
    ap.add_argument("--draft", action="store_true",
                    help="backtest the rule in settings/draft.json (the Lab's Backtest without saving) and nothing else")
    args = ap.parse_args()
    db = sqlite3.connect(args.db, timeout=600)
    db.executescript(SCHEMA)
    migrate(db)
    history_path = os.path.join(os.path.dirname(args.out), "history.json")
    user_rules = rulebook.load(os.path.join(os.path.dirname(args.out), "settings"))
    gone = rulebook.removed(os.path.join(os.path.dirname(args.out), "settings"))      # built-in rules deleted from the Lab
    if args.history_only:
        write_history(db, history_path, user_rules)
        return

    tok, arr, vol, trd = load(db)
    n = len(tok["mint"])
    if n < 500:
        if args.draft:
            with open(os.path.join(os.path.dirname(args.out), "draft-result.json"), "w", encoding="utf-8") as fh:
                json.dump({"requested": json.load(open(os.path.join(os.path.dirname(args.out), "settings", "draft.json"))).get("requested"),
                           "error": f"only {n} coins have complete price history; at least 500 are needed to backtest"}, fh)
        raise SystemExit(f"only {n} coins with complete candles; not enough to backtest")
    split = int(n * TRAIN_SHARE)
    train = np.arange(n) < split
    test = ~train
    gts = tok["graduated_ts"]
    print(f"coins: {n} (train {split}, test {n - split})", flush=True)
    if args.draft:
        write_draft(os.path.dirname(args.out), tok, arr, vol, trd, train, test)
        return
    if args.rules_only:
        print(f"saved rules backtested: {run_rules(db, rulebook.with_extras(user_rules, gone), tok, arr, vol, trd, train, test)}", flush=True)
        write_history(db, history_path, user_rules)
        write_strategy_pages(os.path.dirname(args.out), rulebook.with_extras(user_rules, gone), tok, arr, vol, trd, train, test)
        write_study(os.path.join(os.path.dirname(args.out), "study.csv"), tok, arr, vol, trd, test)
        return

    report = {
        "generated_at": int(time.time() * 1000),
        "sample": {"coins": n, "train": split, "test": n - split, "from": int(gts[0]),
                   "train_to": int(gts[split - 1]), "to": int(gts[-1])},
        "assumptions": {
            "fee_per_side": FEE, "stake_usd": arr["stake"], "stake_sol": STAKE_SOL,
            "sol_usd": sol_usd(arr["p_grad"]), "stop_slippage": SL_SLIP,
            "max_impact": MAX_IMPACT, "grad_mcap_usd": arr["p_grad"] * 1e9,
            "min_mcap_usd": (arr["stake"] / MAX_IMPACT / POOL_TOKENS) ** 2 / arr["p_grad"] * 1e9,
        },
    }

    # 1. what the price does after graduation, with no rules at all
    g = arr["p_grad"]
    after = []
    for m in (5, 10, 15, 20, 30, 60, 120, 240):
        r = arr["close"][:, m] / g - 1
        after.append({"minute": m, "median": np.nanmedian(r), "share_up": np.nanmean(r > 0),
                      "share_down_90": np.nanmean(r <= -0.9)})
    tradable = []
    for e in ENTRIES:
        ok = np.isfinite(net_return(arr, trd, e, np.ones(n)))
        tradable.append({"entry": e, "share": ok.mean(), "per_day": ok.sum() / ((gts[-1] - gts[0]) / 864e5)})
    # sudden collapses: a minute that closes 70%+ below its open, after a tradable entry
    with np.errstate(divide="ignore", invalid="ignore"):
        crash_bar = arr["close"] <= 0.3 * arr["open"]
    crashes = []
    for e in ENTRIES:
        ok = np.isfinite(net_return(arr, trd, e, np.ones(n)))
        crashes.append({"entry": e, **{f"within_{w}": float(crash_bar[ok, e:e + w].any(axis=1).mean())
                                       for w in (30, 60, 240)}})
    report["market"] = {"after_graduation": after, "tradable": tradable, "crashes": crashes}
    report["early"] = early_window(tok, arr, vol, trd)

    # 2. simulate every entry / target / stop / time-stop once
    net = {}
    for e, tp, sl in itertools.product(ENTRIES, TPS, SLS):
        by_hold, _, _ = simulate(arr, e, tp, sl, HOLDS)
        for hold, gr in by_hold.items():
            net[(e, tp, sl, hold)] = net_return(arr, trd, e, gr).astype(np.float32)
    exits = list(itertools.product(TPS, SLS, HOLDS))
    print(f"simulated {len(net)} exit combinations", flush=True)
    report["clusters"] = cluster_study(tok, arr, vol, trd, net, train, test)

    unf = [(k, stats(r[train]), stats(r[test])) for k, r in net.items()]
    report["unfiltered"] = {
        "combos": len(unf),
        "share_positive_train": np.mean([s["mean"] > 0 for _, s, _ in unf]),
        "share_positive_test": np.mean([s["mean"] > 0 for _, _, s in unf]),
        "best": [{"entry": k[0], "tp": k[1], "sl": k[2], "hold": k[3], "train": s, "test": t}
                 for k, s, t in sorted(unf, key=lambda u: -u[1]["mean"])[:5]],
    }

    # 3. which entry conditions matter: quintiles under one middle-of-the-road exit
    base = (15, 0.6, 0.3, 60)
    f15, r15 = features(tok, arr, vol, trd, 15), net[base]
    # a condition known for only part of the coins (first trades still being collected)
    # would quietly shrink the sample to the days it covers, so it is left out until complete
    usable = {name for name in FEATURES if np.isfinite(f15[name]).mean() >= 0.98}
    print(f"conditions left out for incomplete data: {sorted(set(FEATURES) - usable)}", flush=True)
    quint = []
    for name, (label, kind) in FEATURES.items():
        x, ok = f15[name][train], np.isfinite(r15[train]) & np.isfinite(f15[name][train])
        if name not in usable:
            continue
        if kind == "flag":
            buckets = [{"from": v, "to": v, **stats(r15[train][ok & (x == v)])} for v in (0, 1)]
        else:
            edges = np.unique(np.quantile(x[ok], [0, .2, .4, .6, .8, 1]))
            buckets = []
            for lo, hi in zip(edges[:-1], edges[1:]):
                sel = ok & (x >= lo) & ((x < hi) | (hi == edges[-1]))
                buckets.append({"from": lo, "to": hi, **stats(r15[train][sel])})
        quint.append({"name": name, "label": label, "kind": kind, "buckets": buckets})
    report["conditions"] = {"entry": base[0], "tp": base[1], "sl": base[2], "hold": base[3],
                            "features": quint}

    # 4. rule search on train: one or two entry conditions x every exit
    #    score = lower end of a 90% range for the train mean, so thin lucky rules rank low
    rank_m = train & (np.arange(n) < int(split * RANK_SHARE))
    check_m = train & ~rank_m
    cands = []
    tested = positive = 0
    for e in ENTRIES:
        f = features(tok, arr, vol, trd, e)
        R = np.stack([net[(e, *x)][rank_m] for x in exits]).astype(np.float32)   # exits x coins
        V = np.isfinite(R)
        R0 = np.where(V, R, 0.0)
        R2, Vf = R0 * R0, V.astype(np.float32)
        rules = []
        base_ok = rank_m & np.isfinite(net[(e, *exits[0])])
        for name, (label, kind) in FEATURES.items():
            x = f[name]
            # known for nearly every coin at this entry minute: else a rule would quietly keep only the coins that have it;
            # the hour of day wraps around midnight, so a single cut on it is not a rule worth searching for
            if name not in usable or name in SEARCH_SKIP or np.isfinite(x[base_ok]).mean() < 0.98:
                continue
            if kind == "flag":
                rules.append(((name, "=", 1.0), x == 1))
                continue
            vals = x[base_ok & np.isfinite(x)]
            if not len(vals):
                continue
            for thr in sorted({float(v) for v in np.quantile(vals, QUANTILES)}):
                rules.append(((name, ">=", thr), x >= thr))
                rules.append(((name, "<=", thr), x <= thr))

        def score_all(combos):
            """(score, best exit index) of each combo (indices into `rules`) as arrays; -inf when too few trades."""
            nonlocal tested, positive
            sc, ex = np.full(len(combos), -np.inf), np.zeros(len(combos), dtype=np.int32)
            for i in range(0, len(combos), 400):
                chunk = combos[i:i + 400]
                M = np.stack([np.logical_and.reduce([rules[k][1] for k in combo])[rank_m] for combo in chunk]
                             ).astype(np.float32).T                              # coins x rules
                cnt, tot, sq = (np.asarray(Vf @ M, dtype=np.float64), np.asarray(R0 @ M, dtype=np.float64),
                                np.asarray(R2 @ M, dtype=np.float64))
                with np.errstate(divide="ignore", invalid="ignore"):
                    mean = tot / cnt
                    se = np.sqrt(np.maximum(sq / cnt - mean ** 2, 0) / cnt)
                    score = np.where(cnt >= MIN_TRADES, mean - 1.645 * se, -np.inf)
                tested += int((cnt >= MIN_TRADES).sum())
                positive += int(((cnt >= MIN_TRADES) & (mean > 0)).sum())
                best = score.argmax(axis=0)
                ex[i:i + len(chunk)], sc[i:i + len(chunk)] = best, score[best, np.arange(len(chunk))]
            return sc, ex

        feat = [r[0][0] for r in rules]
        combos = [(a,) for a in range(len(rules))] + [(a, b) for a, b in itertools.combinations(range(len(rules)), 2)
                                                      if feat[a] != feat[b]]
        sc, ex = score_all(combos)
        # three conditions: the best pairs, each with one more condition on another feature
        pair_rank = [j for j in np.argsort(-sc) if len(combos[j]) == 2 and np.isfinite(sc[j])][:BEAM]
        triples = sorted({tuple(sorted(combos[j] + (k,))) for j in pair_rank for k in range(len(rules))
                          if feat[k] not in (feat[combos[j][0]], feat[combos[j][1]])})
        sc3, ex3 = score_all(triples)
        combos, sc, ex = combos + triples, np.concatenate([sc, sc3]), np.concatenate([ex, ex3])
        # only the best of each entry minute can reach the checking step: the rest are not kept in memory
        keep = [j for j in np.argsort(-sc)[:CHECKED * 3] if np.isfinite(sc[j])]
        cands += [(float(sc[j]), e, tuple(rules[k] for k in combos[j]), exits[ex[j]]) for j in keep]
        del combos, sc, ex, triples
    cands.sort(key=lambda c: -c[0])
    print(f"rule sets tested on train: {tested}, entry rules kept: {len(cands)}", flush=True)

    def describe(combo):
        return [{"feature": c[0][0], "label": FEATURES[c[0][0]][0], "kind": FEATURES[c[0][0]][1],
                 "op": c[0][1], "value": c[0][2]} for c in combo]

    def mask_of(combo):
        return np.logical_and.reduce([c[1] for c in combo])

    def trim3(x):     # the mean without the 3 best trades: a rule carried by a lucky few does not count
        return float(np.sort(x)[:-3].mean()) if len(x) > 6 else float("nan")

    # checking: the best-ranked rule sets must also hold on the later train coins, without their 3 best trades,
    # and on most days; the pick is the one whose weaker part (ranking or checking) is best
    ranked, seen, checked = [], set(), 0
    day_of = gts // 86400000
    for score, e, combo, (tp, sl, hold) in cands:
        if checked == CHECKED:
            break
        r, m = net[(e, tp, sl, hold)], mask_of(combo)
        # different conditions that select exactly the same trades are one rule, not two
        key = (e, tp, sl, hold, np.packbits(m & train & np.isfinite(r)).tobytes())   # train only: test never decides
        if key in seen:
            continue
        seen.add(key)
        checked += 1
        rr, vv = r[m & rank_m], r[m & check_m]
        rr, vv = rr[np.isfinite(rr)], vv[np.isfinite(vv)]
        if len(vv) < MIN_CHECK_TRADES or not trim3(rr) > 0 or not vv.mean() > 0 or not trim3(vv) > 0:
            continue
        took = m & train & np.isfinite(r)
        days = np.unique(day_of[took])
        good = np.mean([r[took & (day_of == d)].sum() > 0 for d in days]) if len(days) else 0.0
        if good < MIN_DAY_SHARE:
            continue
        ranked.append({"entry": e, "conditions": describe(combo), "tp": tp, "sl": sl, "hold": hold,
                       "score": min(trim3(rr), trim3(vv)), "rank_score": score, "good_days": float(good),
                       "rank": stats(rr), "check": stats(vv), "train": stats(r[m & train]), "test": stats(r[m & test]),
                       "_combo": combo})
    ranked.sort(key=lambda x: -x["score"])
    ranked = ranked[:200]
    te = np.array([r["test"]["mean"] for r in ranked if r["test"]["trades"]], dtype=float)
    report["search"] = {
        "rule_sets_tested": tested, "positive_on_train": positive, "checked": checked, "kept_after_check": len(ranked),
        "top": [{k: v for k, v in r.items() if k != "_combo"} for r in ranked[:12]],
        "top200_share_positive_test": float((te > 0).mean()) if len(te) else None,
        "top200_median_test_mean": float(np.median(te)) if len(te) else None,
    }

    # 5. the pick is the best rule after checking, decided before looking at test
    run_params = {"model": MODEL, "entries": ENTRIES, "tps": TPS, "sls": SLS, "holds": HOLDS, "fee": FEE,
                  "sl_slip": SL_SLIP, "stake": arr["stake"], "stake_sol": STAKE_SOL,
                  "sol_usd": sol_usd(arr["p_grad"]), "max_impact": MAX_IMPACT,
                  "min_trades": MIN_TRADES, "train_share": TRAIN_SHARE, "rank_share": RANK_SHARE, "quantiles": QUANTILES,
                  "beam": BEAM, "checked": CHECKED, "min_check_trades": MIN_CHECK_TRADES, "min_day_share": MIN_DAY_SHARE}
    def detail(e, m, conditions, tp, sl, hold, score, train, test):
        """Everything the report shows about one rule: (the strategy block, its saved round
        trips, the returns of its test round trips, the low end of their plausible range)."""
        by_hold, reason, exit_min = simulate(arr, e, tp, sl, [hold])
        gr = by_hold[hold]
        r = net_return(arr, trd, e, gr)
        took = m & np.isfinite(r)
        s_tr, s_te = stats(r[m & train]), stats(r[m & test])
        te_r = r[took & test]
        rng = np.random.default_rng(0)
        boot = rng.choice(te_r, (5000, len(te_r))).mean(axis=1) if len(te_r) else np.array([np.nan])
        lo, hi = np.quantile(boot, [0.05, 0.95])
        idx = np.flatnonzero(took)
        day = (gts[idx] // 86400000) * 86400000
        per_day = [{"day": int(d), "trades": int((day == d).sum()), "mean": float(r[idx][day == d].mean()),
                    "total": float(r[idx][day == d].sum()), "is_test": bool(test[idx][day == d].all())}
                   for d in np.unique(day)]
        eq = np.cumsum(r[idx])
        step = max(1, len(idx) // 400)
        names = {1: "stop", 2: "target", 3: "time"}
        order = np.argsort(r[idx])
        ex = [int(idx[i]) for i in dict.fromkeys(list(order[:4]) + list(order[len(order) // 2 - 2:len(order) // 2 + 2])
                                                 + list(order[-4:]))]
        low = net_return(arr, trd, e, simulate(arr, e, tp, sl, [hold], "low")[0][hold])

        def near(t2, s2):   # the same entry rule with another target and stop
            key = (e, t2, s2, hold)
            x = net[key] if key in net else net_return(arr, trd, e, simulate(arr, e, t2, s2, [hold])[0][hold])
            return stats(x[m & test])["mean"]
        tps, sls = sorted(set(TPS) | {tp}), sorted(set(SLS) | {sl})
        block = {
            "entry": e, "conditions": conditions, "tp": tp, "sl": sl, "hold": hold,
            "train": s_tr, "test": s_te, "score": score,
            "test_range": [lo, hi],
            "test_without_top3": float(np.sort(te_r)[:-3].mean()) if len(te_r) > 6 else None,
            "worst_case_stops": {"train": stats(low[m & train]), "test": stats(low[m & test])},
            "trades_per_day": len(idx) / ((gts[-1] - gts[0]) / 864e5),
            "exit_mix": {names[k]: float((reason[idx] == k).mean()) if len(idx) else None for k in names},
            "exit_mean": {names[k]: float(r[idx][reason[idx] == k].mean()) if (reason[idx] == k).any() else None
                          for k in names},
            "per_day": per_day,
            "equity": [{"i": int(i + 1), "ts": int(gts[idx[i]] + e * MIN), "total": float(eq[i]),
                        "is_test": bool(test[idx[i]])} for i in list(range(0, len(idx), step)) + [len(idx) - 1]]
            if len(idx) else [],
            "max_drawdown": float((np.maximum.accumulate(eq) - eq).max()) if len(idx) else 0.0,
            "stakes": [{"stake": s, "train": stats(net_return(arr, trd, e, gr, s)[m & train]),
                        "test": stats(net_return(arr, trd, e, gr, s)[m & test])}
                       for s in [k * sol_usd(arr["p_grad"]) for k in (0.1, 0.2, 0.5, 1.0, 2.5, 5.0)]],
            "neighbours": {"tps": tps, "sls": sls, "test_mean": [[near(t2, s2) for s2 in sls] for t2 in tps]},
            "examples": [{"mint": tok["mint"][i], "symbol": tok["symbol"][i],
                          "entry_ts": int(gts[i] + e * MIN), "minutes_held": int(exit_min[i] - e + 1),
                          "reason": names[int(reason[i])], "net": float(r[i])} for i in ex],
        }
        p0 = arr["close"][:, e - 1]
        rows = [(tok["mint"][i], "test" if test[i] else "train", int(gts[i] + e * MIN),
                 float(p0[i]), int(exit_min[i]), names[int(reason[i])], float(gr[i]), float(r[i])) for i in idx]
        return block, rows, te_r, lo

    strategy, trades_rows, verdict = None, [], "no_rule"
    if ranked:
        top = ranked[0]
        strategy, trades_rows, te_r, lo = detail(top["entry"], mask_of(top["_combo"]), top["conditions"], top["tp"], top["sl"],
                                                 top["hold"], top["score"], train, test)
        strategy.update({"rank": top["rank"], "check": top["check"], "good_days": top["good_days"]})
        verdict = ("pass" if len(te_r) >= PASS_TEST_TRADES and lo > 0 and (strategy["test_without_top3"] or 0) > 0 else
                   "weak" if len(te_r) >= PASS_TEST_TRADES and strategy["test"]["mean"] > 0 else "fail")
    if strategy and strategy["train"]["mean"] <= 0:
        verdict = "none"     # even the best-ranked rule lost money on the data it was fitted to
    report["strategy"], report["verdict"] = strategy, verdict

    # 5b. a saved rule marked "Record live" takes the report's place of honour. It was written
    #     by hand after looking at this data, so it is shown on every recorded token, with no
    #     train/test split to pretend otherwise; the search's own pick is kept beside it.
    chosen = next((u for u in user_rules if u["enabled"] and u["live"]), None)
    if chosen:
        every, nobody = np.ones(n, dtype=bool), np.zeros(n, dtype=bool)
        f_c = features(tok, arr, vol, trd, chosen["entry"])
        at_c = features_at(tok, arr, vol, trd)
        shown = [{**c, "label": FEATURES[c["feature"]][0], "kind": FEATURES[c["feature"]][1]}
                 for c in chosen["conditions"]]
        block, _, all_r, lo = detail(chosen["entry"], matches(f_c, chosen["conditions"], at_c), shown, chosen["tp"],
                                     chosen["sl"], chosen["hold"], None, nobody, every)
        report["search_pick"] = {**{k: strategy[k] for k in ("entry", "conditions", "tp", "sl", "hold", "train", "test")},
                                 "verdict": verdict} if strategy else None
        report["featured"] = {"id": chosen["id"], "name": chosen["name"]}
        report["strategy"] = block
        report["verdict"] = ("rule_none" if not len(all_r) else "rule_loss" if block["test"]["mean"] <= 0 else
                             "rule_profit" if len(all_r) >= 40 and lo > 0 else "rule_thin")
        try:
            report["latency"] = latency_study(db, tok, arr, trd, matches(f_c, chosen["conditions"], at_c), chosen["entry"],
                                              chosen["tp"], chosen["sl"], chosen["hold"])
        except Exception as ex:      # pump.fun unreachable or answering oddly: the report goes out without it
            print(f"execution-delay replay skipped: {ex!r}", flush=True)

    # save the run so later runs can be compared against it
    cur = db.execute(
        "INSERT INTO backtest_runs (ran_ts, coins, train_coins, test_coins, sample_from_ts, "
        "sample_to_ts, params, picked_rule, train_stats, test_stats, verdict, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'search')",
        (report["generated_at"], n, split, n - split, int(gts[0]), int(gts[-1]), json.dumps(run_params),
         json.dumps(clean({k: strategy[k] for k in ("entry", "conditions", "tp", "sl", "hold")}))
         if strategy else None,
         json.dumps(clean(strategy["train"])) if strategy else None,
         json.dumps(clean(strategy["test"])) if strategy else None, verdict))
    run_id = cur.lastrowid
    db.executemany(
        "INSERT INTO backtest_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(run_id, i + 1, r["entry"], json.dumps(clean(r["conditions"])), r["tp"], r["sl"], r["hold"],
          r["score"], r["train"]["trades"], r["train"]["mean"], r["train"]["win"],
          r["test"]["trades"], r["test"]["mean"], r["test"]["win"]) for i, r in enumerate(ranked)])
    db.executemany("INSERT INTO backtest_trades (run_id, mint, split, entry_ts, entry_price, exit_min, "
                   "exit_reason, price_multiple, net_return) VALUES (?,?,?,?,?,?,?,?,?)",
                   [(run_id, *t) for t in trades_rows])
    db.commit()
    report["run_id"] = run_id
    report["history"] = [
        {"run_id": rid, "ran_ts": ts, "coins": c, "model": json.loads(params).get("model"),
         "rule": json.loads(rule) if rule else None,
         "train": json.loads(tr) if tr else None, "test": json.loads(te_) if te_ else None, "verdict": v}
        for rid, ts, c, params, rule, tr, te_, v in db.execute(
            "SELECT run_id, ran_ts, coins, params, picked_rule, train_stats, test_stats, verdict "
            "FROM backtest_runs WHERE kind = 'search' ORDER BY run_id DESC LIMIT 20")]

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean(report), fh, separators=(",", ":"))
    os.replace(tmp, args.out)
    print(f"saved rules backtested: {run_rules(db, rulebook.with_extras(user_rules, gone), tok, arr, vol, trd, train, test)}", flush=True)
    write_history(db, history_path, user_rules)
    write_strategy_pages(os.path.dirname(args.out), rulebook.with_extras(user_rules, gone), tok, arr, vol, trd, train, test)
    write_study(os.path.join(os.path.dirname(args.out), "study.csv"), tok, arr, vol, trd, test)
    print(f"run {run_id}: verdict {verdict}; wrote {args.out}", flush=True)
    if strategy:
        print(json.dumps(clean({k: strategy[k] for k in ("entry", "conditions", "tp", "sl", "hold",
                                                         "train", "test", "test_range")}), indent=1))


if __name__ == "__main__":
    main()

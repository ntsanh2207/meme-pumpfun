#!/usr/bin/env python3
"""Pattern Discover: look for price patterns of the first minutes after graduation, backtest them, and keep the profitable ones on a watch list that is
backtested again with every run (and with the coins that graduated after the pattern was found).

It is the pattern-discovery skill (.claude/skills/pattern-discovery) as a program that runs by itself every 2 hours (pump-graduated-patterns.timer, or
when the Pattern Discover page asks):

 1. Seeds: coins that closed their graduation minute between $30.0K and $50.0K and peaked above $90K within 30 minutes of graduation, taken from the
    EARLIER 70% of the saved coins only (graduation order), so what is learned from them can be tested on the newest 30%.
 2. Candidates: patterns whose numbers come from the seeds (their quantiles), in two families, each tried with three exits:
      cross: the coin's first crossing (analyze.crossing_minute: its close goes back above the high of the graduation minute) after a quiet start
             (minute 0 closes at a share of its high; minutes 1..Q close at most x of that high, with a high not above it, a body of at most y of it,
             a low not under $z); bought at the close of the crossing minute;
      jump:  after Q quiet minutes (mean candle range at most r) the first minute that closes at least j above the previous close (optionally while the
             previous close was still below the high of minute 0); bought at its close.
 3. Backtest: every candidate on every saved coin with the backtest's model (fees, price impact, stop slippage: analyze.simulate / net_return), split
    into the earlier 70% and the newest 30%. A candidate is PROFITABLE when it has enough trades and is positive overall, on the earlier 70%, on the
    newest 30% and without its 3 best trades. Candidates that select nearly the same coins count once.
 4. Watch list: every profitable pattern is kept (data/settings/patterns.json) and backtested again at each run on all the data, with its own record of the
    coins that graduated after it was found ("since found"): testing (under 10 such trades), confirmed (positive) or failing (not).

Hundreds of candidates are tried per run, so some pass by luck: the page says how many were tried, and only the "since found" record is a real test.
Writes data/patterns.json (the page). Usage: python3 patterns.py --data data [--db data/pump.db]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import numpy as np

import analyze as A

EXITS = {"A": (0.35, 0.50, 10), "B": (1.00, 0.40, 30), "C": (0.50, 0.50, 15)}      # take profit, stop loss, minutes held
M0_LO, M0_HI = 35000.0, 50000.0         # the coins the patterns are tested on: the graduation minute closes between these (market cap, USD)
SEED_M0_LO, SEED_M0_HI = 30000.0, 50000.0   # a seed closes its graduation minute between these (22 Oct 2026: it was $35K-$50K)
SEED_PEAK, SEED_MINUTES = 90000.0, 30   # a seed peaked above this within this many minutes of graduation (it was $100K within 60)
MIN_TRADES, MIN_EARLIER, MIN_NEWEST = 25, 10, 10
MIN_MEAN = 0.07                          # a profitable pattern averages more than this per trade (7%, since 22 Oct 2026)
MAX_WATCH = 60                           # patterns kept on the watch list
FORWARD_MIN = 10                         # trades since found before a pattern is called confirmed or failing
SAME_COINS = 0.8                         # two candidates that share this much of their coins count once
HISTORY_KEEP = 60
INTERVAL_S = 7200
DAILY_S = 8 * 3600                       # the daily discovery runs this often
DAILY_WINDOW_MS = 24 * 3600 * 1000       # ...on the coins of this many hours before the newest saved coin
DAILY_MIN_SEEDS = 8


def now_ms():
    return int(time.time() * 1000)


def write_json(path, doc):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=os.path.basename(path) + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(A.clean(doc), f, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Data:
    """Every saved coin as arrays, with what the candidates need computed once."""

    def __init__(self, db):
        self.tok, self.arr, self.vol, self.trd = A.load(db)
        tok, arr = self.tok, self.arr
        self.n = n = len(tok["mint"])
        self.gts = tok["graduated_ts"]
        self.train = np.arange(n) < int(n * A.TRAIN_SHARE)
        self.C, self.L, self.H, self.O = (arr[k].astype(np.float64) for k in ("close", "low", "high", "open"))
        self.h0 = self.H[:, 0]
        c0 = self.C[:, 0] * 1e9
        self.uni = np.isfinite(self.h0) & (self.h0 > 0) & (c0 > M0_LO) & (c0 <= M0_HI)
        self.m0r = np.where(np.isfinite(self.h0) & (self.h0 > 0), self.C[:, 0] / self.h0, np.nan)          # minute 0's close as a share of its high (the level)
        self.cm = A.first_crossings(arr)                                          # first crossing minute (-1: none)
        peak = np.nanmax(self.H[:, :SEED_MINUTES], axis=1) * 1e9
        self.seeds = np.isfinite(self.h0) & (self.h0 > 0) & (c0 > SEED_M0_LO) & (c0 <= SEED_M0_HI) & (peak > SEED_PEAK) & self.train
        with np.errstate(invalid="ignore", divide="ignore"):
            self.rng = (self.H - self.L) / np.where(self.C > 0, self.C, np.nan)  # each minute's high-low range as a share of its close
            self.jump = self.C[:, 1:] / self.C[:, :-1] - 1                        # jump[:, m-1]: minute m's close against the previous close
        self._quiet, self._range, self._ret = {}, {}, {}

    def quiet(self, q):
        """Minutes 1..q against the level: (max close share, max high share, max body share, lowest low in USD, all bars known)."""
        if q not in self._quiet:
            s = slice(1, q + 1)
            fin = np.isfinite(np.stack([self.C[:, s], self.H[:, s], self.L[:, s], self.O[:, s]])).all(axis=(0, 2))
            with np.errstate(invalid="ignore"):
                self._quiet[q] = (np.nanmax(self.C[:, s], axis=1) / self.h0, np.nanmax(self.H[:, s], axis=1) / self.h0,
                                  np.nanmax(np.abs(self.C[:, s] - self.O[:, s]), axis=1) / self.h0, np.nanmin(self.L[:, s], axis=1) * 1e9, fin)
        return self._quiet[q]

    def range_before(self, q, m):
        """Mean candle range of the q minutes before minute m (NaN when a bar is missing)."""
        if (q, m) not in self._range:
            self._range[(q, m)] = self.rng[:, m - q:m].mean(axis=1)
        return self._range[(q, m)]

    def returns(self, ex, e):
        """Net return of buying every coin at entry minute e (the close of minute e-1) with exit ex."""
        if (ex, e) not in self._ret:
            tp, sl, hold = EXITS[ex]
            g, _, _ = A.simulate(self.arr, e, tp, sl, [hold])
            self._ret[(ex, e)] = A.net_return(self.arr, self.trd, e, g[hold])
        return self._ret[(ex, e)]


class Lean(Data):
    """A pattern's view of the first minutes of every coin of `arr` (the Lab, the backtest and the live watch evaluate a pattern added to the Lab with this):
    only minutes 0 to 13 are kept, so it is small, and nothing is loaded from a database."""

    def __init__(self, arr, vol=None, cols=14):
        def cut(k):
            x = np.asarray(arr[k][:, :cols], dtype=np.float64)
            return x if x.shape[1] == cols else np.concatenate([x, np.full((x.shape[0], cols - x.shape[1]), np.nan)], axis=1)
        self.C, self.L, self.H, self.O = cut("close"), cut("low"), cut("high"), cut("open")
        self.V = np.full(self.C.shape, np.nan) if vol is None else cut_vol(vol, cols)
        self.arr = {"close": self.C, "low": self.L, "high": self.H, "open": self.O}
        self.n = self.C.shape[0]
        self.h0 = self.H[:, 0]
        c0 = self.C[:, 0] * 1e9
        self.uni = np.isfinite(self.h0) & (self.h0 > 0) & (c0 > M0_LO) & (c0 <= M0_HI)
        self.m0r = np.where(np.isfinite(self.h0) & (self.h0 > 0), self.C[:, 0] / self.h0, np.nan)
        self.cm = A.first_crossings(self.arr)
        with np.errstate(invalid="ignore", divide="ignore"):
            self.rng = (self.H - self.L) / np.where(self.C > 0, self.C, np.nan)
            self.jump = self.C[:, 1:] / self.C[:, :-1] - 1
        self._quiet, self._range, self._ret, self._chosen = {}, {}, {}, {}

    def chosen(self, spec):
        key = spec_id(spec)
        if key not in self._chosen:
            self._chosen[key] = chosen_minutes(spec, self)
        return self._chosen[key]


def lab_name(name):
    """The Lab's name (60 characters at most) keeps the end of the pattern's name, which is its exit."""
    head, _, ex = name.rpartition(" · ")
    full = "Pattern · " + name
    return full if len(full) <= 60 else ("Pattern · " + head)[:60 - len(ex) - 3] + " · " + ex


def lab_rule(p):
    """A watch-list pattern as a Lab rule: it buys at the first minute from the earliest possible one to 13 at which the pattern fired in the minute
    before (the feature `pattern:<id>`, see analyze.pattern_features), with the pattern's exit."""
    spec = p["spec"]
    tp, sl, hold = EXITS[spec["exit"]]
    return {"id": "pattern-" + p["id"], "name": lab_name(p["name"]), "enabled": True, "entry": spec["Q"] + 2, "entry_to": A.CROSS_LAST + 1,
            "conditions": [{"feature": "pattern:" + p["id"], "op": "=", "value": 1.0}], "tp": tp, "sl": sl, "hold": hold}


def cut_vol(vol, cols):
    x = np.asarray(vol[:, :cols], dtype=np.float64)
    return x if x.shape[1] == cols else np.concatenate([x, np.full((x.shape[0], cols - x.shape[1]), np.nan)], axis=1)


def chosen_minutes(spec, D):
    """The minute each coin is bought at (the close of that minute), or -1: the pattern's trigger."""
    n = D.n
    out = np.full(n, -1, dtype=np.int16)
    m0 = D.uni.copy()
    if spec.get("m0"):
        with np.errstate(invalid="ignore"):
            m0 &= (D.m0r >= spec["m0"][0]) & (D.m0r <= spec["m0"][1])
    if spec["family"] == "quiet":
        # the Lab's Strategies H and H+: minute 0 closes in a market-cap range at a share of its high, minutes 1..Q stay quiet (closes, high, body, low,
        # volume against minute 0, every bar's high-low distance in USD); the trigger is minute Q, so the entry is minute Q + 1
        q = spec["Q"]
        cmax, hmax, bmax, lmin, fin = D.quiet(q)
        s = slice(1, q + 1)
        c0 = D.C[:, 0] * 1e9
        with np.errstate(invalid="ignore", divide="ignore"):
            ok = (np.isfinite(D.h0) & (D.h0 > 0) & (c0 > spec["m0_close"][0]) & (c0 <= spec["m0_close"][1]) & (D.m0r >= spec["m0"][0]) & (D.m0r <= spec["m0"][1])
                  & fin & (cmax <= spec["close_max"]) & (hmax <= spec["high_max"]) & (bmax <= spec["body_max"]) & (lmin >= spec["low_min"])
                  & (D.V[:, s].mean(axis=1) <= spec["vol_ratio"] * D.V[:, 0]) & (np.nanmax(D.H[:, s] - D.L[:, s], axis=1) * 1e9 <= spec["range_max"])
                  & (np.nanmin(D.C[:, s], axis=1) / D.h0 >= spec["close_min"]))
        out[ok] = q
        return out
    if spec["family"] == "cross":
        cmax, hmax, bmax, lmin, fin = D.quiet(spec["Q"])
        with np.errstate(invalid="ignore"):
            ok = (m0 & fin & (cmax <= spec["close_max"]) & (hmax <= spec["high_max"]) & (bmax <= spec["body_max"]) & (lmin >= spec["low_min"])
                  & (D.cm > spec["Q"]) & (D.cm <= A.CROSS_LAST))
        out[ok] = D.cm[ok]
    else:
        q = spec["Q"]
        for m in range(max(4, q + 1), A.CROSS_LAST + 1):
            with np.errstate(invalid="ignore"):
                ok = m0 & (out < 0) & (D.range_before(q, m) <= spec["range_max"]) & (D.jump[:, m - 1] >= spec["jump_min"])
                if spec.get("from_below"):
                    ok &= D.C[:, m - 1] < D.h0
            out[ok] = m
    return out


def trades_of(spec, D):
    """(minute per coin, net return per coin: NaN where the pattern does not trade)."""
    minute = chosen_minutes(spec, D)
    r = np.full(D.n, np.nan)
    for m in np.unique(minute[minute >= 0]):
        sel = minute == m
        r[sel] = D.returns(spec["exit"], int(m) + 1)[sel]
    return minute, r


def stats_of(r, mask, D):
    m = mask & np.isfinite(r)
    x = r[m]
    if not len(x):
        return {"trades": 0}
    rng = np.random.default_rng(0)
    boot = np.array([rng.choice(x, len(x)).mean() for _ in range(1500)]) if len(x) >= 5 else np.array([np.nan])
    return {"trades": int(len(x)), "mean": float(x.mean()), "won": float((x > 0).mean()), "total": float(x.sum()),
            "wo3": float(np.sort(x)[:-3].mean()) if len(x) > 3 else None,
            "earlier": {"trades": int((m & D.train).sum()), "mean": float(r[m & D.train].mean()) if (m & D.train).any() else None},
            "newest": {"trades": int((m & ~D.train).sum()), "mean": float(r[m & ~D.train].mean()) if (m & ~D.train).any() else None},
            "boot_lo": float(np.percentile(boot, 5)), "boot_hi": float(np.percentile(boot, 95))}


def passes(st):
    return (st.get("trades", 0) >= MIN_TRADES and st["earlier"]["trades"] >= MIN_EARLIER and st["newest"]["trades"] >= MIN_NEWEST
            and st["mean"] > MIN_MEAN and (st["earlier"]["mean"] or 0) > 0 and (st["newest"]["mean"] or 0) > 0 and (st["wo3"] or 0) > 0)


def spec_id(spec):
    spec = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in spec.items()}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]


def describe(spec):
    tp, sl, hold = EXITS[spec["exit"]]
    exit_text = f"sell at +{tp:.0%}, -{sl:.0%} or after {hold} min"
    band = f"minute 0 closes at {spec['m0'][0]:.0%} to {spec['m0'][1]:.0%} of its high" if spec.get("m0") else "any minute 0 close in $35K-$50K"
    if spec["family"] == "cross":
        return (f"Crossing after a quiet start: {band}; minutes 1-{spec['Q']} close at most {spec['close_max']:.0%} of the level with a high of at most {spec['high_max']:.0%}, "
                f"a body of at most {spec['body_max']:.0%} and a low of at least ${spec['low_min'] / 1000:.0f}K; buy at the close of the first crossing; {exit_text}")
    return (f"Jump after a quiet start: {band}; {spec['Q']} minutes with a mean candle range of at most {spec['range_max']:.0%}, then the first minute that closes at least "
            f"{spec['jump_min']:.0%} above the previous close{' while that close was below the level' if spec.get('from_below') else ''}; buy at its close; {exit_text}")


def short_name(spec):
    tp, sl, hold = EXITS[spec["exit"]]
    ex = f"+{tp:.0%}/-{sl:.0%}/{hold}m"
    if spec["family"] == "cross":
        return f"Cross · quiet 1-{spec['Q']} · close ≤{spec['close_max']:.0%} · body ≤{spec['body_max']:.0%} · low ≥${spec['low_min'] / 1000:.0f}K · {ex}"
    return f"Jump ≥{spec['jump_min']:.0%} · {spec['Q']} quiet min (range ≤{spec['range_max']:.0%}){' · from below' if spec.get('from_below') else ''} · {ex}"


# found by hand with the pattern-discovery skill (22 Oct 2026): always tried, so the page tracks them with the others
HAND = [
    {"family": "jump", "m0": None, "Q": 4, "range_max": 0.07, "jump_min": 0.15, "from_below": False, "exit": "A"},
    {"family": "jump", "m0": None, "Q": 4, "range_max": 0.07, "jump_min": 0.15, "from_below": False, "exit": "B"},
]


def candidates(D, seeds=None, min_seeds=20, hand=True):
    """Patterns whose numbers come from the seeds (the earlier 70% of the coins that peaked above $100K): the quantiles of what they looked like."""
    s = D.seeds if seeds is None else seeds
    out = []
    if s.sum() < min_seeds:
        return list(HAND) if hand else []
    r_ = lambda v, step=0.01: float(np.ceil(v / step) * step)       # noqa: E731  (round up: the seeds' own value must still pass)
    m0r = D.m0r[s]
    bands = {(round(float(np.quantile(m0r, a)), 2), round(float(np.quantile(m0r, b)), 2)) for a, b in ((0.10, 0.90), (0.05, 0.95), (0.20, 0.95))}
    for q in (3, 4, 5):
        cmax, hmax, bmax, lmin, fin = D.quiet(q)
        ok = s & fin
        if ok.sum() < min_seeds:
            continue
        closes = sorted({round(min(1.0, r_(float(np.quantile(cmax[ok], p)))), 2) for p in (0.70, 0.85, 0.95)})
        bodies = sorted({round(r_(float(np.quantile(bmax[ok], p)), 0.05), 2) for p in (0.70, 0.90)})
        lows = sorted({float(np.floor(np.quantile(lmin[ok], p) / 1000) * 1000) for p in (0.05, 0.20)})
        highs = sorted({1.0, round(min(1.0, r_(float(np.quantile(hmax[ok], 0.80)))), 2)})
        for band in bands:
            for c in closes:
                for b in bodies:
                    for lo in lows:
                        for h in highs:
                            for ex in EXITS:
                                out.append({"family": "cross", "m0": list(band), "Q": q, "close_max": c, "high_max": h, "body_max": b, "low_min": lo, "exit": ex})
        ranges = []
        for m in range(max(4, q + 1), A.CROSS_LAST + 1):
            ranges.append(D.range_before(q, m)[s])
        rv = np.concatenate([x[np.isfinite(x)] for x in ranges]) if ranges else np.array([])
        if len(rv) >= min_seeds:
            for p in (0.25, 0.50, 0.75):
                rmax = round(float(np.quantile(rv, p)), 2)
                for j in (0.15, 0.25, 0.35):
                    for fb in (False, True):
                        for ex in EXITS:
                            out.append({"family": "jump", "m0": None, "Q": q, "range_max": rmax, "jump_min": j, "from_below": fb, "exit": ex})
    seen, uniq = set(), []
    for spec in (HAND if hand else []) + out:
        key = spec_id(spec)
        if key not in seen:
            seen.add(key)
            uniq.append(spec)
    return uniq


def search(D, cands, mask):
    """Backtest every candidate on the coins in `mask`; the profitable ones, the best of each group of near-identical ones: [(spec, stats, selected, minute, r)]. Also the number that passed."""
    found = []
    for spec in cands:
        minute, r = trades_of(spec, D)
        st = stats_of(r, (minute >= 0) & mask, D)
        if passes(st):
            found.append((min(st["earlier"]["mean"], st["newest"]["mean"]), spec, st, minute, r))
    found.sort(key=lambda t: -t[0])
    kept = []
    for score, spec, st, minute, r in found:
        sel = (minute >= 0) & mask & np.isfinite(r)
        if any((sel & k[2]).sum() / max((sel | k[2]).sum(), 1) >= SAME_COINS for k in kept):
            continue
        kept.append((spec, st, sel, minute, r))
    return kept, len(found)


def daily_discovery(D, data_to):
    """The pattern-discovery skill on one day of coins: the seeds are the coins of the last 24 hours (before the newest saved coin) that closed minute 0 at
    $30K-$50K and peaked above $90K within 30 minutes; the candidates' numbers come from them, and each candidate is tested on every OTHER saved coin (the days
    before), so the day's coins are never part of the test. Returns (summary for the page, kept patterns)."""
    t0 = time.time()
    day = D.gts >= data_to - DAILY_WINDOW_MS
    c0 = D.C[:, 0] * 1e9
    with np.errstate(invalid="ignore"):
        peak = np.nanmax(D.H[:, :SEED_MINUTES], axis=1) * 1e9
    seeds = day & np.isfinite(D.h0) & (D.h0 > 0) & (c0 > SEED_M0_LO) & (c0 <= SEED_M0_HI) & (peak > SEED_PEAK)
    info = {"ts": now_ms(), "from_ts": int(data_to - DAILY_WINDOW_MS), "to_ts": int(data_to), "coins": int(day.sum()), "near_standard_price": int((day & D.uni).sum()),
            "seeds": int(seeds.sum()), "candidates": 0, "passed": 0, "distinct": 0, "tested_on": int((~day).sum())}
    if seeds.sum() < DAILY_MIN_SEEDS:
        info["note"] = f"only {int(seeds.sum())} seeds in the day (needs {DAILY_MIN_SEEDS})"
        info["seconds"] = round(time.time() - t0, 1)
        return info, []
    cands = candidates(D, seeds=seeds, min_seeds=DAILY_MIN_SEEDS, hand=False)
    kept, n_found = search(D, cands, ~day)
    info.update(candidates=len(cands), passed=n_found, distinct=len(kept))
    # what the day's own coins did under each kept pattern (in sample: the seeds shaped it)
    best = []
    for spec, st, sel, minute, r in kept:
        sd = (minute >= 0) & day & np.isfinite(r)
        best.append({"name": short_name(spec), "tested": {k: st[k] for k in ("trades", "mean", "won", "wo3")},
                     "day_trades": int(sd.sum()), "day_mean": float(r[sd].mean()) if sd.any() else None})
    info["patterns"] = best[:20]
    info["seconds"] = round(time.time() - t0, 1)
    return info, kept


def coin_rows(minute, r, D, pool, limit=40):
    rows = []
    for i in np.flatnonzero(minute >= 0)[::-1][:limit]:
        p = pool.get(D.tok["mint"][i])
        rows.append({"symbol": D.tok["symbol"][i], "mint": D.tok["mint"][i], "graduated_ts": int(D.gts[i]), "minute": int(minute[i]) + 1,
                     "net": None if not np.isfinite(r[i]) else float(r[i]), "dex": f"https://dexscreener.com/solana/{p or D.tok['mint'][i]}"})
    return rows


def forward_of(r, minute, D, since):
    """The pattern's record on the coins that graduated after it was found."""
    m = (minute >= 0) & np.isfinite(r) & (D.gts > since)
    x = r[m]
    return {"trades": int(len(x)), "mean": float(x.mean()) if len(x) else None, "won": int((x > 0).sum()), "since": since}


def status_of(entry, st, fwd):
    if st.get("trades", 0) >= MIN_TRADES and st["mean"] <= MIN_MEAN:
        return "retired"
    if fwd["trades"] < FORWARD_MIN:
        return "testing"
    return "confirmed" if (fwd["mean"] or 0) > 0 else "failing"


def run(data_dir, db_path, force=False):
    t0 = time.time()
    settings = os.path.join(data_dir, "settings")
    store_path = os.path.join(settings, "patterns.json")
    store = read_json(store_path, {"patterns": {}, "runs": []})
    removed = set(read_json(os.path.join(settings, "patterns_removed.json"), []))
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    D = Data(db)
    pool = {m: p for m, p in db.execute("SELECT mint, pump_swap_pool FROM tokens")}
    db.close()
    data_to = int(D.gts.max()) if D.n else 0
    cands = candidates(D)
    print(f"{D.n} coins, {int(D.uni.sum())} near the standard price, {int(D.seeds.sum())} seeds; {len(cands)} candidates", flush=True)
    kept, n_found = search(D, cands, np.ones(D.n, bool))
    found = range(n_found)
    patterns = store.get("patterns", {})
    # known patterns that this run's search did not rediscover are still backtested again below: the watch list owns them
    new_ids = []
    daily, n_main = None, None
    daily_prev = store.get("daily") or {}
    if force or now_ms() - (daily_prev.get("ts") or 0) >= DAILY_S * 1000 - 120000:
        daily, dkept = daily_discovery(D, data_to)
        n_main = len(kept)
        kept = kept + [(spec, st, sel, minute, r, "daily") for spec, st, sel, minute, r in dkept]
    else:
        daily = daily_prev
    for item in kept:
        spec, st, sel, minute, r = item[:5]
        origin = item[5] if len(item) > 5 else ("hand" if spec in HAND else "seeds")
        pid = spec_id(spec)
        if pid in removed or pid in patterns or pid in {x["id"] for x in store.get("retired", [])}:     # (a retired pattern is not found again as new)
            continue
        # a new pattern that trades the same coins as one already watched is the same pattern
        dup = False
        for q in patterns.values():
            mq, rq = trades_of(q["spec"], D)
            sq = (mq >= 0) & np.isfinite(rq)
            if (sel & sq).sum() / max((sel | sq).sum(), 1) >= SAME_COINS:
                dup = True
                break
        if dup:
            continue
        patterns[pid] = {"id": pid, "spec": spec, "name": short_name(spec), "text": describe(spec), "found_ts": now_ms(), "data_to_ts": data_to, "origin": origin,
                         "found": {k: st[k] for k in ("trades", "mean", "won", "wo3", "boot_lo", "boot_hi", "earlier", "newest")}, "history": []}
        new_ids.append(pid)
    # every pattern on the watch list, backtested again on all the data
    rows, retired = [], store.get("retired", [])
    for pid in list(patterns):
        if pid in removed:
            patterns.pop(pid)
            continue
        p = patterns[pid]
        minute, r = trades_of(p["spec"], D)
        st = stats_of(r, minute >= 0, D)
        fwd = forward_of(r, minute, D, p["data_to_ts"])
        status = status_of(p, st, fwd) if st.get("trades") else "testing"
        p["history"] = (p.get("history", []) + [{"ts": now_ms(), "trades": st.get("trades", 0), "mean": st.get("mean"), "earlier": (st.get("earlier") or {}).get("mean"),
                                                 "newest": (st.get("newest") or {}).get("mean"), "fwd_trades": fwd["trades"], "fwd_mean": fwd["mean"]}])[-HISTORY_KEEP:]
        p["status"] = status
        p["now"] = {k: st.get(k) for k in ("trades", "mean", "won", "wo3", "boot_lo", "boot_hi", "earlier", "newest")}
        p["forward"] = fwd
        p["coins"] = coin_rows(minute, r, D, pool)
        p["lab_rule"] = lab_rule(p)
        if status == "retired":
            retired = [x for x in retired if x["id"] != pid] + [{k: p[k] for k in ("id", "name", "text", "found_ts", "found", "now", "history")}]
            patterns.pop(pid)
            continue
        rows.append(p)
    # a full watch list drops the pattern with the weakest record since it was found
    if len(rows) > MAX_WATCH:
        rows.sort(key=lambda p: (p["status"] == "confirmed", p["forward"]["mean"] if p["forward"]["trades"] >= FORWARD_MIN else 0.0), reverse=True)
        for p in rows[MAX_WATCH:]:
            patterns.pop(p["id"], None)
        rows = rows[:MAX_WATCH]
    order = {"confirmed": 0, "testing": 1, "failing": 2}
    rows.sort(key=lambda p: (order.get(p["status"], 3), -(p["now"].get("mean") or 0)))
    seed_idx = np.flatnonzero(D.seeds)
    sample = np.random.default_rng(int(time.time() // INTERVAL_S)).permutation(seed_idx)[:12]
    run_info = {"ts": now_ms(), "seconds": round(time.time() - t0, 1), "coins": D.n, "near_standard_price": int(D.uni.sum()), "seeds": int(D.seeds.sum()),
                "candidates": len(cands), "passed": len(found), "distinct": n_main if n_main is not None else len(kept), "new": len(new_ids), "data_to_ts": data_to}
    store = {"patterns": patterns, "daily": daily, "runs": (store.get("runs", []) + [run_info])[-30:], "retired": retired[-40:]}
    write_json(store_path, store)
    write_json(os.path.join(data_dir, "patterns.json"), {
        "updated": now_ms(), "interval_s": INTERVAL_S, "daily": daily, "daily_s": DAILY_S, "last_run": run_info, "runs": store["runs"],
        "criteria": {"m0": [M0_LO, M0_HI], "seed_m0": [SEED_M0_LO, SEED_M0_HI], "seed_peak": SEED_PEAK, "seed_minutes": SEED_MINUTES, "min_trades": MIN_TRADES, "min_mean": MIN_MEAN, "min_earlier": MIN_EARLIER, "min_newest": MIN_NEWEST,
                     "forward_min": FORWARD_MIN, "exits": {k: list(v) for k, v in EXITS.items()}},
        "seeds": [{"symbol": D.tok["symbol"][i], "mint": D.tok["mint"][i], "graduated_ts": int(D.gts[i]), "peak": float(np.nanmax(D.H[i, :SEED_MINUTES]) * 1e9),
                   "dex": f"https://dexscreener.com/solana/{pool.get(D.tok['mint'][i]) or D.tok['mint'][i]}"} for i in sample],
        "patterns": [{k: v for k, v in p.items()} for p in rows], "retired": store["retired"], "new_ids": new_ids})
    print(f"profitable candidates {len(found)} ({len(kept)} distinct), new {len(new_ids)}, watch list {len(rows)}, {run_info['seconds']} s", flush=True)
    return run_info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--db", default=None)
    ap.add_argument("--force", action="store_true", help="run the daily discovery now")
    a = ap.parse_args()
    db_path = a.db or os.path.join(a.data, "pump.db")
    if not os.path.exists(db_path):
        sys.exit(f"no database at {db_path}")
    force = a.force
    try:        # the page's Run now (data/jobs/patterns_run.json) also runs the daily discovery, once per click
        asked = os.path.getmtime(os.path.join(a.data, "jobs", "patterns_run.json")) * 1000
        force = force or asked > ((read_json(os.path.join(a.data, "settings", "patterns.json"), {}).get("daily") or {}).get("ts") or 0)
    except OSError:
        pass
    run(a.data, db_path, force=force)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Dip watch: a recorder and forward test for Rug Radar's "Danger dip + whale buy (tight exit, big pool)" strategy, live.

The strategy: a coin with an X link and a website that was created and graduated in one block, 1 to 5 minutes after graduation, with a market cap of
$80K+, the price down 30%+ over the last minute, a single buy of 5+ SOL in the last 30 seconds and a pool of 110+ SOL; sold at x1.5, x0.5 or after
30 minutes. Our stored data cannot test it (1-minute bars only), so this service records what the strategy needs: it follows every classic
graduate that passes the static tests (links, graduated within 3 s of its creation) during minutes 1 to 5.5, reads its 1-minute candles every 10 s,
and when the price has fallen sharply it reads the last minutes of trades (the biggest buy of the last 30 s in SOL, the exact price 60 s ago), the
pool's SOL (the vault balance) and the latest holder snapshot, and stores a READING with every condition as its own flag. The first reading at which the
dip conditions hold is a "dip" entry, the first at which every condition holds is a "full" entry; 31 minutes later each entry gets its result
(x1.5 / x0.5 / 30 min on the 1-minute candles, fees, price impact and stop slippage as in the backtest). Nothing is bought.
It writes data/dipwatch.json (the Dip watch page) and keeps readings and entries in data/dipwatch.db.

Usage: python3 dipwatch.py --data data [--probe]      (--probe looks at the young graduates once and prints what it sees)
"""
import argparse
import base64
import copy
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import numpy as np

import analyze as A
import diprule
import pumpapi as C
import solana
import wallets
import watch as W

MIN = 60000
TICK = 2.0
POLL = 10               # seconds between candle reads of a followed coin
AGE_FROM, AGE_TO = 60, 300           # the strategy's window after graduation (seconds)
FOLLOW_FROM, FOLLOW_TO = 45, 330     # a coin is looked at a little outside it
MAX_GAP_MS = 3000       # created and graduated in one block: the two timestamps (to the second) are at most this far apart
MCAP_MIN, MCAP_LOOK = 80000.0, 70000.0
DROP, DROP_LOOK = -0.30, -0.20       # the strategy's one-minute fall, and the looser one that makes the service read the trades
WHALE_SOL, POOL_SOL = 5.0, 110.0
TP, SL, HOLD, DELAY_MS = diprule.TP, diprule.SL, diprule.HOLD, diprule.DELAY_MS     # sold at x1.5, x0.5 or after 30 minutes; the paper trade buys 1.5 s after the signal (trader.py trades the same rule)
KEEP_AGE = 90 * MIN
MAX_READINGS = 40       # readings kept per coin
SOL = "So11111111111111111111111111111111111111112"
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, ts INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS readings_mint ON readings(mint);
CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, mint TEXT, symbol TEXT, kind TEXT, entry_ts INTEGER, data TEXT, updated_ts INTEGER);
"""


def fetch_trades(mint, since_ms, pages=8):
    """(the newest trades down to since_ms, whether they reach back that far). Newest first, 100 a page: a busy coin can have more
    than `pages` x 100 trades in the window, then the oldest part is missing and the second value is False."""
    out, cursor, complete = [], None, False
    for _ in range(pages):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        d = W.fast_get(f"/trades/{C.CHAIN}/{mint}", params) or {}
        rows = d.get("trades") or []
        out += rows
        cursor = d.get("cursor")
        if not rows or not cursor or rows[-1]["blockTimeMs"] < since_ms:
            complete = True
            break
        time.sleep(0.3)
    return [t for t in out if t["blockTimeMs"] >= since_ms], complete


def now_ms():
    return int(time.time() * 1000)


def fnum(x, d=4):
    return None if x is None or not np.isfinite(x) else round(float(x), d)


def link(s):
    return bool((s or "").strip())


class Coin:
    def __init__(self, mint, info, grad):
        self.mint, self.info, self.grad = mint, info, int(grad)
        self.symbol = info.get("symbol") or mint[:6]
        gap = (self.grad - int(info.get("created_ts") or 0)) if info.get("created_ts") else None
        self.links = link(info.get("twitter")) and link(info.get("website"))
        self.farm = gap is not None and 0 <= gap <= MAX_GAP_MS and self.grad % MIN != 0      # (a graduation time on a whole minute is a rounded one: the gap is unknown)
        self.static = self.links and self.farm and info.get("classic") == 1
        self.next_at = self.grad + FOLLOW_FROM * 1000
        self.busy = False
        self.mcap = None          # last price as market cap, USD
        self.state = "new"
        self.n_readings = 0
        self.last_reading = 0
        self.entries = {}         # kind -> entry dict
        self.pool_read = (0, None)   # (when, SOL in the pool)

    def pending(self):
        return any(e.get("outcome") is None for e in self.entries.values())


class Monitor:
    def __init__(self, data, p_grad):
        self.data, self.p_grad = data, p_grad
        self.db_path = os.path.join(data, "dipwatch.db")
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.coins, self.retired = {}, set()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.entries = {}
        with closing(self.connect()) as db:
            for eid, blob in db.execute("SELECT id, data FROM entries"):
                try:
                    self.entries[eid] = json.loads(blob)
                except ValueError:
                    pass
        self.recent = []          # the newest readings, for the page
        self.vaults = {}          # pool -> SOL vault address (or None when the pool is not the coin's own SOL pool)
        self.last_publish = 0.0
        self.followed = 0

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def save_entry(self, e):
        e["updated"] = now_ms()
        with self.lock, closing(self.connect()) as db:
            db.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?)", (e["id"], e["mint"], e["symbol"], e["kind"], e["entry_ts"], json.dumps(A.clean(e)), e["updated"]))
            db.commit()
        self.entries[e["id"]] = e

    def save_reading(self, c, r):
        with self.lock, closing(self.connect()) as db:
            db.execute("INSERT INTO readings (mint, symbol, ts, data) VALUES (?,?,?,?)", (c.mint, c.symbol, r["ts"], json.dumps(A.clean(r))))
            db.commit()
        self.recent = ([r] + self.recent)[:60]

    # ---- the coins ----
    def refresh(self, now):
        try:
            with closing(sqlite3.connect(f"file:{os.path.join(self.data, 'watch.db')}?mode=ro", uri=True, timeout=10)) as w:
                rows = w.execute("SELECT mint, info, graduated_ts FROM tokens WHERE graduated_ts > ? AND info IS NOT NULL", (now - KEEP_AGE,)).fetchall()
        except sqlite3.Error:
            return
        for mint, info_s, grad in rows:
            if mint in self.coins or mint in self.retired or not grad:
                continue
            try:
                info = json.loads(info_s)
            except ValueError:
                continue
            c = Coin(mint, info, grad)
            if not c.static:
                self.retired.add(mint)          # not a candidate (no links, not a one-block coin, not the standard curve): never read
                continue
            for e in self.entries.values():
                if e["mint"] == mint:
                    c.entries[e["kind"]] = e
            self.coins[mint] = c
        late = {e["mint"] for e in self.entries.values() if e.get("outcome") is None} - set(self.coins)
        if late:        # an entry whose result is still due after a long stop: its coin is brought back whatever its age
            try:
                with closing(sqlite3.connect(f"file:{os.path.join(self.data, 'watch.db')}?mode=ro", uri=True, timeout=10)) as w:
                    for mint in late:
                        row = w.execute("SELECT info, graduated_ts FROM tokens WHERE mint = ?", (mint,)).fetchone()
                        if row and row[0] and row[1]:
                            c = Coin(mint, json.loads(row[0]), row[1])
                            for e in self.entries.values():
                                if e["mint"] == mint:
                                    c.entries[e["kind"]] = e
                            c.next_at = now
                            self.coins[mint] = c
            except (sqlite3.Error, ValueError):
                pass
        for mint in [m for m, c in self.coins.items() if now - c.grad > KEEP_AGE and not c.pending()]:
            del self.coins[mint]
            self.retired.add(mint)

    # ---- reading the chain ----
    def pool_sol(self, c, now):
        """SOL in the coin's PumpSwap pool: the balance of the pool's SOL vault (the pool account holds the vault addresses)."""
        pool = c.info.get("pool")
        if not pool:
            return None
        if now - c.pool_read[0] < 8000:
            return c.pool_read[1]
        try:
            if pool not in self.vaults:
                res = solana.call(self.data, "getAccountInfo", [pool, {"encoding": "base64", "commitment": "confirmed"}], tries=3, timeout=10)
                raw = base64.b64decode(((res or {}).get("value") or {}).get("data", [""])[0]) if res else b""
                if len(raw) < 203:
                    return None
                key = lambda a: wallets.b58encode(raw[a:a + 32])        # noqa: E731
                self.vaults[pool] = key(171) if key(43) == c.mint and key(75) == SOL else None
            vault = self.vaults.get(pool)
            if not vault:
                return None
            res = solana.call(self.data, "getTokenAccountBalance", [vault, {"commitment": "confirmed"}], tries=3, timeout=10)
            val = float(((res or {}).get("value") or {}).get("uiAmount"))
            c.pool_read = (now, val)
            return val
        except Exception as e:
            print(f"  ! pool of {c.symbol}: {e!r}", flush=True)
            return None

    def holders(self, c, now):
        try:
            with closing(sqlite3.connect(f"file:{os.path.join(self.data, 'holders.db')}?mode=ro", uri=True, timeout=5)) as h:
                r = h.execute("SELECT ts, holders, top10_pct, dev_pct, snipers_pct, bundlers_pct FROM holder_snaps WHERE mint = ? AND ts <= ? ORDER BY ts DESC LIMIT 1", (c.mint, now)).fetchone()
        except sqlite3.Error:
            return None
        return None if not r else {"ts": r[0], "holders": r[1], "top10": r[2], "dev": r[3], "snipers": r[4], "bundlers": r[5]}

    def candles(self, c, to_ms, count=12):
        params = {"res": "1m", "count": count, "to": to_ms}
        if c.info.get("pool"):
            params["pool"] = c.info["pool"]
        r = W.fast_get(f"/candles/{C.CHAIN}/{c.mint}", params) or {}
        return r.get("candles") or []

    # ---- one coin ----
    def step(self, c):
        now = now_ms()
        age = (now - c.grad) / 1000
        c.next_at = now + POLL * 1000
        if age > FOLLOW_TO and not c.pending():
            c.state = "over"
            return
        if age > FOLLOW_TO:       # only the paper trades of its entries are still followed
            self.resolve(c, now)
            c.next_at = now + 10000
            return
        bars = self.candles(c, now)
        if not bars:
            c.state = "no candles yet"
            return
        minute = now // MIN * MIN
        form = next((b for b in reversed(bars) if int(b["openTimeMs"]) == minute), None)
        prev = next((b for b in reversed(bars) if int(b["openTimeMs"]) == minute - MIN), None)
        if form is None and prev is None:
            return
        price = float((form or prev)["close"])
        c.mcap = price * 1e9
        trig = False
        if c.mcap >= MCAP_LOOK and FOLLOW_FROM <= age <= FOLLOW_TO:
            if prev and (price / float(prev["close"]) - 1 <= DROP_LOOK or price / float(prev["high"]) - 1 <= DROP_LOOK - 0.08):
                trig = True
            if form and (price / float(form["high"]) - 1 <= DROP_LOOK - 0.05 or price / float(form["open"]) - 1 <= DROP_LOOK):
                trig = True
        c.state = "dip: reading the trades" if trig else f"watching ({age:.0f} s, ${c.mcap / 1e3:,.0f}K)"
        if trig and c.n_readings < MAX_READINGS:
            self.read(c, now, age)
        self.resolve(c, now)

    def read(self, c, now, age):
        try:
            trades, complete = fetch_trades(c.mint, now - 75000, pages=8)
        except Exception as e:
            print(f"  ! trades of {c.symbol}: {e!r}", flush=True)
            return
        amm = [t for t in trades if t.get("venue") == "pump_amm"]
        if not amm:
            return
        latest = amm[0]                                   # newest first
        p_now = float(latest["priceUsd"])
        old = [t for t in amm if t["blockTimeMs"] <= now - 60000]
        if not old and not complete:        # a very busy coin: more than the first pages of trades are inside the last minute, read further back once
            try:
                trades, complete = fetch_trades(c.mint, now - 75000, pages=24)
                amm = [t for t in trades if t.get("venue") == "pump_amm"] or amm
                old = [t for t in amm if t["blockTimeMs"] <= now - 60000]
            except Exception as e:
                print(f"  ! deeper trades of {c.symbol}: {e!r}", flush=True)
        p60 = float(old[0]["priceUsd"]) if old else None
        w30 = [t for t in amm if t["blockTimeMs"] > now - 30000]
        sol_of = lambda t: int(t["quoteAmount"]["raw"]) / 1e9 if (t.get("quote") or {}).get("id") == SOL else 0.0      # noqa: E731
        buys = [sol_of(t) for t in w30 if t["side"] == "buy"]
        sells = [sol_of(t) for t in w30 if t["side"] == "sell"]
        mcap, drop = p_now * 1e9, (p_now / p60 - 1) if p60 else None
        c_age, c_mcap = AGE_FROM <= age <= AGE_TO, mcap >= MCAP_MIN
        c_drop = drop is not None and drop <= DROP
        c_whale = bool(buys) and max(buys) >= WHALE_SOL
        dip = c_age and c_mcap and c_drop
        pool = self.pool_sol(c, now) if (dip or (c_age and c_mcap and c_whale)) else None
        c_pool = pool is not None and pool >= POOL_SOL
        h = self.holders(c, now)
        r = {"mint": c.mint, "symbol": c.symbol, "ts": now, "age_s": round(age, 1), "mcap": fnum(mcap, 0), "drop60": fnum(drop), "price_60s_ago": fnum(p60 * 1e9, 0) if p60 else None,
             "max_buy30_sol": fnum(max(buys)) if buys else 0.0, "buys30": len(buys), "sells30": len(sells), "buy_sol30": fnum(sum(buys)), "sell_sol30": fnum(sum(sells)),
             "pool_sol": fnum(pool, 1), "holders": h, "danger_proxy": bool(h and ((h["bundlers"] or 0) >= 50 or (h["dev"] or 0) >= 50)) if h else None,
             "dev_buy_sol": c.info.get("dev_buy_sol"), "partial": not complete, "drop_unknown": p60 is None,
             "c_age": c_age, "c_mcap": c_mcap, "c_drop": c_drop, "c_whale": c_whale, "c_pool": c_pool, "dip": bool(dip), "full": bool(dip and c_whale and c_pool)}
        c.n_readings += 1
        self.save_reading(c, r)
        c.mcap = mcap
        for kind, ok in (("dip", dip), ("full", r["full"])):
            if ok and kind not in c.entries:
                e = {"id": f"{c.mint}:{kind}", "mint": c.mint, "symbol": c.symbol, "kind": kind, "grad": c.grad, "entry_ts": now + DELAY_MS, "read_ts": now, "entry_mcap": fnum(mcap, 0),
                     "age_s": r["age_s"], "reading": {k: r[k] for k in ("drop60", "max_buy30_sol", "pool_sol", "danger_proxy", "c_whale", "c_pool", "buy_sol30", "sell_sol30")},
                     "pool": c.info.get("pool"), "name": c.info.get("name"), "outcome": None}
                c.entries[kind] = e
                self.save_entry(e)
                print(f"ENTRY {kind.upper()} {c.symbol} age {age:.0f}s mcap ${mcap:,.0f} drop {drop * 100:+.0f}% whale {max(buys) if buys else 0:.1f} SOL pool {pool}", flush=True)
        c.state = "FULL SIGNAL" if r["full"] else ("dip" + (" + whale" if c_whale else "") + (" + pool" if c_pool else "") if dip else "watching")

    def net_of(self, pe, gross):
        """Net return of a trade bought at price pe and sold at gross x pe: fees and the price impact of our stake, as in analyze.net_return."""
        stake = A.stake_usd(self.p_grad)
        arr = {"p_grad": self.p_grad}
        qi, qo = A.depth(arr, pe), A.depth(arr, pe * gross)
        return gross * (1 - A.FEE) ** 2 / ((1 + stake / qi) * (1 + stake * gross / qo)) - 1, stake / qi

    def seconds(self, c, to_ms):
        params = {"res": "1s", "count": 300, "to": to_ms}
        if c.info.get("pool"):
            params["pool"] = c.info["pool"]
        r = W.fast_get(f"/candles/{C.CHAIN}/{c.mint}", params) or {}
        return sorted(r.get("candles") or [], key=lambda b: int(b["openTimeMs"]))

    def paper(self, c, e, now):
        """The paper trade of one entry, followed second by second on the 1-second candles: bought at the first second after the signal
        (entry_ts = the reading + 1.5 s), sold at x1.5 or x0.5 the moment a second touches it (the stop first inside a second) or after 30 minutes.
        Fees, price impact and stop slippage as in the backtest; the stake is analyze.STAKE_SOL. Returns True while the trade is still open."""
        p = e.get("paper")
        if p is None:
            p = e["paper"] = {"cursor": e["entry_ts"], "p0": None, "last": None, "peak": None, "low": None, "status": "waiting", "stake_sol": A.STAKE_SOL, "polls": 0}
        end = e["entry_ts"] + HOLD * MIN
        try:
            for _ in range(8):
                if p["cursor"] >= min(now, end + 2000):
                    break
                to = min(now, p["cursor"] + 300000, end + 2000)
                bars = [b for b in self.seconds(c, to) if p["cursor"] <= int(b["openTimeMs"]) < to]
                p["cursor"] = to
                for b in bars:
                    o, h, lo, cl = (float(b[x]) for x in ("open", "high", "low", "close"))
                    t = int(b["openTimeMs"])
                    if p["p0"] is None:
                        p["p0"], p["status"], p["bought_ts"] = o, "open", t          # bought at the first second's open after the signal
                        p["peak"] = p["low"] = o
                    p["last"] = cl
                    p["peak"], p["low"] = max(p["peak"], h), min(p["low"], lo)
                    m = lambda x: x / p["p0"]       # noqa: E731
                    if m(lo) <= 1 - SL:
                        gross, why = min(min(m(o), 1 - SL), m(cl)) * (1 - A.SL_SLIP), "stop"
                    elif m(h) >= 1 + TP:
                        gross, why = 1 + TP, "target"
                    else:
                        continue
                    self.close_paper(c, e, gross, why, t)
                    return False
        except Exception as ex:
            p["polls"] = p.get("polls", 0) + 1
            print(f"  ! paper trade of {c.symbol}: {ex!r}", flush=True)
            if p["polls"] >= 30 and p["p0"] is None:
                return None            # the 1-second candles are not there: the 1-minute result is used
            return True
        if now >= end and p["cursor"] >= end:
            if p["p0"] is None:
                return None
            self.close_paper(c, e, p["last"] / p["p0"], "time", end)
            return False
        if p["p0"] is None and now > e["entry_ts"] + 120000:
            return None                   # no trade in the two minutes after the signal: the 1-minute result is used
        return True

    def close_paper(self, c, e, gross, why, t):
        p = e["paper"]
        net, impact = self.net_of(p["p0"], gross)
        p["status"] = "closed"
        e["entry_price_mcap"] = fnum(p["p0"] * 1e9, 0)
        e["outcome"] = {"net": fnum(net), "gross": fnum(gross), "exit": why, "held": round((t - p["bought_ts"]) / MIN, 1), "peak": fnum(p["peak"] / p["p0"] - 1), "trough": fnum(p["low"] / p["p0"] - 1),
                        "impact": fnum(impact), "pnl_sol": fnum(net * p["stake_sol"], 5), "second_level": True, "closed_ts": t}
        self.save_entry(e)
        print(f"PAPER {e['kind']} {c.symbol}: {why} {net * 100:+.1f}% ({net * p['stake_sol']:+.4f} SOL)", flush=True)

    def resolve(self, c, now):
        """Each open paper trade is followed second by second; one whose 1-second candles never came is judged 31 minutes after its entry
        on the 1-minute candles (what it would have done at x1.5 / x0.5 / 30 minutes)."""
        for e in c.entries.values():
            if e.get("outcome") is None and not e.get("paper_failed"):
                r = self.paper(c, e, now)
                if r is None:
                    e["paper_failed"] = True
                    self.save_entry(e)
                elif r:
                    e["now_mult"] = fnum(e["paper"]["last"] / e["paper"]["p0"]) if e["paper"].get("p0") and e["paper"].get("last") else None
                    self.save_entry(e)
                    continue
                else:
                    continue
            if e.get("outcome") is not None or now < e["entry_ts"] + (HOLD + 1) * MIN + 5000:
                continue
            t0 = e["entry_ts"] // MIN * MIN + MIN              # the entry minute is not used (its high and low may precede the buy)
            end = e["entry_ts"] + HOLD * MIN
            try:
                bars = self.candles(c, end + MIN + 5000, count=60)        # (the bars up to the end of the hold, whenever this runs)
            except Exception as ex:
                print(f"  ! candles of {c.symbol}: {ex!r}", flush=True)
                continue
            pe = e["entry_mcap"] / 1e9
            seq = sorted([b for b in bars if t0 <= int(b["openTimeMs"]) < end], key=lambda b: int(b["openTimeMs"]))
            if not seq:
                # the API lists only minutes with trades: a coin that went dead (or was rugged) after the entry has few or no bars; it is judged on its last close
                before = [b for b in bars if int(b["openTimeMs"]) < t0]
                if not before:
                    e["tries"] = e.get("tries", 0) + 1
                    if e["tries"] >= 5:
                        e["outcome"] = {"net": None, "exit": "no candles"}
                        self.save_entry(e)
                    continue
                seq = [dict(before[-1], open=before[-1]["close"], high=before[-1]["close"], low=before[-1]["close"])]
            gross, why, held = None, "time", HOLD
            for k, b in enumerate(seq):
                o, h, lo, cl = (float(b[x]) / pe for x in ("open", "high", "low", "close"))
                if lo <= 1 - SL:
                    gross, why, held = min(min(o, 1 - SL), cl) * (1 - A.SL_SLIP), "stop", k + 1
                    break
                if h >= 1 + TP:
                    gross, why, held = 1 + TP, "target", k + 1
                    break
            if gross is None:
                gross = float(seq[-1]["close"]) / pe
            stake = A.stake_usd(self.p_grad)
            arr = {"p_grad": self.p_grad}
            qi, qo = A.depth(arr, pe), A.depth(arr, pe * gross)
            net = gross * (1 - A.FEE) ** 2 / ((1 + stake / qi) * (1 + stake * gross / qo)) - 1
            e["outcome"] = {"net": fnum(net), "gross": fnum(gross), "exit": why, "held": held, "bars": len(seq),
                            "peak": fnum(max(float(b["high"]) for b in seq) / pe - 1), "trough": fnum(min(float(b["low"]) for b in seq) / pe - 1), "impact": fnum(stake / qi)}
            self.save_entry(e)
            print(f"RESULT {e['kind']} {c.symbol}: {e['outcome']}", flush=True)

    # ---- the page ----
    def publish(self, now):
        watching = []
        for c in sorted(self.coins.values(), key=lambda c: -c.grad):
            age = (now - c.grad) / 1000
            if age > FOLLOW_TO + 30 or c.mcap is None:
                continue
            watching.append({"mint": c.mint, "symbol": c.symbol, "age_s": round(age), "mcap": fnum(c.mcap, 0), "state": c.state, "readings": c.n_readings, "pool": c.info.get("pool")})
        evs = None
        for _ in range(3):
            try:
                evs = sorted(copy.deepcopy(list(self.entries.values())), key=lambda e: -e["entry_ts"])
                break
            except RuntimeError:
                time.sleep(0.05)
        if evs is None:
            return

        def stat(sel):
            r = [e["outcome"]["net"] for e in sel if e.get("outcome") and e["outcome"].get("net") is not None]
            return {"n": len(r), "mean": fnum(float(np.mean(r))) if r else None, "won": int(sum(x > 0 for x in r)), "total": fnum(float(np.sum(r))) if r else None}
        dip = [e for e in evs if e["kind"] == "dip"]
        full = [e for e in evs if e["kind"] == "full"]
        rd = lambda e, k: (e.get("reading") or {}).get(k)      # noqa: E731
        pnl = lambda sel: fnum(sum((e["outcome"].get("pnl_sol") if e["outcome"].get("pnl_sol") is not None else (e["outcome"]["net"] or 0) * A.STAKE_SOL) for e in sel if e.get("outcome") and e["outcome"].get("net") is not None), 4)      # noqa: E731
        stats = {"stake_sol": A.STAKE_SOL, "open": sum(1 for e in evs if not e.get("outcome")), "pnl_full": pnl(full), "pnl_dip": pnl(dip), "followed": sum(1 for c in self.coins.values() if (now - c.grad) / 1000 <= FOLLOW_TO), "entries": len(evs), "dip": len(dip), "full": len(full), "pending": sum(1 for e in evs if not e.get("outcome")),
                 "full_all": stat(full), "dip_all": stat(dip),
                 "dip_whale": stat([e for e in dip if rd(e, "c_whale")]), "dip_no_whale": stat([e for e in dip if not rd(e, "c_whale")]),
                 "dip_pool": stat([e for e in dip if rd(e, "c_pool")]), "dip_no_pool": stat([e for e in dip if not rd(e, "c_pool")]),
                 "dip_danger": stat([e for e in dip if rd(e, "danger_proxy")]), "dip_not_danger": stat([e for e in dip if rd(e, "danger_proxy") is False])}
        doc = {"updated": now, "watching": watching[:40], "entries": evs[:120], "readings": self.recent[:30], "stats": stats,
               "rule": {"age": [AGE_FROM, AGE_TO], "mcap": MCAP_MIN, "drop": DROP, "whale_sol": WHALE_SOL, "pool_sol": POOL_SOL, "tp": TP, "sl": SL, "hold": HOLD, "gap_ms": MAX_GAP_MS}}
        W.write_json(os.path.join(self.data, "dipwatch.json"), doc)

    def run(self):
        print(f"dip watch started; {len(self.entries)} entries on record", flush=True)
        while True:
            now = now_ms()
            try:
                self.refresh(now)
                for c in list(self.coins.values()):
                    if not c.busy and now >= c.next_at and (c.state != "over" or c.pending()):
                        c.busy = True
                        self.pool.submit(self.work, c)
                if time.time() - self.last_publish >= 3:
                    self.last_publish = time.time()
                    self.publish(now)
            except Exception as e:
                print(f"  ! loop: {e!r}", flush=True)
            time.sleep(TICK)

    def work(self, c):
        try:
            self.step(c)
        except Exception as e:
            print(f"  ! {c.symbol}: {e!r}", flush=True)
            c.next_at = now_ms() + 20000
        finally:
            c.busy = False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--probe", action="store_true", help="look at the young graduates once, print what is seen, and exit")
    args = ap.parse_args()
    p_grad, _ = W.report_extras(args.data)
    mon = Monitor(args.data, p_grad)
    if args.probe:
        now = now_ms()
        mon.refresh(now)
        print(f"{len(mon.coins)} candidate coins in the last {KEEP_AGE // MIN} minutes (links + one-block + standard curve)")
        for c in sorted(mon.coins.values(), key=lambda c: c.grad):
            c.next_at = 0
            if (now - c.grad) / 1000 <= FOLLOW_TO:
                mon.step(c)
            print(f"{c.symbol:12} age {(now - c.grad) / 1000:6.0f}s  cap ${c.mcap or 0:>9,.0f}  {c.state}")
        mon.publish(now_ms())
        return
    mon.run()


if __name__ == "__main__":
    sys.exit(main())

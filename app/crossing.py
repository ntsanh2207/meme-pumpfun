#!/usr/bin/env python3
"""Watch every new graduate for the crossing pattern, live.

The pattern (analyze.cross_at, rewritten 21 Oct 2026): a coin graduates with its first minute closing between $35K and $50K, never
trades above the highest price of that minute (its level) and then closes back above it after a close below it. This service follows each young graduate from watch.db, reads its 1-minute candles every
15-30 s (the current minute included), flags the moment its price first tops the level, reads the trades around
that moment (who bought and sold in the 60 s before and after), confirms the crossing when the minute closes, and later records what
a buy at that price would have done (+100% / -40% / 15 minutes, as in the backtest). Nothing is bought: it is a monitor and a
forward test. It writes data/crossing.json (the Crossing page) and keeps every event in data/crossing.db.

Usage: python3 crossing.py --data data [--probe]      (--probe prints what it sees once and exits)
"""
import argparse
import copy
import json
import os
import sqlite3
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import numpy as np

import analyze as A
import collector as C
import notify
import watch as W

MIN = 60000
TICK = 2.0              # seconds between looks at the list of coins
POLL_ARMED, POLL_IDLE = 15, 30   # seconds between candle reads of a coin that is armed / before that
WATCH_AGE = 14 * MIN    # a graduate is followed for its first 14 minutes (the pattern needs minutes 4-12)
KEEP_AGE = 45 * MIN     # and kept in memory while an event of it waits for its trades or its result
SETTLE = 2500           # ms after a minute ends before its candle is read as final
WINDOW = 60000          # the trade windows before and after the crossing
GROUP = (0.1, 10.0)     # minutes from creation to graduation: the group that did best in the backtest
QUIET = 857             # trades in the last 5 minutes at entry: the threshold picked on the earlier coins
EXIT = (1.0, 0.4, 15)   # take profit, stop loss, minutes held (the backtest's main exit)
SELL_SHARE_MAX, BUYERS_MIN = 0.15, 8   # "sellers gone" at the crossing, fixed from KEN and Mimi
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,            -- mint:minute (a coin that fell back can cross again in a later minute)
  mint TEXT, symbol TEXT, grad_ts INTEGER, m INTEGER, detected_ts INTEGER,
  status TEXT,                    -- live (price topped the graduation price), confirmed (the minute closed above it), failed (it fell back)
  data TEXT,                      -- JSON: the whole event
  updated_ts INTEGER
);
"""


class Coin:
    def __init__(self, mint, info, grad):
        self.mint, self.info, self.grad = mint, info, int(grad)
        self.grad_min = self.grad // MIN * MIN
        self.symbol = info.get("symbol") or mint[:6]
        self.next_at = self.grad_min + MIN + SETTLE     # nothing to see before the graduation minute has closed
        self.busy = False
        self.price = None          # last close, as market cap in USD
        self.k = 0
        self.ref = None            # the price to cross (see step)
        self.sent_watch = self.sent_armed = False      # Telegram alerts already sent for this coin
        self.rows = []             # the live status rows (timeline)
        self.below = 0             # closed minutes below the level so far
        self.state = "new"
        self.dev = None            # creator's first buy in SOL (looked up once, when the coin first gets close to being armed)
        self.event = None          # the event being followed (a dict) or None
        self.done = False

    def pending(self):
        """Work still to do for the event of this coin: the trades after the crossing, the confirmation, the result."""
        ev = self.event
        return bool(ev and (ev["status"] == "live" or ev["post"] is None or (ev["status"] == "confirmed" and ev.get("outcome") is None)))


def out_reason(close, high, low, closed, p0):
    """Why a coin can no longer become a crossing (a limit that only gets worse as minutes pass), or None.
    closed = the number of final minutes (0..closed-1); p0 = the level (the highest price of minute 0). Called after the crossing
    check of the minute that just closed, so a minute that closed above the level while it crossed is already an event."""
    if closed < 2:
        return None
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)          # (a slice without any bar: nothing to compare)
        if np.nanmin(low[0:closed]) < A.CROSS_FLOOR_MCAP / A.SUPPLY:
            return f"out: fell under ${A.CROSS_FLOOR_MCAP / 1e3:.0f}K"
        if np.nanmax(high[1:min(closed, 3)]) > p0:
            return "out: the high of minute 1 or 2 was above the level"
        if np.nanmax(close[1:closed]) > p0:
            return "out: closed above the level before crossing it"
    return None


def timeline(c, close, low, high, closed, k, price_now, state=""):
    """The coin's live status as rows [{t0, t1, minute, text, cls}] (times in ms; t1 None = the forming minute): what each minute did against the level to cross."""
    H, rows = c.ref, []
    mc = lambda x: f"${x * 1e9 / 1e3:,.1f}K"
    rows.append({"t0": c.grad, "t1": None, "minute": None, "text": "Graduated to PumpSwap", "cls": ""})
    if closed >= 1:
        ok = bool(A.cross_base(close, H))
        rows.append({"t0": c.grad_min, "t1": c.grad_min + MIN, "minute": 0, "cls": "" if ok else "n",
                     "text": f"Graduation minute. Highest price {mc(high[0])}: the level to cross. Closed {mc(close[0])}" + (f", inside {A.CROSS_START_MCAP / 1e3:.0f}K to {A.CROSS_PRE_MCAP / 1e3:.0f}K." if ok else f", outside {A.CROSS_START_MCAP / 1e3:.0f}K to {A.CROSS_PRE_MCAP / 1e3:.0f}K: not a candidate.")})
    for m in range(1, min(closed, A.T)):
        cl, lo, hi = close[m], low[m], high[m]
        cross = bool(A.cross_at(close, low, H, m, high))
        bits = [f"Closed {mc(cl)}, {'at or above' if cl >= H else 'below'} the level"]
        if hi > H:
            bits.append(f"high {mc(hi)} above the level" + (": out (rule 2)" if m <= 2 else " (allowed: only closes count from minute 3)"))
        if cl > H and not cross:
            bits.append("closed above the level: out")
        if lo < A.CROSS_FLOOR_MCAP / A.SUPPLY:
            bits.append(f"low {mc(lo)} under the {A.CROSS_FLOOR_MCAP / 1e3:.0f}K floor")
        rows.append({"t0": c.grad_min + m * MIN, "t1": c.grad_min + (m + 1) * MIN, "minute": m, "text": ", ".join(bits) + (". A crossing" if cross else "") + ".", "cls": "p" if cross else ""})
    if k >= 1 and price_now is not None and k < A.T and not state.startswith("not at"):
        gap = price_now / H - 1
        rows.append({"t0": c.grad_min + k * MIN, "t1": None, "minute": k, "cls": "w",
                     "text": f"Now {mc(price_now)} ({gap * 100:+.1f}% vs the level). " + (
                         "Above the level: it crosses if this minute closes there." if state == "CROSSING NOW" else
                         f"Out: {state[5:]}." if state.startswith("out:") else "Past minute 12: missed." if k > A.CROSS_LAST or state == "missed" else
                         "Too early: the earliest crossing is minute 3." if k < A.CROSS_FIRST else
                         "Waiting for a close at or above the level." if state.startswith("armed") else
                         "The last close was above the level: it must close below it first.")})
    return rows


def now_ms():
    return int(time.time() * 1000)


def fnum(x, d=6):
    return None if x is None or not np.isfinite(x) else round(float(x), d)


def window_stats(trades, t0, t1, seen=()):
    """Buyers and sellers of PumpSwap trades with t0 <= time < t1."""
    w = [t for t in trades if t0 <= t["blockTimeMs"] < t1 and t.get("venue") == "pump_amm"]
    b = [t for t in w if t["side"] == "buy"]
    s = [t for t in w if t["side"] == "sell"]
    v = lambda t: float(t["valueUsd"])   # noqa: E731
    wallet = lambda t: t["trader"]["address"]   # noqa: E731
    bu, su = sum(map(v, b)), sum(map(v, s))
    buyers = {wallet(t) for t in b}
    return {"buys": len(b), "buy_usd": round(bu, 2), "sells": len(s), "sell_usd": round(su, 2), "buyers": len(buyers),
            "sellers": len({wallet(t) for t in s}), "sell_share": round(su / (bu + su), 3) if bu + su else None,
            "biggest_buy": round(max(map(v, b)), 2) if b else 0, "new_buyers": len(buyers - set(seen))}


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


class Monitor:
    def alert(self, kind, c, text):
        """A Telegram message for the Crossing page's alerts (settings/telegram.json of the Crossing folder: its own bot and what to send).
        kind: 'watching' (a coin enters Watching or is armed), 'confirmed'. In the background; a slow Telegram never holds up a read."""
        if now_ms() < self.quiet_until:      # (just after a restart every coin in its first minutes would be announced again)
            return
        def go():
            try:
                cfg = notify.load(os.path.join(self.data, "crossing", "settings"))
                if not notify.ready(cfg):
                    return
                ev = notify.events(cfg)
                if kind == "confirmed" and not ev["confirmed"] or kind in ("watching", "armed") and (ev["watching"] == "off" or kind == "watching" and ev["watching"] != "all"):
                    return
                links = f'<a href="https://pump.fun/coin/{notify.esc(c.mint)}">pump.fun</a> · <a href="https://dexscreener.com/solana/{notify.esc(c.info.get("pool") or c.mint)}">DexScreener</a>'
                notify.send(cfg, f"{text}\n{links}")
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    def __init__(self, data, p0):
        self.data, self.p0 = data, p0
        self.quiet_until = now_ms() + 45000
        self.lock = threading.Lock()
        self.coins = {}
        self.db_path = os.path.join(data, "crossing.db")
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)
        self.events = {}
        with closing(self.connect()) as db:
            for eid, blob in db.execute("SELECT id, data FROM events"):
                try:
                    self.events[eid] = json.loads(blob)
                except ValueError:
                    pass
        self.retired = set()        # coins that are over: not picked up again from watch.db
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.last_publish = 0.0
        self.seen_tokens = 0

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def save(self, ev):
        ev["updated"] = now_ms()
        with self.lock, closing(self.connect()) as db:
            db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                       (ev["id"], ev["mint"], ev["symbol"], ev["grad"], ev["m"], ev["detected"], ev["status"], json.dumps(A.clean(ev)), ev["updated"]))
            db.commit()
        self.events[ev["id"]] = ev

    # ---- the coins ----
    def refresh(self, now):
        try:
            with closing(sqlite3.connect(f"file:{os.path.join(self.data, 'watch.db')}?mode=ro", uri=True, timeout=10)) as w:
                rows = w.execute("SELECT mint, info, graduated_ts FROM tokens WHERE graduated_ts > ? AND info IS NOT NULL",
                                 (now - KEEP_AGE,)).fetchall()
        except sqlite3.Error:
            return
        for mint, info_s, grad in rows:
            if mint in self.coins or mint in self.retired or not grad:
                continue
            try:
                info = json.loads(info_s)
            except ValueError:
                continue
            if info.get("classic") != 1:       # the pattern is defined for the standard curve only
                continue
            c = Coin(mint, info, grad)
            mine = [e for e in self.events.values() if e["mint"] == mint and e["status"] != "failed"]   # (a failed try does not stop a later crossing)
            if mine:
                c.event, c.next_at = mine[0], now
                c.done = c.event["status"] == "confirmed" and c.event.get("outcome") is not None
            self.coins[mint] = c
        for mint in [m for m, c in self.coins.items() if now - c.grad_min > KEEP_AGE or (c.done and now - c.grad_min > WATCH_AGE)]:
            del self.coins[mint]
            self.retired.add(mint)

    # ---- one coin ----
    def look(self, c, now):
        """Read the candles and update everything about one coin."""
        params = {"res": "1m", "count": int(min(300, max(40, (now - c.grad_min) // MIN + 3))), "to": now}
        if c.info.get("pool"):
            params["pool"] = c.info["pool"]
        r = W.fast_get(f"/candles/{C.CHAIN}/{c.mint}", params) or {}
        bars = r.get("candles") or []
        if not bars:
            return None
        arr, vol, trd = W.arrays(bars, c.grad_min, self.p0)
        return arr, vol, trd

    def features(self, c, arr, vol, trd, e):
        info = dict(c.info)
        info["dev_buy_sol"] = c.dev if c.dev is not None else float("nan")
        f = A.features(W.token_frame(self.data, c.mint, info, c.grad_min), arr, vol, trd, e)
        return {k: fnum(v[0]) for k, v in f.items()}

    def step(self, c):
        res = self.look(c, now_ms())
        now = now_ms()                      # (after the read, which can take seconds: a minute may have ended meanwhile)
        k = int((now - c.grad_min) // MIN)
        c.k = k
        if res is None:
            c.next_at = now + POLL_IDLE * 1000
            return
        arr, vol, trd = res
        close, low, high = arr["close"][0].astype(float), arr["low"][0].astype(float), arr["high"][0].astype(float)
        h0 = float(high[0]) if len(high) and np.isfinite(high[0]) else self.p0
        if c.event is not None and c.event.get("ref") is None:
            h0 = self.p0        # an event recorded before the change keeps the level it was flagged at
        p0 = c.ref = h0         # the level to cross: the highest price of the coin's own graduation minute (since 20 Oct 2026; before that the standard graduation price, self.p0)
        c.price = fnum(close[min(k, len(close) - 1)] * 1e9, 1)
        closed = min(k, A.T - 1)                   # minutes 0..closed-1 are final
        base = A.cross_base(close, p0) if closed >= 1 else None
        c.below = int((close[1:closed] < p0).sum()) if closed >= 2 else 0      # closes below the level
        armed = bool(base) and A.CROSS_FIRST <= k <= A.CROSS_LAST and closed >= 3 and A.cross_setup(close, low, high, p0, k) and bool(close[k - 1] < p0)      # rules 1-4 hold so far and the last close is below the level
        # the creator's first buy: looked up once, when the coin first gets close to being armed
        if c.dev is None and base and k >= 2:
            try:
                c.dev = W.dev_buy(c.mint, quick=True)
            except Exception:
                c.dev = float("nan")
        ev = c.event
        gone = out_reason(close, high, low, closed, p0) if base else None
        if ev is None:
            if k - 1 >= A.CROSS_FIRST and bool(base) and A.cross_at(close, low, p0, k - 1, high):
                self.detect(c, close, now, k - 1)       # the minute k-1 crossed and closed between two looks
            elif base is False:
                c.state, c.done = "not at the standard price", True
            elif gone:
                c.state, c.done = gone, True
            elif k > A.CROSS_LAST:
                c.state, c.done = "missed", True
            elif armed and A.cross_at(close, low, p0, k, high):
                self.detect(c, close, now, k)
            else:
                c.state = "armed (waiting for the cross)" if armed else ("early" if k < A.CROSS_FIRST else "above the level")
        mc = lambda x: f"${x * 1e9 / 1e3:,.1f}K"      # noqa: E731
        if base and not c.done and not c.sent_watch and not c.event:
            c.sent_watch = True
            self.alert("watching", c, f"👀 <b>${notify.esc(c.symbol)}</b> is watched (minute {k}): level to cross {mc(p0)}, now {mc(close[min(k, len(close) - 1)])}")
        if c.state.startswith("armed") and not c.sent_armed:
            c.sent_armed = True
            self.alert("armed", c, f"🎯 <b>${notify.esc(c.symbol)}</b> is armed (minute {k}): its last close is below the level, a close at or above {mc(p0)} is a crossing. Now {mc(close[min(k, len(close) - 1)])}")
        try:
            c.rows = timeline(c, close, low, high, closed, k, close[min(k, len(close) - 1)], c.state)
        except Exception as e:
            print(f"  ! timeline of {c.symbol}: {e!r}", flush=True)
        ev = c.event
        if ev is not None:
            self.follow(c, ev, close, low, arr, vol, trd, now)
        # when to look next
        gap = (POLL_ARMED if (armed or c.event) else POLL_IDLE) * 1000
        nxt = now + gap
        if A.CROSS_FIRST - 1 <= k <= A.CROSS_LAST:
            boundary = c.grad_min + (k + 1) * MIN + SETTLE
            if now < boundary <= nxt:
                nxt = boundary
        c.next_at = nxt

    def detect(self, c, close, now, k):
        """The price topped the graduation price in minute k: record it and read the trades around it."""
        ev = {"id": f"{c.mint}:{k}", "mint": c.mint, "symbol": c.symbol, "grad": c.grad, "grad_min": c.grad_min, "m": k, "detected": now, "status": "live",
              "price": fnum(close[k] * 1e9, 1), "name": c.info.get("name"), "x": bool((c.info.get("twitter") or "").strip()),
              "web": bool((c.info.get("website") or "").strip()), "dev_buy_sol": fnum(c.dev) if c.dev is not None else None,
              "pool": c.info.get("pool"), "ref": fnum(c.ref * 1e9, 1), "pre": None, "post": None, "t_cross": None, "feat": None, "flags": None, "outcome": None}
        try:
            trades, complete = fetch_trades(c.mint, min(now - 3 * MIN, c.grad_min + k * MIN - WINDOW - 10000))
            minute = [t for t in trades if t.get("venue") == "pump_amm" and c.grad_min + k * MIN <= t["blockTimeMs"] < c.grad_min + (k + 1) * MIN and float(t["priceUsd"]) >= c.ref]
            ev["t_cross"] = min(t["blockTimeMs"] for t in minute) if minute else now
            ev["pre"] = window_stats(trades, ev["t_cross"] - WINDOW, ev["t_cross"])
            ev["pre"]["partial"] = not complete
        except Exception as e:
            print(f"  ! trades of {c.symbol}: {e!r}", flush=True)
            ev["t_cross"] = now
        c.event, c.state = ev, "CROSSING NOW"
        try:
            self.save(ev)
        except Exception as e:      # the page still shows it from memory; the next save retries the file
            self.events[ev["id"]] = ev
            print(f"  ! could not save the event of {c.symbol}: {e!r}", flush=True)
        print(f"CROSSING {c.symbol} {c.mint} minute {k} at ${ev['price']:,.0f}", flush=True)

    def follow(self, c, ev, close, low, arr, vol, trd, now):
        k_ev = ev["m"]
        ev["now_mcap"] = c.price
        # 1. the trades after the crossing, once their 60 s have passed
        if ev["post"] is None and ev["t_cross"] and now >= ev["t_cross"] + WINDOW + 5000:
            try:
                trades, complete = fetch_trades(c.mint, ev["t_cross"] - WINDOW)
                if complete or ev["pre"] is None:      # (a busy coin's truncated fetch must not replace the fuller one read at the crossing)
                    ev["pre"] = window_stats(trades, ev["t_cross"] - WINDOW, ev["t_cross"])
                    ev["pre"]["partial"] = not complete
                seen = {t["trader"]["address"] for t in trades if t["side"] == "buy" and t["blockTimeMs"] < ev["t_cross"]}
                ev["post"] = window_stats(trades, ev["t_cross"], ev["t_cross"] + WINDOW, seen)
                p = ev["post"]
                ev["post"]["sellers_gone"] = bool(p["sell_share"] is not None and p["sell_share"] <= SELL_SHARE_MAX and p["buyers"] >= BUYERS_MIN)
                ev["post"]["partial"] = not complete
            except Exception as e:
                print(f"  ! trades of {c.symbol}: {e!r}", flush=True)
                ev["post"] = {"error": True}
            self.save(ev)
        # 2. the crossing minute has closed: confirmed or not
        if ev["status"] == "live" and now >= c.grad_min + (k_ev + 1) * MIN + SETTLE:
            if A.cross_at(close, low, c.ref, k_ev, arr["high"][0].astype(float)):
                e = k_ev + 1
                f = self.features(c, arr, vol, trd, e)
                mtg, t5 = f.get("mins_to_grad"), f.get("trades_last_5m")
                ev["status"], ev["confirmed"], ev["entry"] = "confirmed", now, fnum(close[k_ev] * 1e9, 1)
                ev["feat"] = {k: f.get(k) for k in ("mins_to_grad", "trades_last_5m", "vol_usd_last_5m", "avg_trade_usd_5m", "dev_buy_sol", "trades_total", "holders", "top10_pct", "bundlers_pct", "snipers_pct", "ret_last_5m")}
                ev["flags"] = {"group": bool(mtg is not None and GROUP[0] <= mtg <= GROUP[1]), "quiet": bool(t5 is not None and t5 <= QUIET), "quiet500": bool(t5 is not None and t5 <= 500),
                               "pattern": bool(A.cross_pattern(close, arr["high"][0].astype(float), low, arr["open"][0].astype(float), c.ref, k_ev))}
                print(f"CONFIRMED {c.symbol} minute {k_ev} group={ev['flags']['group']} quiet={ev['flags']['quiet']} pattern={ev['flags']['pattern']}", flush=True)
                self.alert("confirmed", c, f"✅ <b>Crossing confirmed: ${notify.esc(c.symbol)}</b> (minute {k_ev})\nEntry {ev['entry'] and '$%.1fK' % (ev['entry'] / 1e3)}, level {'$%.1fK' % (c.ref * 1e9 / 1e3)}"
                           f"{' · group' if ev['flags']['group'] else ''}{' · quiet' if ev['flags']['quiet'] else ''}{' · PATTERN (strategy: +%d%% / -%d%% / %d min)' % (A.PATTERN_EXIT[0] * 100, A.PATTERN_EXIT[1] * 100, A.PATTERN_EXIT[2]) if ev['flags']['pattern'] else ''}")
                c.state = "crossed"
            else:
                ev["status"], ev["confirmed"] = "failed", now
                c.event, c.state = None, "fell back"
                print(f"FAILED {c.symbol} minute {k_ev}: closed below the graduation price", flush=True)
            self.save(ev)
        # 3. what a buy at the close of the crossing minute did (+100% / -40% / 15 minutes), and under the pattern strategy's exit (PATTERN_EXIT)
        if ev["status"] == "confirmed" and ev.get("outcome") is None:
            e = k_ev + 1
            px = A.PATTERN_EXIT
            if ev.get("outcome2") is None and now >= c.grad_min + (e + px[2] + 1) * MIN + SETTLE:
                g2, reason2, em2 = A.simulate(arr, e, px[0], px[1], [px[2]])
                ev["outcome2"] = {"net": fnum(A.net_return(arr, trd, e, g2[px[2]])[0]), "exit": {1: "stop", 2: "target", 3: "time"}[int(reason2[0])], "held": int(em2[0]) - e + 1}
                self.save(ev)
            if now >= c.grad_min + (e + EXIT[2] + 1) * MIN + SETTLE:
                g, reason, em = A.simulate(arr, e, EXIT[0], EXIT[1], [EXIT[2]])
                r = A.net_return(arr, trd, e, g[EXIT[2]])[0]
                entry = close[e - 1]
                ev["outcome"] = {"net": fnum(r), "exit": {1: "stop", 2: "target", 3: "time"}[int(reason[0])], "held": int(em[0]) - e + 1,
                                 "peak": fnum(np.nanmax(arr["high"][0, e:e + EXIT[2]]) / entry - 1)}
                c.done = True
                print(f"RESULT {c.symbol}: {ev['outcome']}", flush=True)
                self.save(ev)
            else:
                ev["now_mult"] = fnum(close[min(int((now - c.grad_min) // MIN), A.T - 1)] / close[e - 1])

    # ---- the page ----
    def publish(self, now):
        watching, other = [], 0
        for c in sorted(self.coins.values(), key=lambda c: -c.grad):
            age = now - c.grad
            if age > WATCH_AGE or c.price is None:
                continue
            if c.state in ("not at the standard price", "missed", "crossed") or c.state.startswith("out:"):    # not (or no longer) a candidate
                if c.state != "crossed":
                    other += 1
                continue
            watching.append({"mint": c.mint, "symbol": c.symbol, "age": round(age / MIN, 1), "mcap": c.price, "vs_grad": fnum(c.price / (c.ref * 1e9), 3), "ref_mcap": fnum(c.ref * 1e9, 1), "rows": c.rows,
                             "below": c.below, "state": c.state, "x": bool((c.info.get("twitter") or "").strip()), "web": bool((c.info.get("website") or "").strip()),
                             "mtg": fnum(max(0.0, (c.grad_min - c.info["created_ts"]) / MIN), 1) if c.info.get("created_ts") else None})
        evs = None
        for _ in range(3):          # (a worker may be adding a key to an event right now)
            try:
                evs = sorted(copy.deepcopy(list(self.events.values())), key=lambda e: -e["detected"])
                break
            except RuntimeError:
                time.sleep(0.05)
        if evs is None:
            return
        conf = [e for e in evs if e["status"] == "confirmed" and e.get("outcome")]
        pat = [e for e in evs if e["status"] == "confirmed" and (e.get("flags") or {}).get("pattern")]

        def stat(sel, key="outcome"):
            r = [e[key]["net"] for e in sel if e.get(key) and e[key]["net"] is not None]
            return {"n": len(r), "mean": fnum(float(np.mean(r))) if r else None, "won": int(sum(x > 0 for x in r)), "total": fnum(float(np.sum(r))) if r else None}
        stats = {"events": len(evs), "confirmed": sum(e["status"] == "confirmed" for e in evs), "failed": sum(e["status"] == "failed" for e in evs), "finished": len(conf),
                 "all": stat(conf), "group": stat([e for e in conf if e["flags"]["group"]]), "quiet": stat([e for e in conf if e["flags"]["group"] and e["flags"]["quiet"]]),
                 "quiet500": stat([e for e in conf if e["flags"]["group"] and e["flags"]["quiet500"]]),
                 "pattern": stat([e for e in pat if e.get("outcome2")], "outcome2"), "pattern_events": len(pat), "pattern_old_exit": stat([e for e in pat if e.get("outcome")]),
                 "sellers_gone": stat([e for e in conf if (e.get("post") or {}).get("sellers_gone")]),
                 "not_sellers_gone": stat([e for e in conf if e.get("post") and not e["post"].get("error") and not e["post"].get("sellers_gone")])}
        doc = {"updated": now, "p0_mcap": round(self.p0 * 1e9), "watching": watching, "ignored": other, "events": evs[:100], "stats": stats,
               "rule": {"first": A.CROSS_FIRST, "last": A.CROSS_LAST, "floor_mcap": A.CROSS_FLOOR_MCAP, "pre_mcap": A.CROSS_PRE_MCAP, "start_mcap": A.CROSS_START_MCAP, "group": GROUP, "quiet": QUIET,
                        "exit": EXIT, "pattern_exit": A.PATTERN_EXIT, "sell_share_max": SELL_SHARE_MAX, "buyers_min": BUYERS_MIN}}
        W.write_json(os.path.join(self.data, "crossing.json"), doc)

    def run(self):
        print(f"crossing monitor started; graduation price ${self.p0 * 1e9 / 1e3:.1f}K market cap; {len(self.events)} events on record", flush=True)
        while True:
            now = now_ms()
            try:
                self.refresh(now)
                for c in list(self.coins.values()):
                    if not c.busy and now >= c.next_at and (not c.done or c.pending()):
                        c.busy = True
                        self.pool.submit(self.work, c)
                if time.time() - self.last_publish >= 3:
                    self.last_publish = time.time()
                    self.publish(now)
            except Exception as e:      # the loop must keep running whatever happens
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
    ap.add_argument("--probe", action="store_true", help="read the young graduates once, print what the detector sees, and exit")
    args = ap.parse_args()
    p0, _ = W.report_extras(args.data)
    mon = Monitor(args.data, p0)
    if args.probe:
        now = now_ms()
        mon.refresh(now)
        for c in sorted(mon.coins.values(), key=lambda c: c.grad):
            if now - c.grad_min > WATCH_AGE:
                continue
            c.next_at = 0
            mon.step(c)
            print(f"{c.symbol:12} age {(now - c.grad) / MIN:5.1f} min  cap ${c.price or 0:>9,.0f}  below {c.below}  {c.state}")
        mon.publish(now_ms())
        return
    mon.run()


if __name__ == "__main__":
    sys.exit(main())

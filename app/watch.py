#!/usr/bin/env python3
"""Paper-trade every strategy on tokens as they graduate, and say why each bought or passed.

Runs forever. The strategies are the automatic search's current rule, every enabled rule of
the Rule settings tab and the built-in ones in rules.WATCH_EXTRA. All are recorded; the web
app shows the rules marked "Record live", or all of them when none is marked. Every new graduate that holders.py lists is followed: at each
strategy's entry minute its 1-minute bars are fetched and measured exactly as the backtest
measures them (the same analyze.features), the strategy's conditions are checked one by one,
and the token is marked bought or passed for that strategy, with every measured value kept.
A bought token is then followed minute by minute until its take profit, stop loss or time
stop, and the profit or loss at each minute is kept for the chart. Tokens a strategy passed
get the result they would have had, so a strategy can be judged on tokens that did not exist
when it was written. Nothing is bought: this is a record.

Writes watch.db (its own database, like holders.db) and watch.json for the web app.

Usage: python3 watch.py [--data data]
"""
import argparse
import csv
import io
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

import numpy as np

import analyze as A
import collector as C
import first_trades
import notify
import rules as rulebook

MIN = 60000
LOOKBACK = 3 * 3600      # on start, also take graduates first seen this recently
SETTLE = 2               # seconds after a minute ends before its bar is read (measured: final within 1 s)
DUE_FOR = 180            # a decision this many seconds past its minute is no longer hurried (catch-up handles it)
LATE = 180               # a decision made this many seconds after its minute is marked late
MAX_TRIES = 30           # failed lookups (one a minute) before a token is given up
LISTED = 7 * 86400       # the web app lists the tokens of this many days; the database keeps every one
FOLLOW = 120             # minutes a bought token's price is kept after the buy, whatever the exit was
ALT_TPS = (1.0, 2.0)     # every buy is also run with these take profits, to show what aiming higher would have done
CORE = ("entry", "conditions", "tp", "sl", "hold")

SCHEMA = """
DROP TABLE IF EXISTS watch;          -- the first version kept one rule's decision per token
CREATE TABLE IF NOT EXISTS tokens (
  mint TEXT PRIMARY KEY,
  first_seen_ts INTEGER NOT NULL,   -- unix ms holders.py first listed it as graduated
  status TEXT NOT NULL,             -- new, active, done, unchecked (not a standard launch), failed
  tries INTEGER NOT NULL DEFAULT 0,
  info TEXT,                        -- JSON: name, symbol, image, links, created_ts, pool, classic, dev_buy_sol
  graduated_ts INTEGER, graduated_src TEXT
);
CREATE TABLE IF NOT EXISTS measures (  -- the token as it looked at one entry minute
  mint TEXT NOT NULL, entry INTEGER NOT NULL,
  measured_ts INTEGER, tradable INTEGER, traded INTEGER, impact REAL, entry_price REAL,
  cluster TEXT, features TEXT,
  PRIMARY KEY (mint, entry)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS decisions (
  mint TEXT NOT NULL, strategy TEXT NOT NULL,
  rule TEXT NOT NULL,               -- JSON: the strategy as it was when it decided
  entry INTEGER NOT NULL, decided_ts INTEGER, late INTEGER,
  pick INTEGER, checks TEXT,        -- JSON: every condition with the measured value
  status TEXT NOT NULL,             -- open, closed
  exit_reason TEXT, minutes_held INTEGER, net_return REAL,
  path TEXT,                        -- JSON, bought tokens only: [minutes held, return if sold then]
  full_path TEXT,                   -- JSON, bought tokens only: the same for FOLLOW minutes, ignoring the exit
  alts TEXT,                        -- JSON, bought tokens only: the same buy with each take profit of ALT_TPS,
                                    -- the same stop loss and FOLLOW minutes as the time limit
  notified INTEGER, notified_exit INTEGER,   -- 1 once the Telegram message about the buy / the sale was sent
  PRIMARY KEY (mint, strategy)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_sent (day TEXT PRIMARY KEY, ts INTEGER);   -- daily reports sent to Telegram
CREATE INDEX IF NOT EXISTS tokens_status ON tokens(status);
CREATE INDEX IF NOT EXISTS decisions_open ON decisions(status);
CREATE INDEX IF NOT EXISTS decisions_strategy ON decisions(strategy);
"""


def read_only(path):
    """Open a database for reading. A copied database that lost its -wal file refuses a
    read-only open, so fall back to a plain one."""
    try:
        db = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=60)
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()   # the open itself is lazy
        return db
    except sqlite3.Error:
        return sqlite3.connect(path, timeout=60)


def labelled(conditions):
    return [{**c, "label": rulebook.FEATURES[c["feature"]][0], "kind": rulebook.FEATURES[c["feature"]][1]}
            for c in conditions if c["feature"] in rulebook.FEATURES]


def load_strategies(data):
    """The automatic search's rule and every enabled saved rule, each with its backtest record."""
    out = []
    try:
        with closing(read_only(os.path.join(data, "pump.db"))) as db:
            saved = [r for r in rulebook.load(os.path.join(data, "settings")) if r["enabled"]]
            chosen = [r for r in saved if r["live"]]
            row = db.execute("SELECT run_id, picked_rule FROM backtest_runs WHERE kind = 'search' "
                             "AND picked_rule IS NOT NULL ORDER BY run_id DESC LIMIT 1").fetchone()
            # every strategy is recorded; the web app shows the rules marked "record live",
            # or all of them when none is marked
            if row:
                r = json.loads(row[1])
                out.append({"id": "auto", "name": "Automatic search (best rule found)", "run_id": row[0],
                            "shown": not chosen, **{k: r[k] for k in CORE}})
            for r in rulebook.with_extras(saved):
                if r["enabled"]:
                    hit = db.execute("SELECT MAX(run_id) FROM backtest_runs WHERE kind = 'rule' AND rule_id = ? "
                                     "AND picked_rule IS NOT NULL", (r["id"],)).fetchone()
                    out.append({"id": r["id"], "name": r["name"], "run_id": hit[0],
                                "shown": r["live"] or not chosen, **{k: r[k] for k in CORE}})
            for s in out:
                s["conditions"] = labelled(s["conditions"])
                n, mean, wins = db.execute("SELECT COUNT(*), AVG(net_return), SUM(net_return > 0) FROM backtest_trades "
                                           "WHERE run_id = ?", (s["run_id"],)).fetchone() if s["run_id"] else (0, None, 0)
                te = db.execute("SELECT COUNT(*), AVG(net_return) FROM backtest_trades WHERE run_id = ? "
                                "AND split = 'test'", (s["run_id"],)).fetchone() if s["run_id"] else (0, None)
                s["backtest"] = {"trades": n, "mean": mean, "wins": wins or 0, "recent_trades": te[0], "recent_mean": te[1]}
    except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
        print(f"  ! strategies could not be read: {e!r}", flush=True)
        return None
    return out


def report_extras(data):
    """Completion price and each cluster's record, from the latest report."""
    p_grad, clusters = 4.76e-5, {}
    try:
        with open(os.path.join(data, "report.json"), encoding="utf-8") as f:
            rep = json.load(f)
        p_grad = rep["assumptions"]["grad_mcap_usd"] / 1e9
        for c in rep.get("clusters", {}).get("clusters", []):
            clusters[c["id"]] = [{"entry": x["entry"], "trades": x["all"]["trades"], "mean": x["all"]["mean"],
                                  "collapse_30": x["collapse_30"]} for x in c["by_entry"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return p_grad, clusters


def coin_info(data, mint, listed):
    """What is known about the coin: holders.py's listing row, else the collector's."""
    c = first = None
    if listed:
        try:
            c = first = json.loads(listed)
        except ValueError:
            c = None
    # a listing row saved before the curve fields were kept cannot say what kind of launch it was
    if c is None or "virtual_sol_reserves" not in c:
        try:
            with closing(read_only(os.path.join(data, "pump.db"))) as db:
                row = db.execute("SELECT raw FROM tokens WHERE mint = ?", (mint,)).fetchone()
            c = json.loads(row[0]) if row else first
        except (sqlite3.Error, ValueError, TypeError):
            c = first
    if c is None:
        return None
    return {"classic": C.is_classic(c) if "virtual_sol_reserves" in c else None, "name": c.get("name"),
            "symbol": c.get("symbol"), "image_uri": c.get("image_uri"),
            "twitter": c.get("twitter") or "", "telegram": c.get("telegram") or "", "website": c.get("website") or "",
            "created_ts": c.get("created_timestamp"), "pool": c.get("pump_swap_pool"), "creator": c.get("creator")}


def fast_get(path, params):
    """C.get for a decision that is due now: a refused or slow request is tried again after
    0.5, 1 and 2 s rather than after the shared pause of 10 s or more."""
    url = C.API + path + "?" + urllib.parse.urlencode(params)
    for pause in (0.5, 1, 2, None):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=C.HEADERS), timeout=8) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:   # timeouts, resets, bad json
            err = e
        if pause is None:
            raise RuntimeError(f"GET {url} failed: {err}")
        time.sleep(pause)


def fetch_bars(mint, pool, to_ms=None, fast=False):
    """(300 1-minute bars ending now, or at `to_ms` for a token looked at later; graduation
    slot or None). Raises when there are none. fast: for a decision due now."""
    params = {"res": "1m", "count": 300, "to": min(to_ms or 10 ** 15, int(time.time() * 1000))}
    if pool:
        params["pool"] = pool
    r = (fast_get(f"/candles/{C.CHAIN}/{mint}", params) if fast else
         C.get(f"/candles/{C.CHAIN}/{mint}", params, retries=3)) or {}
    marker = r.get("graduatedAt")
    bars = r.get("candles") or []
    if not bars:
        raise RuntimeError("no bars")
    return bars, (int(marker.split("-")[0]) or None) if marker else None


def slot_time(slot):
    """Unix ms of a Solana slot, or None."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getBlockTime", "params": [slot]}).encode()
    try:
        req = urllib.request.Request(C.RPC, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            t = json.load(r).get("result")
        return int(t) * 1000 if t else None
    except Exception:  # the price fallback covers it
        return None


def graduation(bars, slot, created, p_grad):
    """(graduation time in ms, source): pump.fun's marker, else the first minute the completion price traded."""
    t = slot_time(slot) if slot else None
    if t and (not created or t >= created - MIN):
        return t, "slot"
    for b in bars:
        if float(b["high"]) >= 0.97 * p_grad:
            return int(b["openTimeMs"]), "price"
    return None, None


def arrays(bars, grad_min, p_grad):
    """One coin's bars in the shape analyze.py works on (1 x T, minute 0 = graduation minute)."""
    arr = {k: np.full((1, A.T), np.nan, dtype=np.float32) for k in ("open", "high", "low", "close")}
    vol = np.zeros((1, A.T), dtype=np.float32)
    trd = np.zeros((1, A.T), dtype=np.float32)
    for b in bars:
        m = (int(b["openTimeMs"]) - grad_min) // MIN
        if 0 <= m < A.T:
            for k in arr:
                arr[k][0, m] = float(b[k])
            vol[0, m], trd[0, m] = float(b["volumeUsd"]), b["trades"]
    close = arr["close"]
    for m in range(1, A.T):
        if np.isnan(close[0, m]):
            close[0, m] = close[0, m - 1]
    prev = np.concatenate([close[:, :1], close[:, :-1]], axis=1)
    for k in ("open", "high", "low"):
        arr[k] = np.where(np.isnan(arr[k]), prev, arr[k])
    arr["p_grad"] = p_grad
    arr["stake"] = A.stake_usd(p_grad)
    return arr, vol, trd


def holder_rows(data, mint):
    try:
        with closing(sqlite3.connect(os.path.join(data, "holders.db"), timeout=30)) as h:
            rows = [row for _, row in A.holder_rows(h, mint)]
    except sqlite3.Error:
        rows = []
    return np.array(rows, dtype=np.float64) if rows else None


def dev_buy(mint, quick=False):
    """SOL the creator bought at launch. Raises when it cannot be looked up: without it a
    creator-bought curve would look like an organic launch. quick: one short try (a decision is due)."""
    _, t = first_trades.fetch(mint, quick=quick) if quick else first_trades.fetch(mint)
    if not isinstance(t, dict):
        raise RuntimeError("first trade not available")
    return float(t["amountSol"]) if t.get("isDevBuy") and t.get("amountSol") else 0.0


def token_frame(data, mint, info, grad_min):
    """The per-coin facts analyze.features needs, for one coin."""
    created = info.get("created_ts")
    flags = {k: float(bool((info.get(k) or "").strip())) for k in ("twitter", "website", "telegram")}
    tok = {"mint": [mint], "graduated_ts": np.array([grad_min], dtype=np.int64),
           "dev_buy_sol": np.array([info["dev_buy_sol"]]),
           "mins_to_grad": np.array([max(0.0, (grad_min - created) / MIN) if created else np.nan]),
           "has_twitter": np.array([flags["twitter"]]), "has_website": np.array([flags["website"]]),
           "has_telegram": np.array([flags["telegram"]]), "socials": np.array([float(any(flags.values()))]),
           "holder_snaps": {}}
    snaps = holder_rows(data, mint)
    if snaps is not None:
        tok["holder_snaps"][0] = snaps
    return tok


def num(x, digits=6):
    x = float(x)
    return float(f"{x:.{digits}g}") if np.isfinite(x) else None


def measure(data, mint, info, grad_min, e, frame):
    """The token at entry minute e: what analyze.features sees, and whether it could be bought."""
    arr, vol, trd = frame
    f = A.features(token_frame(data, mint, info, grad_min), arr, vol, trd, e)
    p0 = float(arr["close"][0, e - 1])
    with np.errstate(divide="ignore", invalid="ignore"):
        impact = arr["stake"] / float(A.depth(arr, np.array([p0]))[0]) if p0 > 0 else np.inf
    return {"f": f, "tradable": bool(np.isfinite(A.net_return(arr, trd, e, np.ones(1))[0])),
            "traded": bool(trd[0, e - 5:e].sum() > 0), "impact": num(impact), "entry_price": num(p0, 9),
            "cluster": rulebook.CLUSTERS[int(A.cluster_labels(f)[0])]["id"]}


def check(strategy, f):
    return [{"feature": c["feature"], "op": c["op"], "value": c["value"], "actual": num(f[c["feature"]][0], 9),
             "ok": bool(A.matches(f, [c])[0])} for c in strategy["conditions"]]


def follow(rule, frame, done_minutes, want_path):
    """The rule's exit applied to the bars that are complete so far.

    Returns (finished, exit reason, minutes held, net return, path). Not finished while
    neither the target nor the stop was hit and the time stop is still ahead."""
    arr, vol, trd = frame
    e = rule["entry"]
    full = min(rule["hold"], A.T - e)
    held = min(full, done_minutes - e)
    if held < 1:
        return False, None, None, None, []
    by_hold, reason, exit_min = A.simulate(arr, e, rule["tp"], rule["sl"], [held])
    r = float(A.net_return(arr, trd, e, by_hold[held])[0])
    k = int(reason[0])
    finished = k in (1, 2) or held >= full
    last = int(exit_min[0])
    path = []
    if want_path:
        p0 = float(arr["close"][0, e - 1])
        for m in range(e, last + 1):
            g = np.array([float(arr["close"][0, m]) / p0])
            path.append([m - e + 1, num(A.net_return(arr, trd, e, g)[0], 4)])
        if finished and path and np.isfinite(r):
            path[-1][1] = num(r, 4)   # the last point is what the sale really fetched
    return finished, {1: "stop", 2: "target", 3: "time"}[k], last - e + 1, r if np.isfinite(r) else None, path


def held_path(rule, frame, done_minutes):
    """What a bought token would have returned if simply held and sold at the end of each
    minute, for up to FOLLOW minutes after the buy, ignoring the target and the stop."""
    arr, vol, trd = frame
    e = rule["entry"]
    p0 = float(arr["close"][0, e - 1])
    out = []
    for m in range(e, min(e + FOLLOW, done_minutes, A.T)):
        px = float(arr["close"][0, m])
        r = A.net_return(arr, trd, e, np.array([px / p0]))[0] if p0 > 0 and np.isfinite(px) else np.nan
        out.append([m - e + 1, num(r, 4)])
    return out


def alternatives(rule, frame, done_minutes, before):
    """The same buy with a higher take profit: same stop loss, sold at the target, at the stop
    or FOLLOW minutes after the buy. One that has finished is kept as it is."""
    kept = {a["tp"]: a for a in before or [] if a.get("done")}
    out = []
    for tp in ALT_TPS:
        if tp in kept:
            out.append(kept[tp])
            continue
        done, reason, held, r, path = follow({**rule, "tp": tp, "hold": FOLLOW}, frame, done_minutes, True)
        out.append({"tp": tp, "done": bool(done), "exit": reason, "held": held, "net": r, "path": path})
    return out


ALERT_FOR = 15 * 60      # a buy older than this is no longer worth an alert
CATCH_UP_AFTER = 6 * 3600   # a token this long past graduation is only being caught up from history
CATCH_UP_PER_ROUND = 4      # how many of those are worked per round of the main loop


def _usd(v):
    if v is None:
        return "no data"
    return f"${v / 1e6:.2f}M" if abs(v) >= 1e6 else f"${v / 1e3:.1f}K" if abs(v) >= 1e3 else f"${v:.2f}"


def _pct(v, digits=1):
    return "no data" if v is None else f"{v * 100:+.{digits}f}%"


def _share(v):
    """A share of the supply: a percentage that is never negative, so without a sign."""
    return "no data" if v is None else f"{v * 100:.1f}%"


def _shown(v, kind, name=""):
    if v is None:
        return "no data"
    if kind == "pct" and ("_pct" in name):
        return _share(v)
    return (_pct(v) if kind == "pct" else _usd(v) if kind == "usd" else f"{v:.2f} SOL" if kind == "sol"
            else f"{v:.1f} min" if kind == "min" else ("yes" if v else "no") if kind == "flag" else f"{v:,.0f}")


def buy_message(strategy, rule, info, mint, m, f, checks):
    """The Telegram text for one buy: what was bought, why, and what the token looked like."""
    e, g = notify.esc, lambda k: f.get(k)
    name = {c["id"]: c["name"] for c in rulebook.CLUSTERS}.get(m["cluster"], "")
    lines = [
        f"🟢 <b>BUY (paper)</b>: <b>{e(info.get('name') or mint[:8])}</b> ({e(info.get('symbol') or '')})",
        f"Strategy: <b>{e(strategy)}</b>",
        f"Bought {rule['entry']} min after graduation at {_usd((m['entry_price'] or 0) * 1e9)} market cap, "
        f"{A.STAKE_SOL:g} SOL stake. Sells at {_pct(rule['tp'], 0)}, {_pct(-rule['sl'], 0)} or after {rule['hold']} min.",
        "",
        "<b>Why it was bought</b>",
        *[f"✓ {e(rulebook.FEATURES[c['feature']][0])}: {_shown(c['actual'], rulebook.FEATURES[c['feature']][1], c['feature'])}"
          for c in checks],
        "",
        "<b>The token at that minute</b>",
        f"Kind: {e(name)}",
        f"Price: {_pct(g('ret_since_grad'), 0)} since graduation, {_pct(g('drawdown_from_high'), 0)} from its high, "
        f"{_pct(g('ret_last_5m'), 0)} over 5 min",
        f"Volume: {_usd(g('vol_usd_last_5m'))} in 5 min ({_shown(g('trades_last_5m'), 'num')} trades, "
        f"{_usd(g('avg_trade_usd_5m'))} average), {_usd(g('vol_usd_total'))} since graduation",
        f"Holders: {_shown(g('holders'), 'num')} ({_pct(g('holders_change_5m'))} in 5 min)",
        f"Largest wallet {_share(g('top1_pct'))}, top 10 {_share(g('top10_pct'))}, top 50 {_share(g('top50_pct'))}",
        f"Snipers {_share(g('snipers_pct'))}, bundled wallets {_share(g('bundlers_pct'))}",
        f"Creator: bought {_shown(g('dev_buy_sol'), 'sol')} at launch, held {_share(g('dev_pct_at_grad'))} at graduation",
        f"Graduated {_shown(g('mins_to_grad'), 'min')} after creation",
        "",
        f"<a href=\"https://pump.fun/coin/{e(mint)}\">Chart on pump.fun</a>"
        + "".join(f" · <a href=\"{e(info[k])}\">{label}</a>" for k, label in (("twitter", "X"), ("website", "Website"))
                  if str(info.get(k) or "").startswith(("http://", "https://"))),
        f"<code>{e(mint)}</code>",
        "Nothing was bought: this is a simulated trade.",
    ]
    return "\n".join(lines)


def send_alerts(db, data, strategies):
    """Telegram messages for live buys by the shown strategies, and for how each ended."""
    cfg = notify.load(os.path.join(data, "settings"))
    if not notify.ready(cfg):
        return
    names = {s["id"]: s["name"] for s in strategies if s["shown"]}
    if not names:
        return
    marks = ",".join("?" * len(names))
    since = int((time.time() - ALERT_FOR) * 1000)
    for mint, sid, rule, checks, info in db.execute(
            f"SELECT d.mint, d.strategy, d.rule, d.checks, t.info FROM decisions d JOIN tokens t USING (mint) "
            f"WHERE d.pick = 1 AND d.late = 0 AND d.notified IS NULL AND d.decided_ts > ? AND d.strategy IN ({marks})",
            (since, *names)).fetchall():
        rule = json.loads(rule)
        row = db.execute("SELECT entry_price, cluster, features FROM measures WHERE mint = ? AND entry = ?",
                         (mint, rule["entry"])).fetchone()
        if not row:
            continue
        text = buy_message(names[sid], rule, json.loads(info), mint, {"entry_price": row[0], "cluster": row[1]},
                           json.loads(row[2] or "{}"), json.loads(checks or "[]"))
        sent, why = notify.send(cfg, text)
        if not sent:
            print(f"  ! Telegram: {why}", flush=True)
            return      # tried again on the next round, until the buy is too old to matter
        db.execute("UPDATE decisions SET notified = 1 WHERE mint = ? AND strategy = ?", (mint, sid))
        db.commit()
    for mint, sid, reason, held, r, info in db.execute(
            "SELECT d.mint, d.strategy, d.exit_reason, d.minutes_held, d.net_return, t.info FROM decisions d "
            "JOIN tokens t USING (mint) WHERE d.pick = 1 AND d.notified = 1 AND d.status = 'closed' "
            "AND d.notified_exit IS NULL").fetchall():
        info = json.loads(info)
        how = {"target": "take profit", "stop": "stop loss", "time": "time stop"}.get(reason, reason)
        text = (f"{'✅' if (r or 0) > 0 else '🔴'} <b>SOLD (paper)</b>: <b>{notify.esc(info.get('name') or mint[:8])}</b> "
                f"({notify.esc(info.get('symbol') or '')})\nStrategy: <b>{notify.esc(names.get(sid, sid))}</b>\n"
                f"{_pct(r)} ({'+' if (r or 0) >= 0 else '-'}{abs((r or 0) * A.STAKE_SOL):.3f} SOL) by {how} after {held} min.\n"
                f"<a href=\"https://pump.fun/coin/{notify.esc(mint)}\">Chart on pump.fun</a>")
        sent, why = notify.send(cfg, text)
        if not sent:
            print(f"  ! Telegram: {why}", flush=True)
            return
        db.execute("UPDATE decisions SET notified_exit = 1 WHERE mint = ? AND strategy = ?", (mint, sid))
        db.commit()


def reopen(db, strategies):
    """Finished tokens that a shown strategy never decided on (it was added, or ticked, after
    they were done) are worked again, from their stored history. Returns how many."""
    n = 0
    for s in strategies:
        if s["shown"]:
            n += db.execute("UPDATE tokens SET status = 'active', tries = 0 WHERE status = 'done' AND mint NOT IN "
                            "(SELECT mint FROM decisions WHERE strategy = ?)", (s["id"],)).rowcount
    db.commit()
    return n


def setback(db, mint, why):
    """Count a failed step for one token; after MAX_TRIES it is dropped instead of retried forever."""
    print(f"  ! {mint}: {why}", flush=True)
    db.execute("UPDATE tokens SET tries = tries + 1 WHERE mint = ?", (mint,))
    db.execute("UPDATE tokens SET status = 'failed' WHERE mint = ? AND tries >= ?", (mint, MAX_TRIES))
    db.commit()


def live_record(db, strategies):
    """What each strategy has done on watched tokens so far, over everything ever recorded."""
    for s in strategies:
        n, picks = db.execute("SELECT COUNT(*), SUM(pick) FROM decisions WHERE strategy = ?", (s["id"],)).fetchone()
        fin, total, wins, best, worst = db.execute(
            "SELECT COUNT(*), SUM(net_return), SUM(net_return > 0), MAX(net_return), MIN(net_return) FROM decisions "
            "WHERE strategy = ? AND pick = 1 AND status = 'closed' AND net_return IS NOT NULL", (s["id"],)).fetchone()
        skipped, skip_mean = db.execute(
            "SELECT COUNT(*), AVG(net_return) FROM decisions WHERE strategy = ? AND pick = 0 AND status = 'closed' "
            "AND net_return IS NOT NULL", (s["id"],)).fetchone()
        # the same finished buys, had each simply been held to its time stop instead
        held = [v for v, in db.execute(
            "SELECT json_extract(full_path, '$[' || (json_extract(rule, '$.hold') - 1) || '][1]') FROM decisions "
            "WHERE strategy = ? AND pick = 1 AND status = 'closed' AND full_path IS NOT NULL", (s["id"],)) if v is not None]
        s["live"] = {"checked": n, "picks": picks or 0, "finished": fin, "total": total or 0.0, "wins": wins or 0,
                     "best": best, "worst": worst, "skipped": skipped, "skip_mean": skip_mean,
                     "held_n": len(held), "held_mean": sum(held) / len(held) if held else None}


def token_detail(db, mint):
    """Everything recorded about one token, for every strategy: what the web app shows when
    the token is opened."""
    seen, status, info, grad, src = db.execute(
        "SELECT first_seen_ts, status, info, graduated_ts, graduated_src FROM tokens WHERE mint = ?", (mint,)).fetchone()
    ms = {str(e): {"tradable": tr, "traded": td, "impact": imp, "price": px, "cluster": cl,
                   "f": json.loads(f) if f else None}
          for e, tr, td, imp, px, cl, f in db.execute(
              "SELECT entry, tradable, traded, impact, entry_price, cluster, features FROM measures "
              "WHERE mint = ? ORDER BY entry", (mint,))}
    ds = []
    for s, rule, e, dt, late, pick, ch, st, ex, held, net, path, full, alts in db.execute(
            "SELECT strategy, rule, entry, decided_ts, late, pick, checks, status, exit_reason, minutes_held, "
            "net_return, path, full_path, alts FROM decisions WHERE mint = ? ORDER BY entry, strategy", (mint,)):
        rule = json.loads(rule)
        ds.append({"s": s, "entry": e, "decided_ts": dt, "late": late, "pick": pick,
                   "checks": json.loads(ch) if ch else [], "status": st, "exit": ex, "held": held, "net": net,
                   "path": json.loads(path) if path else None, "full": json.loads(full) if full else None,
                   "alts": json.loads(alts) if alts else None, "tp": rule["tp"], "sl": rule["sl"], "hold": rule["hold"]})
    return {"mint": mint, "first_seen_ts": seen, "status": status, "info": json.loads(info) if info else None,
            "graduated_ts": grad, "graduated_src": src, "measures": ms, "decisions": ds}


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(A.clean(doc), f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


DATA_DIR = "data"   # set by main: where the daily report files live
_was_live = set()   # tokens still changing at the last publish: each gets one more write after it settles


def publish(db, data, strategies, clusters, p_grad):
    """For the web app: watch.json with one short row for every token kept and the shown
    strategies' decisions on it, and watch_tokens/<mint>.json with a token's full detail,
    loaded when the token is opened. Strategies that are not shown are recorded all the same."""
    global _was_live
    hidden = sum(not s["shown"] for s in strategies)
    # every recorded strategy, shown or not, for the Trading tab's choice of what to trade
    tradable = [{"id": s["id"], "name": s["name"], "shown": s["shown"], "entry": s["entry"], "tp": s["tp"], "sl": s["sl"],
                 "hold": s["hold"], "backtest": s.get("backtest")} for s in strategies]
    strategies = [s for s in strategies if s["shown"]]
    shown = {s["id"] for s in strategies}
    live_record(db, strategies)

    folder = os.path.join(data, "watch_tokens")
    os.makedirs(folder, exist_ok=True)
    have = {f[:-5] for f in os.listdir(folder) if f.endswith(".json")}
    since = int((time.time() - LISTED) * 1000)
    kept = {m for m, in db.execute("SELECT mint FROM tokens WHERE status != 'failed' AND first_seen_ts >= ?", (since,))}
    live = {m for m, in db.execute("SELECT mint FROM tokens WHERE status IN ('new', 'active')")}
    for mint in (live | _was_live | (kept - have)) & kept:
        write_json(os.path.join(folder, mint + ".json"), token_detail(db, mint))
    for mint in have - kept:            # no longer listed; its record stays in the database
        os.remove(os.path.join(folder, mint + ".json"))
    _was_live = live

    first = {m: (cl, mc, v5, h) for m, _, cl, mc, v5, h in db.execute(
        "SELECT mint, MIN(entry), cluster, json_extract(features, '$.mcap_usd'), "
        "json_extract(features, '$.vol_usd_last_5m'), json_extract(features, '$.holders') FROM measures GROUP BY mint")}
    decided = {}
    if shown:
        marks = ",".join("?" * len(shown))
        for m, sid, e, pick, st, net, late in db.execute(
                f"SELECT mint, strategy, entry, pick, status, net_return, late FROM decisions "
                f"WHERE strategy IN ({marks}) ORDER BY entry, strategy", tuple(shown)):
            decided.setdefault(m, []).append({"s": sid, "entry": e, "pick": pick, "status": st, "net": net, "late": late})
    rows = []
    for mint, seen, status, name, symbol, grad in db.execute(
            "SELECT mint, first_seen_ts, status, json_extract(info, '$.name'), json_extract(info, '$.symbol'), "
            "graduated_ts FROM tokens WHERE status != 'failed' AND first_seen_ts >= ? "
            "ORDER BY COALESCE(graduated_ts, first_seen_ts) DESC", (since,)):
        cl, mc, v5, h = first.get(mint, (None, None, None, None))
        rows.append({"mint": mint, "first_seen_ts": seen, "status": status, "name": name, "symbol": symbol,
                     "graduated_ts": grad, "cluster": cl, "mcap": mc, "vol5": v5, "holders": h,
                     "d": decided.get(mint, [])})
    write_json(os.path.join(data, "watch.json"), {
        "generated_at": int(time.time() * 1000), "stake_usd": A.stake_usd(p_grad), "stake_sol": A.STAKE_SOL,
        "sol_usd": A.sol_usd(p_grad), "strategies": strategies, "background": hidden, "tradable": tradable,
        "clusters": {c["id"]: {"name": c["name"], "note": c["note"], "history": clusters.get(c["id"], [])}
                     for c in rulebook.CLUSTERS},
        "features": {k: {"label": v[0], "kind": v[1]} for k, v in rulebook.FEATURES.items()},
        "days": [{"strategy": s["id"], **{k: v for k, v in day_numbers(db, s["id"], d).items() if k != "list"}}
                 for s in strategies for d in [utc((time.time() - k * 86400) * 1000, "%Y-%m-%d") for k in range(DAYS_SHOWN)]],
        "rows": rows})


REPORT_HEAD = ["graduated_utc", "token", "symbol", "mint", "strategy", "strategy_id", "entry_minute", "decision",
               "decided_live", "kind_of_token", "buy_utc", "buy_price_usd", "buy_market_cap_usd", "take_profit_price_usd",
               "stop_loss_price_usd", "sell_utc", "sold_by", "minutes_held", "result_pct",
               "profit_sol", "if_held_to_time_stop_pct", "if_target_100_pct", "if_target_200_pct", "failed_conditions"]


def utc(ms, fmt="%Y-%m-%d %H:%M:%S"):
    return time.strftime(fmt, time.gmtime(ms / 1000)) if ms else ""


def write_reports(db, data, strategies, days=None):
    """One CSV per UTC day of graduation in reports/<date>.csv: every strategy's decision on every
    token that graduated that day, with the buy, the sale and everything measured at the entry
    minute. `days` limits the rewrite to those dates; None rewrites every day on record."""
    folder = os.path.join(data, "reports")
    os.makedirs(folder, exist_ok=True)
    names = {s["id"]: s["name"] for s in strategies}
    feats = list(rulebook.FEATURES)
    if days is None:
        days = [d for d, in db.execute("SELECT DISTINCT date(graduated_ts / 1000, 'unixepoch') FROM tokens "
                                       "WHERE graduated_ts IS NOT NULL ORDER BY 1")]
    pct = lambda v: "" if v is None else round(v * 100, 2)   # noqa: E731
    price = lambda v: f"{v:.12f}".rstrip("0")                # noqa: E731  a plain decimal, never 8.5e-05
    for day in days:
        rows = db.execute(
            "SELECT t.graduated_ts, t.info, t.mint, d.strategy, d.rule, d.entry, d.pick, d.late, d.checks, d.status, "
            "d.exit_reason, d.minutes_held, d.net_return, d.full_path, d.alts, m.tradable, m.entry_price, m.cluster, "
            "m.features FROM decisions d JOIN tokens t USING (mint) LEFT JOIN measures m ON m.mint = d.mint AND "
            "m.entry = d.entry WHERE date(t.graduated_ts / 1000, 'unixepoch') = ? ORDER BY t.graduated_ts, d.entry, "
            "d.strategy", (day,)).fetchall()
        path, buys = os.path.join(folder, day + ".csv"), 0
        with open(path + ".tmp", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(REPORT_HEAD + feats)
            for (grad, info, mint, sid, rule, e, pick, late, checks, status, reason, held, net, full, alts, tradable,
                 px, cluster, features) in rows:
                info, rule, f = json.loads(info or "{}"), json.loads(rule), json.loads(features or "{}")
                buy_ts = grad // MIN * MIN + e * MIN
                closed = status == "closed" and net is not None
                at_stop = next((p[1] for p in json.loads(full or "[]") if p[0] == rule["hold"]), None)
                alt = {a["tp"]: a["net"] for a in json.loads(alts or "[]") if a.get("done")}
                failed = "; ".join(f"{c['feature']} {c['op']} {c['value']:g} (was {c['actual']})"
                                   for c in json.loads(checks or "[]") if not c["ok"])
                buys += bool(pick)
                w.writerow([
                    utc(grad), info.get("name") or "", info.get("symbol") or "", mint, names.get(sid, sid), sid, e,
                    "bought" if pick else "passed" if tradable else "not tradable", 0 if late else 1,
                    cluster or "", utc(buy_ts) if pick else "", price(px) if pick and px else "",
                    round(px * 1e9, 2) if pick and px else "", price(px * (1 + rule["tp"])) if pick and px else "",
                    price(px * (1 - rule["sl"])) if pick and px else "",
                    utc(buy_ts + held * MIN) if pick and closed and held else "",
                    {"target": "take profit", "stop": "stop loss", "time": "time stop"}.get(reason, "") if closed else "",
                    held if closed else "", pct(net) if closed else "",
                    round(net * A.STAKE_SOL, 5) if pick and closed else "",
                    pct(at_stop) if pick else "", pct(alt.get(1.0)) if pick else "", pct(alt.get(2.0)) if pick else "",
                    failed] + [f.get(k, "") if f.get(k) is not None else "" for k in feats])
        os.replace(path + ".tmp", path)
        idx = os.path.join(folder, "index.json")
        try:
            with open(idx, encoding="utf-8") as fh:
                index = json.load(fh)
        except (OSError, ValueError):
            index = {}
        index[day] = {"rows": len(rows), "buys": buys, "bytes": os.path.getsize(path), "written": int(time.time() * 1000)}
        write_json(idx, index)


REPORT_AT = 10 * 60      # seconds after midnight UTC that yesterday's report is sent to Telegram
DAYS_SHOWN = 30          # days in the day-by-day table of the web app


def day_numbers(db, sid, day):
    """One strategy's day: decisions, buys (live and reconstructed), and how the finished buys ended."""
    rows = db.execute(
        "SELECT d.pick, d.late, d.status, d.exit_reason, d.minutes_held, d.net_return, d.full_path, d.alts, d.rule, "
        "t.info, t.graduated_ts, t.mint, d.entry, m.entry_price FROM decisions d JOIN tokens t USING (mint) "
        "LEFT JOIN measures m ON m.mint = d.mint AND m.entry = d.entry WHERE d.strategy = ? AND "
        "date(t.graduated_ts / 1000, 'unixepoch') = ? ORDER BY t.graduated_ts", (sid, day)).fetchall()
    buys = []
    for pick, late, status, reason, held, net, full, alts, rule, info, grad, mint, e, px in rows:
        if not pick:
            continue
        rule, info = json.loads(rule), json.loads(info or "{}")
        at_stop = next((p[1] for p in json.loads(full or "[]") if p[0] == rule["hold"]), None)
        alt = {a["tp"]: a["net"] for a in json.loads(alts or "[]") if a.get("done")}
        buys.append({"symbol": info.get("symbol") or mint[:6], "mint": mint, "buy_ts": grad // MIN * MIN + e * MIN,
                     "live": not late, "closed": status == "closed" and net is not None, "exit": reason, "held": held,
                     "net": net, "price": px, "held_to_stop": at_stop, "tp100": alt.get(1.0), "tp200": alt.get(2.0)})
    done = [b for b in buys if b["closed"]]
    return {"day": day, "checked": len(rows), "buys": len(buys), "live_buys": sum(b["live"] for b in buys),
            "finished": len(done), "wins": sum(b["net"] > 0 for b in done), "total": sum(b["net"] for b in done),
            "best": max((b["net"] for b in done), default=None), "worst": min((b["net"] for b in done), default=None),
            "list": buys}


def day_report(db, strategies, day):
    """(Telegram text, CSV of the shown strategies' decisions) for one UTC day."""
    shown = [s for s in strategies if s["shown"]]
    standard = db.execute("SELECT COUNT(*) FROM tokens WHERE date(graduated_ts / 1000, 'unixepoch') = ?", (day,)).fetchone()[0]
    other = db.execute("SELECT COUNT(*) FROM tokens WHERE status = 'unchecked' AND "
                       "date(first_seen_ts / 1000, 'unixepoch') = ?", (day,)).fetchone()[0]
    e, p = notify.esc, lambda v: _pct(v)
    lines = [f"📊 <b>Daily report {day}</b> (UTC)",
             f"Graduated: {standard} standard launches watched, {other} other launches not checked", ""]
    for s in shown:
        n = day_numbers(db, s["id"], day)
        lines += [f"<b>{e(s['name'])}</b>: {n['checked']} checked, {n['buys']} bought "
                  f"({n['live_buys']} live, {n['buys'] - n['live_buys']} reconstructed)"]
        if n["finished"]:
            lines.append(f"Result: {n['wins']} won, {n['finished'] - n['wins']} lost, "
                         f"{'+' if n['total'] >= 0 else '-'}{abs(n['total'] * A.STAKE_SOL):.3f} SOL "
                         f"(average {p(n['total'] / n['finished'])}; best {p(n['best'])}, worst {p(n['worst'])})")
        for b in n["list"]:
            how = {"target": "take profit", "stop": "stop loss", "time": "time stop"}.get(b["exit"], "open")
            res = f"{p(b['net'])} by {how} after {b['held']} min" if b["closed"] else "still open"
            extra = f"; held to time stop {p(b['held_to_stop'])}, +100% target {p(b['tp100'])}" if b["closed"] else ""
            lines.append(f"• {e(b['symbol'])} {utc(b['buy_ts'], '%H:%M')}{'' if b['live'] else ' (reconstructed)'}: {res}{extra}")
        everything = live_total(db, s["id"])
        lines += [f"Since recording began: {everything[0]} buys, {everything[1]} won, "
                  f"{'+' if everything[2] >= 0 else '-'}{abs(everything[2] * A.STAKE_SOL):.3f} SOL", ""]
    lines.append("Simulated trades: nothing was bought. The day's decisions are attached.")
    # the attached file: the shown strategies' rows of the day's full report
    full = os.path.join(DATA_DIR, "reports", day + ".csv")
    out = io.StringIO()
    try:
        with open(full, encoding="utf-8") as fh:
            r = csv.reader(fh)
            w = csv.writer(out)
            head = next(r)
            w.writerow(head)
            col = head.index("strategy_id")
            ids = {s["id"] for s in shown}
            for row in r:
                if row[col] in ids:
                    w.writerow(row)
    except (OSError, StopIteration, ValueError):
        pass
    return "\n".join(lines), out.getvalue().encode("utf-8")


def live_total(db, sid):
    n, wins, total = db.execute("SELECT COUNT(*), SUM(net_return > 0), SUM(net_return) FROM decisions WHERE strategy = ? "
                                "AND pick = 1 AND status = 'closed' AND net_return IS NOT NULL", (sid,)).fetchone()
    return n, wins or 0, total or 0.0


def send_daily(db, data, strategies):
    """Yesterday's report to Telegram, once, a little after midnight UTC; also kept as a file."""
    now = time.time()
    if now % 86400 < REPORT_AT:
        return
    day = utc((now - 86400) * 1000, "%Y-%m-%d")
    text, attach = day_report(db, strategies, day)
    folder = os.path.join(data, "reports")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, day + "-summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(folder, day + "-shown.csv"), "wb") as fh:
        fh.write(attach)
    if db.execute("SELECT 1 FROM daily_sent WHERE day = ?", (day,)).fetchone():
        return
    cfg = notify.load(os.path.join(data, "settings"))
    if not notify.ready(cfg):
        return
    sent, why = notify.send(cfg, text)
    if sent:
        notify.send_file(cfg, f"{day}-strategy-decisions.csv", attach, f"Decisions of {day} (UTC)")
        db.execute("INSERT OR REPLACE INTO daily_sent VALUES (?, ?)", (day, int(now * 1000)))
        db.commit()
    else:
        print(f"  ! daily Telegram report: {why}", flush=True)


def newly_listed(data, since_ms):
    """(mint, first seen, listing row) of graduates holders.py has listed since `since_ms`."""
    try:
        with closing(sqlite3.connect(os.path.join(data, "holders.db"), timeout=30)) as h:
            has_info = "info" in {r[1] for r in h.execute("PRAGMA table_info(holder_watch)")}
            return h.execute(f"SELECT mint, first_seen_ts, {'info' if has_info else 'NULL'} FROM holder_watch "
                             "WHERE first_seen_ts > ? ORDER BY first_seen_ts", (since_ms,)).fetchall()
    except sqlite3.Error:
        return []


def due(db, strategies, now_ms):
    """([(mint, info, graduated_ts)] with a decision due now, unix ms the next one falls due).
    A decision is due SETTLE seconds after its entry minute ends, for DUE_FOR seconds."""
    entries, out, nxt = sorted({s["entry"] for s in strategies}), [], now_ms + 10_000
    for mint, info, grad in db.execute("SELECT mint, info, graduated_ts FROM tokens WHERE status = 'active' "
                                       "AND graduated_ts > ?", (now_ms - (max(entries) + 5) * MIN,)).fetchall():
        grad_min = grad // MIN * MIN
        ready = [e for e in entries if 0 <= now_ms - (grad_min + e * MIN + SETTLE * 1000) < DUE_FOR * 1000]
        nxt = min([nxt] + [t for t in (grad_min + e * MIN + SETTLE * 1000 for e in entries) if t > now_ms])
        if ready:
            decided = {sid for sid, in db.execute("SELECT strategy FROM decisions WHERE mint = ?", (mint,))}
            if any(s["entry"] in ready and s["id"] not in decided for s in strategies):
                out.append((mint, info, grad))
    return out, nxt


def work_token(db, data, mint, info, grad, strategies, p_grad, last_fetch, fast=False):
    """Do whatever is due for one graduated token. Returns True when something changed."""
    now_ms = int(time.time() * 1000)
    grad_min = grad // MIN * MIN
    done_minutes = (now_ms - SETTLE * 1000 - grad_min) // MIN     # bars 0 .. done_minutes-1 are final
    decided = {s: json.loads(rule) for s, rule in db.execute(
        "SELECT strategy, rule FROM decisions WHERE mint = ?", (mint,))}
    opened = [(s, json.loads(rule), pick) for s, rule, pick in db.execute(
        "SELECT strategy, rule, pick FROM decisions WHERE mint = ? AND status = 'open'", (mint,))]
    # a buy is followed for FOLLOW minutes after it, also once it has been sold
    following = [(s, json.loads(rule)) for s, rule, n in db.execute(
        "SELECT strategy, rule, CASE WHEN alts IS NULL THEN 0 ELSE COALESCE(json_array_length(full_path), 0) END "
        "FROM decisions WHERE mint = ? AND pick = 1", (mint,)) if n < min(FOLLOW, A.T - json.loads(rule)["entry"])]
    todo = [s for s in strategies if s["id"] not in decided and s["entry"] <= done_minutes]
    # a bought token is looked at every minute; one that was passed only when its time stop is over
    wake = bool(todo) or any(
        (pick and now_ms - last_fetch.get(mint, 0) >= 60000) or done_minutes >= r["entry"] + r["hold"]
        for _, r, pick in opened) or (bool(following) and now_ms - last_fetch.get(mint, 0) >= 60000)
    if not wake:
        horizon = max([s["entry"] + s["hold"] for s in strategies] + [r["entry"] + r["hold"] for r in decided.values()])
        if not opened and not following and done_minutes > horizon:
            db.execute("UPDATE tokens SET status = 'done' WHERE mint = ?", (mint,))
            db.commit()
            return True
        return False

    # a token decided long after the fact is read as it was: the bars up to 5 hours past graduation
    bars, _ = fetch_bars(mint, info.get("pool"), grad_min + (A.T + 3) * MIN, fast=fast and bool(todo))
    if fast and todo:
        # the bar of the minute before the entry should be there; if the token traded the minute
        # before that but this bar is missing, the source may lag: read again before deciding
        need = grad_min + (min(s["entry"] for s in todo) - 1) * MIN
        for _ in range(2):
            have = {int(b["openTimeMs"]) for b in bars}
            if need in have or need - MIN not in have:
                break
            time.sleep(1)
            bars, _ = fetch_bars(mint, info.get("pool"), grad_min + (A.T + 3) * MIN, fast=True)
    last_fetch[mint] = now_ms
    if "dev_buy_sol" not in info:
        info["dev_buy_sol"] = dev_buy(mint, quick=fast)
        db.execute("UPDATE tokens SET info = ? WHERE mint = ?", (json.dumps(info), mint))
    frame = arrays(bars, grad_min, p_grad)
    t = int(time.time() * 1000)
    measured = {}
    for s in todo:
        e = s["entry"]
        if e not in measured:
            m = measure(data, mint, info, grad_min, e, frame)
            measured[e] = m
            db.execute("INSERT OR IGNORE INTO measures VALUES (?,?,?,?,?,?,?,?,?)",
                       (mint, e, t, int(m["tradable"]), int(m["traded"]), m["impact"], m["entry_price"], m["cluster"],
                        json.dumps({k: num(v[0]) for k, v in m["f"].items()})))
        m = measured[e]
        checks = check(s, m["f"])
        pick = m["tradable"] and all(c["ok"] for c in checks)
        late = int(t - (grad_min + e * MIN) > LATE * 1000)
        # a token that cannot be bought has no result to wait for
        db.execute("INSERT OR IGNORE INTO decisions (mint, strategy, rule, entry, decided_ts, late, pick, checks, status) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (mint, s["id"], json.dumps({k: s[k] for k in CORE}), e, t, late, int(pick), json.dumps(checks),
                    "open" if m["tradable"] else "closed"))
        if pick:
            print(f"BUY  {info.get('symbol')} {mint} by '{s['name']}' ({m['cluster']})", flush=True)
    if todo:
        db.commit()     # the trader reads decisions from the database: a buy is visible to it at once
    for sid, rule, pick in db.execute("SELECT strategy, rule, pick FROM decisions WHERE mint = ? AND status = 'open'",
                                      (mint,)).fetchall():
        finished, reason, held, r, path = follow(json.loads(rule), frame, done_minutes, bool(pick))
        if finished:
            db.execute("UPDATE decisions SET status = 'closed', exit_reason = ?, minutes_held = ?, net_return = ?, "
                       "path = ? WHERE mint = ? AND strategy = ?",
                       (reason, held, r, json.dumps(path) if pick else None, mint, sid))
            if pick:
                print(f"SOLD {info.get('symbol')} {mint} by {sid}: {reason}, {r}", flush=True)
        elif pick and held:
            db.execute("UPDATE decisions SET minutes_held = ?, net_return = ?, path = ? WHERE mint = ? AND strategy = ?",
                       (held, r, json.dumps(path), mint, sid))
    for sid, rule, alts in db.execute("SELECT strategy, rule, alts FROM decisions WHERE mint = ? AND pick = 1",
                                      (mint,)).fetchall():
        rule = json.loads(rule)
        if np.isfinite(frame[0]["close"][0, rule["entry"] - 1]):
            db.execute("UPDATE decisions SET alts = ? WHERE mint = ? AND strategy = ?",
                       (json.dumps(alternatives(rule, frame, done_minutes, json.loads(alts) if alts else None)), mint, sid))
        full = held_path(rule, frame, done_minutes)
        # bars are only served for the last 5 hours: a buy older than that cannot be followed any more
        gone = done_minutes >= A.T or not np.isfinite(frame[0]["close"][0, rule["entry"] - 1])
        if gone and len(full) < min(FOLLOW, A.T - rule["entry"]):
            full += [[len(full) + k + 1, None] for k in range(min(FOLLOW, A.T - rule["entry"]) - len(full))]
        db.execute("UPDATE decisions SET full_path = ? WHERE mint = ? AND strategy = ? AND "
                   "COALESCE(json_array_length(full_path), 0) <= ?", (json.dumps(full), mint, sid, len(full)))
    db.execute("UPDATE tokens SET tries = 0 WHERE mint = ?", (mint,))
    db.commit()
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--lookback", type=float, default=LOOKBACK / 3600,
                    help="on start, also take graduates first listed within this many hours")
    args = ap.parse_args()
    data = args.data
    db = sqlite3.connect(os.path.join(data, "watch.db"), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    for col in ("full_path TEXT", "alts TEXT", "notified INTEGER", "notified_exit INTEGER"):
        if col.split()[0] not in {r[1] for r in db.execute("PRAGMA table_info(decisions)")}:
            db.execute(f"ALTER TABLE decisions ADD COLUMN {col}")
    # buys made before prices were kept after the sale: follow them again while their bars are still served
    db.execute("UPDATE tokens SET status = 'active' WHERE status = 'done' AND graduated_ts > ? AND mint IN "
               "(SELECT mint FROM decisions WHERE pick = 1 AND (full_path IS NULL OR alts IS NULL))",
               (int((time.time() - 4 * 3600) * 1000),))
    db.commit()
    strategies, p_grad, clusters, next_rules, next_publish = None, 4.76e-5, {}, 0.0, 0.0
    next_reports = next_all_reports = time.time() + 120    # after the first rounds of work
    next_daily = time.time() + 180
    global DATA_DIR
    DATA_DIR = data
    retry_at, last_fetch = {}, {}   # mint -> when to try again after a failure / when its bars were last read
    seen_to = int((time.time() - args.lookback * 3600) * 1000)
    dirty = False
    # decisions are made in their own thread with their own connection, the moment they fall due,
    # so a slow read for another token (or a refused request) never holds up a buy
    shared = {"strategies": None, "p_grad": p_grad}
    locks, locks_lock, decider_retry = {}, threading.Lock(), {}

    def lock_of(mint):
        with locks_lock:
            return locks.setdefault(mint, threading.Lock())

    def decider():
        ddb = sqlite3.connect(os.path.join(data, "watch.db"), timeout=30)
        while True:
            nxt = time.time() + 1
            try:
                if shared["strategies"]:
                    got, due_ms = due(ddb, shared["strategies"], int(time.time() * 1000))
                    nxt = min(nxt, due_ms / 1000)
                    for mint, info, grad in got:
                        if decider_retry.get(mint, 0) > time.time():
                            continue
                        lk = lock_of(mint)
                        if not lk.acquire(timeout=2):   # the main loop is on it: the next due token first
                            decider_retry[mint] = time.time() + 1
                            continue
                        try:
                            work_token(ddb, data, mint, json.loads(info), grad, shared["strategies"],
                                       shared["p_grad"], last_fetch, fast=True)
                        except Exception as e:
                            ddb.rollback()
                            decider_retry[mint] = time.time() + 3
                            print(f"  ! decision {mint}: {e!r}", flush=True)
                        finally:
                            lk.release()
            except Exception as e:   # the thread must keep running whatever happens
                print(f"  ! decider: {e!r}", flush=True)
            time.sleep(min(1.0, max(0.05, nxt - time.time())))

    threading.Thread(target=decider, daemon=True).start()

    print("watching new graduates", flush=True)
    while True:
        now = time.time()
        if now >= next_rules:
            next_rules = now + 60
            got = load_strategies(data)
            if got and json.dumps(got, sort_keys=True) != json.dumps(strategies, sort_keys=True):
                if [s["id"] for s in got] != [s["id"] for s in strategies or []]:
                    print(f"strategies: {len(got)} ({', '.join(s['id'] for s in got)})", flush=True)
                if {s["id"] for s in got if s["shown"]} != {s["id"] for s in strategies or [] if s["shown"]}:
                    print(f"earlier tokens to check for the shown strategies: {reopen(db, got)}", flush=True)
                dirty = True
            strategies = got or strategies
            p_grad, clusters = report_extras(data)
            shared["strategies"], shared["p_grad"] = strategies, p_grad
        if not strategies:
            time.sleep(30)
            continue

        for mint, seen, listed in newly_listed(data, seen_to):
            seen_to = max(seen_to, seen)
            try:
                info = coin_info(data, mint, listed)
            except Exception as e:   # looked up again when its graduation time is
                print(f"  ! {mint}: {e!r}", flush=True)
                info = None
            cur = db.execute("INSERT OR IGNORE INTO tokens (mint, first_seen_ts, status, info) VALUES (?,?,?,?)",
                             (mint, seen, "new", json.dumps(info) if info else None))
            dirty |= cur.rowcount > 0
        db.commit()

        # 1. find out what kind of launch each new token was and when it graduated
        for mint, info, tries in db.execute("SELECT mint, info, tries FROM tokens WHERE status = 'new'").fetchall():
            if retry_at.get(mint, 0) > now:
                continue
            retry_at[mint] = now + 60
            grad = src = None
            try:
                info = json.loads(info) if info else coin_info(data, mint, None)
                if info and info.get("classic") is None:
                    # listed before the collector knew the coin; once it does it can say what launch it was
                    found = coin_info(data, mint, None)
                    info["classic"] = found.get("classic") if found else None
                # the backtest only covers standard 85 SOL curves: its pool-depth model does not fit others
                if info and info.get("classic") != 0:
                    bars, slot = fetch_bars(mint, info.get("pool"))
                    grad, src = graduation(bars, slot, info.get("created_ts"), p_grad)
            except Exception as e:   # one odd answer must not stop the watch
                print(f"  ! {mint}: {e!r}", flush=True)
                info, grad = (info if isinstance(info, dict) else None), None
            if info and info.get("classic") == 0:
                db.execute("UPDATE tokens SET status = 'unchecked', info = ? WHERE mint = ?", (json.dumps(info), mint))
            elif grad:
                retry_at.pop(mint, None)
                if "dev_buy_sol" not in info:
                    try:   # looked up now, so the first decision does not wait for it (else it is tried then)
                        info["dev_buy_sol"] = dev_buy(mint)
                    except Exception:
                        pass
                db.execute("UPDATE tokens SET status = 'active', tries = 0, graduated_ts = ?, graduated_src = ?, "
                           "info = ? WHERE mint = ?", (grad, src, json.dumps(info), mint))
            else:
                db.execute("UPDATE tokens SET tries = ?, status = ?, info = COALESCE(?, info) WHERE mint = ?",
                           (tries + 1, "failed" if tries + 1 >= MAX_TRIES else "new",
                            json.dumps(info) if info else None, mint))
            db.commit()
            dirty = True

        # 2. decide for every strategy whose entry minute has come, and follow what is open
        #    The newest tokens go first: their decisions are due now. Tokens being caught up
        #    from history (see reopen) wait their turn, a few each round, so they never delay those.
        caught_up = 0
        soon = {m for m, _, _ in due(db, strategies, int(time.time() * 1000) + 30000)[0]}
        for mint, info, grad in db.execute(
                "SELECT mint, info, graduated_ts FROM tokens WHERE status = 'active' ORDER BY graduated_ts DESC").fetchall():
            if retry_at.get(mint, 0) > time.time():
                continue
            old = time.time() * 1000 - grad > CATCH_UP_AFTER * 1000
            if old and caught_up >= CATCH_UP_PER_ROUND:
                continue
            if mint in soon:
                continue        # its decision is due within 30 s: the decision thread will read it
            lk = lock_of(mint)
            if not lk.acquire(blocking=False):
                continue        # the decision thread is on it
            try:
                worked = work_token(db, data, mint, json.loads(info), grad, strategies, p_grad, last_fetch)
                caught_up += old and worked
                dirty |= worked
            except Exception as e:
                db.rollback()
                retry_at[mint] = time.time() + 60
                setback(db, mint, repr(e))
                dirty = True
            finally:
                lk.release()

        try:
            send_alerts(db, data, strategies)
        except Exception as e:   # an alert that cannot be built must not stop the watch
            print(f"  ! alerts: {e!r}", flush=True)
        if now >= next_reports:     # the last days often, every day on record a few times a day
            everything = now >= next_all_reports
            next_reports, next_all_reports = now + 600, (now + 6 * 3600 if everything else next_all_reports)
            try:
                write_reports(db, data, strategies,
                              None if everything else [utc((now - k * 86400) * 1000, "%Y-%m-%d") for k in (2, 1, 0)])
            except Exception as e:
                print(f"  ! daily reports: {e!r}", flush=True)
        if now >= next_daily:
            next_daily = now + 300
            try:
                send_daily(db, data, strategies)
            except Exception as e:
                print(f"  ! daily report: {e!r}", flush=True)
        if (dirty and now >= next_publish) or not os.path.exists(os.path.join(data, "watch.json")):
            next_publish, dirty = now + 15, False
            try:
                publish(db, data, strategies, clusters, p_grad)
            except OSError as e:
                print(f"  ! watch.json not written: {e!r}", flush=True)
        for m in [m for m, t in list(last_fetch.items()) if now * 1000 - t > 6 * 3600 * 1000]:
            last_fetch.pop(m, None)
            retry_at.pop(m, None)
            decider_retry.pop(m, None)
        time.sleep(10)          # decisions do not wait for this: they have their own thread


if __name__ == "__main__":
    main()

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

Writes watch.db (its own database, like holders.db) and watch.json for the web app. It also saves every graduate's
complete 1-minute bars into pump.db once they exist (archive_loop), so the backtest grows with the live record.

Usage: python3 watch.py [--data data]
"""
import argparse
import csv
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

import numpy as np

import analyze as A
import collector as C
import first_trades
import rules as rulebook

MIN = 60000
LOOKBACK = 3 * 3600      # on start, also take graduates first seen this recently
SETTLE = 1               # seconds after a minute ends before its bar is read (measured 19 Oct 2026: final from +0.1 s;
                         # a missing bar is still read again before deciding)
DUE_FOR = 180            # a decision this many seconds past its minute is no longer hurried (catch-up handles it)
DECIDERS = 4             # tokens decided at the same time
LATE = 180               # a decision made this many seconds after its minute is marked late
MAX_TRIES = 30           # failed lookups (one a minute) before a token is given up
LISTED = 7 * 86400       # the web app lists the tokens of this many days; the database keeps every one
FOLLOW = 120             # minutes a bought token's price is kept after the buy, whatever the exit was
ALT_TPS = (1.0, 2.0)     # every buy is also run with these take profits, to show what aiming higher would have done
PASSED_SHOWN = 100       # passed tokens listed on a strategy page (newest first)
ARCHIVE_AFTER = (A.T + 3) * MIN   # a graduate's whole window of bars (what the backtest reads) exists this long after it graduated
ARCHIVE_EVERY = 3.0      # seconds between archive requests: the decisions keep their share of pump.fun's limits
ARCHIVE_TRIES = 3        # answers without any bars before a graduate is left out (it has no history to keep)
_range_eval = {}      # (mint, strategy id) -> the last minute a rule with a range of entry minutes (entry_to) was evaluated at
CORE = ("entry", "conditions", "tp", "sl", "hold", "be", "be_stop")      # "be": breakeven trigger, 0 = none; "be_stop": where the stop moves to (rules.py)

SCHEMA = """
DROP TABLE IF EXISTS watch;          -- the first version kept one rule's decision per token
CREATE TABLE IF NOT EXISTS tokens (
  mint TEXT PRIMARY KEY,
  first_seen_ts INTEGER NOT NULL,   -- unix ms holders.py first listed it as graduated
  status TEXT NOT NULL,             -- new, active, done, unchecked (not a standard launch), failed
  tries INTEGER NOT NULL DEFAULT 0,
  info TEXT,                        -- JSON: name, symbol, image, links, created_ts, pool, classic, dev_buy_sol
  graduated_ts INTEGER, graduated_src TEXT,
  archived INTEGER                  -- 1: its bars are saved in pump.db (the backtest's data); -1: it had none to save
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
CREATE INDEX IF NOT EXISTS decisions_decided ON decisions(decided_ts);
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


def snapshot(s, e):
    """The rule as stored with a decision made at entry minute e. A rule with a range of entry minutes keeps the minute it bought at as
    "entry" (follow() and the rest read it there) and its own range as entry_from / entry_to."""
    snap = {k: s[k] for k in CORE}
    if s.get("entry_to"):
        snap.update(entry=e, entry_from=s["entry"], entry_to=s["entry_to"])
    return snap


def entry_minutes(s):
    """Every entry minute a strategy may decide at."""
    return range(s["entry"], s.get("entry_to", s["entry"]) + 1)


def load_strategies(data):
    """The automatic search's rule and every enabled saved rule, each with its backtest record."""
    out = []
    try:
        with closing(read_only(os.path.join(data, "pump.db"))) as db:
            saved = [r for r in rulebook.load(os.path.join(data, "settings")) if r["enabled"]]
            chosen = [r for r in saved if r["live"]]
            # the newest search run's pick, only when it passed its test: an older pass is not kept, since among
            # millions of rule sets an occasional lucky pass is expected and must not stay on for good
            row = db.execute("SELECT run_id, picked_rule FROM backtest_runs WHERE kind = 'search' "
                             "ORDER BY run_id DESC LIMIT 1").fetchone()
            if row and (not row[1] or db.execute("SELECT verdict FROM backtest_runs WHERE run_id = ?",
                                                 (row[0],)).fetchone()[0] != "pass"):
                row = None
            # every strategy is recorded; the web app shows the rules marked "record live",
            # or all of them when none is marked
            if row:
                r = json.loads(row[1])
                out.append({"id": "auto", "name": "Automatic search (best rule found)", "run_id": row[0],
                            "shown": not chosen, **{k: r.get(k, 0.0) for k in CORE}})
            for r in rulebook.with_extras(saved, rulebook.removed(os.path.join(data, "settings"))):
                if r["enabled"]:
                    hit = db.execute("SELECT MAX(run_id) FROM backtest_runs WHERE kind = 'rule' AND rule_id = ? "
                                     "AND picked_rule IS NOT NULL", (r["id"],)).fetchone()
                    out.append({"id": r["id"], "name": r["name"], "run_id": hit[0],
                                "shown": r["live"] or not chosen, **{k: r.get(k, 0.0) for k in CORE},
                                **({"entry_to": r["entry_to"]} if r.get("entry_to") else {})})
            for s in out:
                s["conditions"] = labelled(s["conditions"])
                n, mean, wins = db.execute("SELECT COUNT(*), AVG(net_return), SUM(net_return > 0) FROM backtest_trades "
                                           "WHERE run_id = ?", (s["run_id"],)).fetchone() if s["run_id"] else (0, None, 0)
                te = db.execute("SELECT COUNT(*), AVG(net_return) FROM backtest_trades WHERE run_id = ? "
                                "AND split = 'test'", (s["run_id"],)).fetchone() if s["run_id"] else (0, None)
                s["backtest"] = {"trades": n, "mean": mean, "wins": wins or 0, "recent_trades": te[0], "recent_mean": te[1], "stake_sol": A.STAKE_SOL}
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


def check(strategy, f, at):
    """Each condition against the token's readings: at the entry minute (f), or at the condition's own earlier
    minute ("at": at(minute) gives the readings there)."""
    return [{"feature": c["feature"], "op": c["op"], "value": c["value"], **({"at": c["at"]} if c.get("at") else {}),
             "actual": num((at(c["at"]) if c.get("at") else f)[c["feature"]][0], 9),
             "ok": bool(A.matches(f, [c], at)[0])} for c in strategy["conditions"]]


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
    by_hold, reason, exit_min = A.simulate(arr, e, rule["tp"], rule["sl"], [held], be=rule.get("be", 0.0), be_stop=rule.get("be_stop", 0.0))
    r = float(A.net_return(arr, trd, e, by_hold[held])[0])
    k = int(reason[0])
    finished = k in (1, 2) or held >= full
    last = int(exit_min[0])
    path = []
    if want_path:
        # each minute held: [minute, net return if sold then, price ÷ entry price at its close, its high ÷ entry price]
        p0 = float(arr["close"][0, e - 1])
        for m in range(e, last + 1):
            x = float(arr["close"][0, m]) / p0
            path.append([m - e + 1, num(A.net_return(arr, trd, e, np.array([x]))[0], 4), num(x, 4),
                         num(float(arr["high"][0, m]) / p0, 4)])
        if finished and path and np.isfinite(r):
            path[-1][1] = num(r, 4)   # the last point is what the sale really fetched
            path[-1][2] = num(float(by_hold[held][0]), 4)   # and the price it sold at (the stop or the target)
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


def write_strategy_live(db, data, strategies, p_grad):
    """strategies/<id>.live.json for every rule strategy (the Lab page): every token the watch decided on for it,
    bought or passed, and which condition kept the passed ones out. Written whether or not the strategy is shown
    on the Record live tab. A rule that no longer exists leaves no record file behind."""
    folder = os.path.join(data, "strategies")
    os.makedirs(folder, exist_ok=True)
    known = {r["id"] for r in rulebook.with_extras(rulebook.load(os.path.join(data, "settings")),
                                                   rulebook.removed(os.path.join(data, "settings")))}
    for f in os.listdir(folder):
        if f.endswith(".live.json") and f[:-10] not in known:
            os.remove(os.path.join(folder, f))
    for s in strategies:
        if s["id"] == "auto":     # the automatic search's pick is not a rule of the Lab
            continue
        # only decisions made with the strategy as it is defined now: an edited strategy starts a new record
        core = lambda r: (r.get("entry_from", r["entry"]), r.get("entry_to") or 0, r["tp"], r["sl"], r["hold"], r.get("be", 0.0), r.get("be_stop", 0.0),   # noqa: E731
                          [rulebook.cond_core(c) for c in r["conditions"]])
        rows, earlier, first_seen, old, ent = [], 0, None, {}, {}      # ent: the entry minute each decision was made at (a rule with entry_to buys at a different one per token)
        for row in db.execute(
                "SELECT d.mint, json_extract(t.info, '$.symbol'), t.graduated_ts, d.decided_ts, d.late, d.pick, d.checks, "
                "d.status, d.exit_reason, d.minutes_held, d.net_return, m.tradable, m.entry_price, d.path, d.full_path, d.alts, "
                "d.rule, d.entry FROM decisions d "
                "JOIN tokens t USING (mint) LEFT JOIN measures m ON m.mint = d.mint AND m.entry = d.entry "
                "WHERE d.strategy = ? ORDER BY d.decided_ts DESC", (s["id"],)):
            ent[row[0]] = row[17]
            if core(json.loads(row[16])) == core(s):
                rows.append(row[:16])
                first_seen = row[3] if first_seen is None or (row[3] and row[3] < first_seen) else first_seen
            else:
                earlier += 1
                if row[5]:      # what an earlier version bought, kept as its own list (newest version first, see below)
                    old.setdefault(json.dumps(core(json.loads(row[16]))), (json.loads(row[16]), []))[1].append(row[:16])
        # only tokens whose entry minute came after this version of the strategy was first seen: a strategy that is
        # added or edited also decides on tokens it missed (reconstructed from their saved bars), but those were
        # never live signals and are already in its backtest
        before = 0
        if first_seen:
            kept_rows = [r for r in rows if r[2] is None or r[2] // MIN * MIN + ent.get(r[0], s["entry"]) * MIN >= first_seen - LATE * 1000]
            before, rows = len(rows) - len(kept_rows), kept_rows
        columns = rulebook.page_columns(s["conditions"])
        bought, fails, untradable, passed_rows = [], {}, 0, []
        top = max(ALT_TPS)

        def bought_row(r, tp=s["tp"]):
            """One bought token's row for the page (r: the decision's columns, see the query above)."""
            mint, symbol, grad, dt, late, pick, checks, status, reason, held, net, tradable, px, path, full, alts = r
            checks = json.loads(checks) if checks else []
            actual = {rulebook.cond_key(c): c.get("actual") for c in checks}
            # the best moment while held, the same buy simply held for FOLLOW minutes, and aimed at the top ALT_TPS target
            pts = json.loads(path) if path else []
            nets = [p[1] for p in pts if p[1] is not None]
            # "Now / exit": the price as a multiple of the entry now (open) or at the sale (closed), and its peak;
            # decisions made before the path held prices have none
            xs = [p for p in pts if len(p) >= 4 and p[2] is not None]
            x_now = xs[-1][2] if xs else None
            x_peak = max([p[3] for p in xs if p[3] is not None] + ([x_now] if x_now is not None else []), default=None)
            kept = [v for _, v in (json.loads(full) if full else []) if v is not None]
            alt = next((a for a in (json.loads(alts) if alts else []) if a.get("tp") == top), None)
            return [mint, symbol, grad, dt, bool(late), status, reason, held, net, px,
                           [actual.get(k) for k in columns], max(nets) if nets else None,
                           kept[-1] if kept else None, max(kept) if kept else None,
                           {"net": alt.get("net"), "exit": alt.get("exit"), "held": alt.get("held"), "done": alt.get("done")}
                           if alt and top > tp else None, x_now, x_peak, ent.get(mint, s["entry"])]

        for r in rows:
            mint, symbol, grad, dt, late, pick, checks, status, reason, held, net, tradable, px, path, full, alts = r
            checks = json.loads(checks) if checks else []
            if pick:
                bought.append(bought_row(r))
            elif tradable == 0:
                untradable += 1
            else:
                for c in checks:
                    if not c.get("ok"):
                        f = fails.setdefault(rulebook.cond_key(c), {"failed": 0, "no_reading": 0})
                        f["failed" if c.get("actual") is not None else "no_reading"] += 1
                if len(passed_rows) < PASSED_SHOWN:   # rows come newest first
                    passed_rows.append([mint, symbol, grad, dt, bool(late), status, net,
                                        [[rulebook.cond_key(c), c.get("actual")] for c in checks if not c.get("ok")], ent.get(mint, s["entry"])])
        passed = [r for r in rows if not r[5] and r[7] == "closed" and r[10] is not None]
        write_json(os.path.join(folder, s["id"] + ".live.json"), {
            "generated_at": int(time.time() * 1000), "id": s["id"], "name": s["name"],
            "rule": {**{k: s[k] for k in ("entry", "tp", "sl", "hold", "be", "be_stop", "conditions")}, **({"entry_to": s["entry_to"]} if s.get("entry_to") else {})},
            "stake_usd": A.stake_usd(p_grad), "stake_sol": A.STAKE_SOL, "sol_usd": A.sol_usd(p_grad),
            "since_ts": first_seen, "decided": len(rows),
            "earlier_rule": earlier,   # decisions made with an earlier definition of the strategy, left out
            "before_rule": before,     # tokens whose entry minute was over before this version existed, left out
            "untradable": untradable, "fails": fails,
            # what the passed tokens would have returned had they been bought all the same
            "passed_closed": len(passed), "passed_mean": sum(r[10] for r in passed) / len(passed) if passed else None,
            "bought_columns": ["mint", "symbol", "graduated_ts", "decided_ts", "late", "status", "exit", "minutes_held",
                               "net_return", "entry_price", "measured", "best", "held_end", "held_best", "alt", "x", "peak", "entry"],
            "measured_columns": columns,   # the features in each bought row's "measured", in order
            "follow_minutes": FOLLOW, "alt_tp": top if top > s["tp"] else None,
            "bought": bought,
            # what earlier versions of the strategy bought (newest version first, at most 5): their own rule, and rows like "bought"
            "earlier_versions": [{"rule": {**{k: v.get("entry_from", v.get("entry", 0.0)) if k == "entry" else v.get(k, [] if k == "conditions" else 0.0) for k in CORE}, "entry_to": v.get("entry_to") or 0}, "bought": [bought_row(r, v["tp"]) for r in rs]}
                                 for v, rs in sorted(old.values(), key=lambda t: -max(r[3] or 0 for r in t[1]))[:5]],
            # the newest passed tokens that could be bought: which conditions failed (with the reading), and their result
            "passed_columns": ["mint", "symbol", "graduated_ts", "decided_ts", "late", "status", "net_return", "failed", "entry"],
            "passed": passed_rows})


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
    tradable = [{"id": s["id"], "name": s["name"], "shown": s["shown"], "entry": s["entry"], **({"entry_to": s["entry_to"]} if s.get("entry_to") else {}), "tp": s["tp"], "sl": s["sl"],
                 "be": s.get("be", 0.0), "be_stop": s.get("be_stop", 0.0), "hold": s["hold"], "backtest": s.get("backtest")} for s in strategies]
    write_strategy_live(db, data, strategies, p_grad)
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
                failed = "; ".join(f"{rulebook.cond_key(c)} {c['op']} {c['value']:g} (was {c['actual']})"
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
    entries, out, nxt = sorted({e for s in strategies for e in entry_minutes(s)}), [], now_ms + 10_000
    for mint, info, grad in db.execute("SELECT mint, info, graduated_ts FROM tokens WHERE status = 'active' "
                                       "AND graduated_ts > ?", (now_ms - (max(entries) + 5) * MIN,)).fetchall():
        grad_min = grad // MIN * MIN
        ready = [e for e in entries if 0 <= now_ms - (grad_min + e * MIN + SETTLE * 1000) < DUE_FOR * 1000]
        nxt = min([nxt] + [t for t in (grad_min + e * MIN + SETTLE * 1000 for e in entries) if t > now_ms])
        if ready:
            decided = {sid for sid, in db.execute("SELECT strategy FROM decisions WHERE mint = ?", (mint,))}
            if any(any(e in ready for e in entry_minutes(s)) and s["id"] not in decided for s in strategies):
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
    # a rule with a range of entry minutes is looked at again each time a new minute has closed inside its range, until it buys or the range is over
    todo = [s for s in strategies if s["id"] not in decided and s["entry"] <= done_minutes
            and (not s.get("entry_to") or _range_eval.get((mint, s["id"]), -1) < min(done_minutes, s["entry_to"]))]
    # a bought token is looked at every minute; one that was passed only when its time stop is over
    wake = bool(todo) or any(
        (pick and now_ms - last_fetch.get(mint, 0) >= 60000) or done_minutes >= r["entry"] + r["hold"]
        for _, r, pick in opened) or (bool(following) and now_ms - last_fetch.get(mint, 0) >= 60000)
    if not wake:
        horizon = max([s.get("entry_to", s["entry"]) + s["hold"] for s in strategies] + [r.get("entry_to", r["entry"]) + r["hold"] for r in decided.values()])
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
        need = grad_min + (min(max(s["entry"], _range_eval.get((mint, s["id"]), s["entry"] - 1) + 1) for s in todo) - 1) * MIN
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
    measured, at_minute = {}, {}

    def earlier(e2):    # the readings at an earlier minute, for conditions measured there
        if e2 not in at_minute:
            at_minute[e2] = A.features(token_frame(data, mint, info, grad_min), *frame, e2)
        return at_minute[e2]
    for s in todo:
        last_e = s.get("entry_to", s["entry"])
        # the minutes not looked at yet (a rule without a range has one), up to the last closed one: the first at which the token passes is the buy
        start = max(s["entry"], _range_eval.get((mint, s["id"]), s["entry"] - 1) + 1) if s.get("entry_to") else s["entry"]
        upto = min(done_minutes, last_e)
        chosen = None
        for e in range(start, upto + 1):
            if e not in measured:
                m = measure(data, mint, info, grad_min, e, frame)
                measured[e] = m
                db.execute("INSERT OR IGNORE INTO measures VALUES (?,?,?,?,?,?,?,?,?)",
                           (mint, e, t, int(m["tradable"]), int(m["traded"]), m["impact"], m["entry_price"], m["cluster"],
                            json.dumps({k: num(v[0]) for k, v in m["f"].items()})))
            m = measured[e]
            checks = check(s, m["f"], earlier)
            pick = m["tradable"] and all(c["ok"] for c in checks)
            chosen = (e, m, checks, pick)
            if pick:
                break
        if s.get("entry_to"):
            _range_eval[(mint, s["id"])] = upto
        if chosen is None or not (chosen[3] or chosen[0] >= last_e):
            continue            # nothing bought yet and the range is not over: decide again when the next minute has closed
        e, m, checks, pick = chosen
        late = int(t - (grad_min + e * MIN) > LATE * 1000)
        # a token that cannot be bought has no result to wait for
        db.execute("INSERT OR IGNORE INTO decisions (mint, strategy, rule, entry, decided_ts, late, pick, checks, status) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (mint, s["id"], json.dumps(snapshot(s, e)), e, t, late, int(pick), json.dumps(checks),
                    "open" if m["tradable"] else "closed"))
        _range_eval.pop((mint, s["id"]), None)
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


# ---- the bars of every graduate, kept for the backtest ----
def archive_token(pdb, mint, info, grad, src, bars):
    """Saves one graduate's bars in pump.db, where analyze.py reads them, the way the collector would have: the coin
    (a standard launch only when known to be one) with its bars done, its candles, and the creator's launch buy. A coin
    the collector already holds keeps its own graduation time. Returns the number of bars."""
    rows = [(mint, int(b["openTimeMs"]), float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]),
             float(b["volumeUsd"]), b["trades"]) for b in bars]
    raw = {k: info.get(k) for k in ("name", "symbol", "image_uri", "twitter", "telegram", "website", "creator")}
    raw.update(mint=mint, created_timestamp=info.get("created_ts"), pump_swap_pool=info.get("pool"), source="watch")
    now = int(time.time() * 1000)
    pdb.execute(
        "INSERT INTO tokens (mint, name, symbol, creator, pump_swap_pool, created_ts, graduated_ts, graduated_src, classic, "
        "first_bar_ts, bars_to_ts, candles_done, first_seen_ts, updated_ts, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?) "
        "ON CONFLICT(mint) DO UPDATE SET graduated_ts = COALESCE(graduated_ts, excluded.graduated_ts), "
        "graduated_src = COALESCE(graduated_src, excluded.graduated_src), classic = COALESCE(classic, excluded.classic), "
        "first_bar_ts = COALESCE(first_bar_ts, excluded.first_bar_ts), bars_to_ts = excluded.bars_to_ts, "
        "candles_done = 1, updated_ts = excluded.updated_ts",
        (mint, info.get("name"), info.get("symbol"), info.get("creator"), info.get("pool"), info.get("created_ts"), grad, src,
         # a standard launch only when known to be one: the backtest's pool-depth model fits no other
         1 if info.get("classic") == 1 else None, rows[0][1] if rows else None, grad // MIN * MIN + ARCHIVE_AFTER, now, now, json.dumps(raw, ensure_ascii=False)))
    pdb.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
    if info.get("dev_buy_sol") is not None:   # the rules that look at the creator's launch buy need it
        pdb.execute("INSERT OR IGNORE INTO first_trades (mint, ts, type, user, is_dev_buy, amount_sol) VALUES (?,?,?,?,?,?)",
                    (mint, None, "buy", info.get("creator"), int(info["dev_buy_sol"] > 0), info["dev_buy_sol"]))
    pdb.commit()
    return len(rows)


def archive_once(data, fails, backoff):
    """Archives the newest graduate that is old enough and not yet saved. Returns "idle" (nothing to do),
    "done" or "failed". Its own connections: it runs in its own thread."""
    now = time.time() * 1000
    with closing(sqlite3.connect(os.path.join(data, "watch.db"), timeout=30)) as wdb:
        todo = [r for r in wdb.execute(
            "SELECT mint, info, graduated_ts, graduated_src FROM tokens WHERE status IN ('active', 'done') AND archived IS NULL "
            "AND graduated_ts <= ? ORDER BY graduated_ts DESC LIMIT 200", (now - ARCHIVE_AFTER,)) if backoff.get(r[0], 0) <= now]
        if not todo:
            return "idle"
        mint, info, grad, src = todo[0]
        mark = lambda v: (wdb.execute("UPDATE tokens SET archived = ? WHERE mint = ?", (v, mint)), wdb.commit())   # noqa: E731
        try:
            info = json.loads(info)
            with closing(sqlite3.connect(os.path.join(data, "pump.db"), timeout=60)) as pdb:
                pdb.executescript(C.SCHEMA + first_trades.SCHEMA)
                held = pdb.execute("SELECT candles_done FROM tokens WHERE mint = ?", (mint,)).fetchone()
                if not (held and held[0]):      # the collector may already hold it complete
                    bars, _ = fetch_bars(mint, info.get("pool"), grad // MIN * MIN + ARCHIVE_AFTER)
                    archive_token(pdb, mint, info, grad, src, bars)
            mark(1)
            return "done"
        except Exception as e:
            if "no bars" in str(e):
                fails[mint] = fails.get(mint, 0) + 1
                if fails[mint] >= ARCHIVE_TRIES:
                    mark(-1)
            backoff[mint] = now + 600_000
            print(f"  ! archive {mint}: {e!r}", flush=True)
            return "429" if "429" in str(e) else "failed"


def archive_loop(data):
    """Forever, slowly: one graduate every few seconds, newest first (the ones the strategies are trading). Everything a
    graduate needs to be in the backtest is here, so it grows without the collector, which pump.fun throttles."""
    fails, backoff, saved = {}, {}, 0
    while True:
        try:
            r = archive_once(data, fails, backoff)
        except Exception as e:   # the thread must keep running whatever happens
            print(f"  ! archive: {e!r}", flush=True)
            r = "failed"
        if r == "done":
            saved += 1
            if saved % 25 == 0:
                print(f"archived the bars of {saved} graduates since the start", flush=True)
        time.sleep(60 if r in ("idle", "429") else ARCHIVE_EVERY)


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
    if "archived" not in {r[1] for r in db.execute("PRAGMA table_info(tokens)")}:
        db.execute("ALTER TABLE tokens ADD COLUMN archived INTEGER")
    # buys made before prices were kept after the sale: follow them again while their bars are still served
    db.execute("UPDATE tokens SET status = 'active' WHERE status = 'done' AND graduated_ts > ? AND mint IN "
               "(SELECT mint FROM decisions WHERE pick = 1 AND (full_path IS NULL OR alts IS NULL))",
               (int((time.time() - 4 * 3600) * 1000),))
    db.commit()
    strategies, p_grad, clusters, next_rules, next_publish = None, 4.76e-5, {}, 0.0, 0.0
    next_reports = next_all_reports = time.time() + 120    # after the first rounds of work
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

    # tokens due in the same minute are decided side by side (each in its own thread with its own connection),
    # so one token's slow read never delays another's buy
    workers, busy_mints, busy_lock = ThreadPoolExecutor(max_workers=DECIDERS), set(), threading.Lock()
    local = threading.local()

    def decide_one(mint, info, grad):
        lk = lock_of(mint)
        try:
            if not hasattr(local, "db"):
                local.db = sqlite3.connect(os.path.join(data, "watch.db"), timeout=30)
            if not lk.acquire(timeout=2):   # the main loop is on it: tried again in a moment
                decider_retry[mint] = time.time() + 1
                return
            try:
                work_token(local.db, data, mint, json.loads(info), grad, shared["strategies"],
                           shared["p_grad"], last_fetch, fast=True)
            except Exception as e:
                local.db.rollback()
                decider_retry[mint] = time.time() + 3
                print(f"  ! decision {mint}: {e!r}", flush=True)
            finally:
                lk.release()
        finally:
            with busy_lock:
                busy_mints.discard(mint)

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
                        with busy_lock:
                            if mint in busy_mints:
                                continue
                            busy_mints.add(mint)
                        workers.submit(decide_one, mint, info, grad)
            except Exception as e:   # the thread must keep running whatever happens
                print(f"  ! decider: {e!r}", flush=True)
            time.sleep(min(0.25, max(0.05, nxt - time.time())))

    threading.Thread(target=decider, daemon=True).start()
    threading.Thread(target=archive_loop, args=(data,), daemon=True).start()

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

        if now >= next_reports:     # the last days often, every day on record a few times a day
            everything = now >= next_all_reports
            next_reports, next_all_reports = now + 600, (now + 6 * 3600 if everything else next_all_reports)
            try:
                write_reports(db, data, strategies,
                              None if everything else [utc((now - k * 86400) * 1000, "%Y-%m-%d") for k in (2, 1, 0)])
            except Exception as e:
                print(f"  ! daily reports: {e!r}", flush=True)
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

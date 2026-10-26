"""The Recorder page (owner only), laid out like Rug Radar's Recorder: what the watch is recording now, the coins
recorded on each UTC day, and one coin's price with what every strategy did on it.

Everything is read from watch.db (tokens, measures, decisions: written by watch.py) and pump.db (the 1-minute bars
watch.py's archive loop saves once a coin is old enough), both opened read-only: this runs in the web service,
which has little memory and must never hold a write lock. Queries start from the day's tokens (their index on
graduated_ts) and look decisions up by key, so a page never scans a table."""
import calendar
import json
import os
import sqlite3
import time
from contextlib import closing

import solana

MIN = 60_000
HORIZON_MIN = 120           # minutes of bars kept after graduation (analyze.T - 1)
SUPPLY = 1e9                # pump.fun tokens: market cap = price x supply
ACTIVE_LIMIT = 60           # coins listed under "Recording now"
DAYS_SHOWN = 30
_day_cache = {}             # day -> (computed at, coins): finished days only


def _open(path):
    """Read-only. A database whose -wal file is gone (the writer stopped cleanly) cannot be opened as read-only WAL here: it is then read as it is (immutable)."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error:
        db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=10)
    db.execute("PRAGMA query_only = ON")
    return db


def _json(text):
    try:
        v = json.loads(text) if text else {}
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _day_range(day):
    try:
        t0 = calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000
    except ValueError:
        raise OSError("not a day")
    return t0, t0 + 86_400_000


def _names(data):
    """strategy id -> name, from the file watch.py publishes for the Trading tab."""
    try:
        with open(os.path.join(data, "watch.json"), encoding="utf-8") as f:
            w = json.load(f)
        return {s["id"]: s.get("name") or s["id"] for s in w.get("tradable", []) + w.get("strategies", [])}, w
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}


def status(data):
    """Coins being recorded now, today's counts and the recorded days."""
    now = int(time.time() * 1000)
    names, w = _names(data)
    midnight = now - now % 86_400_000
    with closing(_open(os.path.join(data, "watch.db"))) as db:
        active = []
        for mint, st, grad, seen, info in db.execute(
                "SELECT mint, status, graduated_ts, first_seen_ts, info FROM tokens WHERE status = 'new' OR "
                "(status = 'active' AND graduated_ts >= ?) ORDER BY COALESCE(graduated_ts, first_seen_ts) DESC LIMIT ?",
                (now - HORIZON_MIN * MIN, ACTIVE_LIMIT)):
            i = _json(info)
            n, picks, late = db.execute("SELECT COUNT(*), COALESCE(SUM(pick), 0), COALESCE(SUM(late), 0) "
                                        "FROM decisions WHERE mint = ?", (mint,)).fetchone()
            spark = [[e, m] for e, m in db.execute(
                "SELECT entry, json_extract(features, '$.mcap_usd') FROM measures WHERE mint = ? ORDER BY entry", (mint,))
                if m is not None]
            active.append({"mint": mint, "symbol": i.get("symbol"), "name": i.get("name"), "status": st, "graduated_at": grad,
                           "first_seen": seen, "classic": i.get("classic"), "decisions": n, "bought": picks, "late": late,
                           "mcap": spark[-1][1] if spark else None, "spark": spark})
        today = db.execute("SELECT COUNT(*), COALESCE(SUM(archived = 1), 0), COALESCE(SUM(status = 'unchecked'), 0), "
                           "COALESCE(SUM(status = 'failed'), 0) FROM tokens WHERE graduated_ts >= ?", (midnight,)).fetchone()
        waiting = db.execute("SELECT COUNT(*) FROM tokens WHERE status = 'new'").fetchone()[0]
        # decisions made within an hour of their minute are live ones; later ones are a strategy catching up on old coins
        # (reconstructed from their saved bars: never signals), and counting them as "late" hid the real picture
        late_today = db.execute(
            "SELECT COALESCE(SUM(lag < 3600000), 0), COALESCE(SUM(lag < 3600000 AND late = 1), 0), COALESCE(SUM(lag >= 3600000), 0) FROM ("
            "SELECT d.late AS late, d.decided_ts - (t.graduated_ts / 60000 * 60000 + d.entry * 60000) AS lag FROM decisions d "
            "JOIN tokens t USING (mint) WHERE d.decided_ts >= ?)", (midnight,)).fetchone()
        days = [{"day": d, "coins": n, "saved": s or 0} for d, n, s in db.execute(
            "SELECT date(graduated_ts / 1000, 'unixepoch') AS d, COUNT(*), SUM(archived = 1) FROM tokens "
            "WHERE graduated_ts IS NOT NULL AND graduated_ts >= ? GROUP BY d ORDER BY d DESC",
            (now - DAYS_SHOWN * 86_400_000,))]
    return {"now": now, "horizon_min": HORIZON_MIN, "active": active, "waiting": waiting,
            "today": {"coins": today[0], "saved": today[1], "unchecked": today[2], "failed": today[3]},
            "decisions_today": {"total": late_today[0], "late": late_today[1], "catch_up": late_today[2]},
            "sol_usd": solana.sol_usd_now() or None, "sol_usd_model": w.get("sol_usd"), "stake_sol": w.get("stake_sol"), "published_at": w.get("generated_at"), "days": days,
            "strategies": names}


def coins(data, day):
    """Every coin that graduated on `day` (UTC): how it was recorded and what the strategies did on it."""
    t0, t1 = _day_range(day)
    now = time.time()
    hit = _day_cache.get(day)
    if len(_day_cache) > 40:
        _day_cache.clear()
    if hit and now - hit[0] < 600 and t1 < (now - 86_400) * 1000:       # a finished day does not change (apart from late fills)
        return hit[1]
    with closing(_open(os.path.join(data, "watch.db"))) as db:
        out = []
        for mint, seen, st, grad, src, archived, info in db.execute(
                "SELECT mint, first_seen_ts, status, graduated_ts, graduated_src, archived, info FROM tokens "
                "WHERE graduated_ts >= ? AND graduated_ts < ? ORDER BY graduated_ts DESC", (t0, t1)):
            i = _json(info)
            n, picks, late, net = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(pick), 0), COALESCE(SUM(late), 0), "
                "SUM(CASE WHEN pick = 1 AND status = 'closed' THEN net_return END) FROM decisions WHERE mint = ?",
                (mint,)).fetchone()
            first = db.execute("SELECT entry, json_extract(features, '$.mcap_usd') FROM measures WHERE mint = ? "
                               "ORDER BY entry LIMIT 1", (mint,)).fetchone()
            out.append({"mint": mint, "symbol": i.get("symbol"), "name": i.get("name"), "status": st,
                        "graduated_at": grad, "src": src, "detected_sec": (seen - grad) / 1000 if seen and grad else None,
                        "classic": i.get("classic"), "saved": archived, "decisions": n, "bought": picks, "late": late,
                        "net": net, "first_entry": first[0] if first else None, "first_mcap": first[1] if first else None})
    _day_cache[day] = (now, out)
    return out


def coin(data, mint):
    """One coin: its bars (market cap = price x 1e9) around graduation and every strategy's decision on it."""
    names, _ = _names(data)
    with closing(_open(os.path.join(data, "watch.db"))) as db:
        row = db.execute("SELECT status, graduated_ts, graduated_src, first_seen_ts, archived, info FROM tokens WHERE mint = ?",
                         (mint,)).fetchone()
        if not row:
            return None
        st, grad, src, seen, archived, info = row
        i = _json(info)
        decisions = [{"strategy": s, "name": names.get(s, s), "entry": e, "pick": p, "status": status_, "exit": reason,
                      "held": held, "net": net, "late": late, "decided_at": dt, "price": px}
                     for s, e, p, status_, reason, held, net, late, dt, px in db.execute(
                         "SELECT d.strategy, d.entry, d.pick, d.status, d.exit_reason, d.minutes_held, d.net_return, d.late, "
                         "d.decided_ts, m.entry_price FROM decisions d LEFT JOIN measures m ON m.mint = d.mint AND m.entry = d.entry "
                         "WHERE d.mint = ? ORDER BY d.entry, d.strategy", (mint,))]
        measured = [[e, m] for e, m in db.execute(
            "SELECT entry, json_extract(features, '$.mcap_usd') FROM measures WHERE mint = ? ORDER BY entry", (mint,))
            if m is not None]
    bars = []
    if grad:
        g = grad // MIN * MIN
        try:
            with closing(_open(os.path.join(data, "pump.db"))) as pdb:
                bars = [[(t - g) // MIN, o * SUPPLY, h * SUPPLY, lo * SUPPLY, c * SUPPLY, v, n] for t, o, h, lo, c, v, n in pdb.execute(
                    "SELECT t, open, high, low, close, volume_usd, trades FROM candles WHERE mint = ? AND t >= ? AND t <= ? "
                    "ORDER BY t", (mint, g - 10 * MIN, g + HORIZON_MIN * MIN))]
        except sqlite3.Error:
            bars = []
    return {"mint": mint, "symbol": i.get("symbol"), "name": i.get("name"), "status": st, "graduated_at": grad, "src": src,
            "first_seen": seen, "saved": archived, "classic": i.get("classic"), "twitter": i.get("twitter"),
            "website": i.get("website"), "telegram": i.get("telegram"), "bars": bars, "measured": measured,
            "decisions": decisions, "horizon_min": HORIZON_MIN}

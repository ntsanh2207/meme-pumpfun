"""How a Solana wallet really trades meme coins: per-token P&L from its transactions, with a running average cost, and a summary with
findings. A port of Rug Radar's src/wallet/ledger.js and review.js. Run by jobs.py (a heavy job, so not in the web service).
"""
import datetime
import math
import re
import time

import coins
import solana
import walletparse as P

DUST = 0.5
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
TRANSFER_LIMIT = 1000
RECENT_TRADES = 200
SERIES_POINTS = 1500
BASE58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


class Cancelled(Exception):
    pass


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    h = len(s) // 2
    return s[h] if len(s) % 2 else (s[h - 1] + s[h]) / 2


def month(t):
    return datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m")


def day(t):
    return datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")


def blank():
    return {"buys": 0, "sells": 0, "disposals": 0, "swapsIn": 0, "swapsOut": 0, "cost": 0.0, "basisIn": 0.0, "proceeds": 0.0, "swapProceeds": 0.0,
            "unknownProceeds": 0.0, "realized": 0.0, "qty": 0.0, "basis": 0.0, "freeQty": 0.0, "unknownQty": 0.0, "bought": 0.0, "sold": 0.0,
            "freeIn": 0.0, "freeSold": 0.0, "transferredOutBasis": 0.0, "first": None, "last": None}


def take(e, amt):
    """Remove `amt` tokens: the basis removed, how many had zero basis (airdrops), and how many had unknown basis."""
    from_pos = min(amt, max(0.0, e["qty"]))
    frac = from_pos / e["qty"] if e["qty"] > 0 else 0
    out = {"basis": e["basis"] * frac, "free": e["freeQty"] * frac, "unknown": e["unknownQty"] * frac + (amt - from_pos)}
    e["basis"] -= out["basis"]
    e["freeQty"] -= out["free"]
    e["unknownQty"] -= e["unknownQty"] * frac
    e["qty"] -= from_pos
    return out


def buy_(e, amt, cost):
    e["buys"] += 1
    e["cost"] += cost
    e["bought"] += amt
    e["qty"] += amt
    e["basis"] += cost


def free_(e, amt):
    e["freeIn"] += amt
    e["freeQty"] += amt
    e["qty"] += amt


def sell_(e, amt, quote, is_disposal):
    t = take(e, amt)
    known_share = 1 - t["unknown"] / amt
    known = quote * known_share
    e["proceeds"] += known
    e["unknownProceeds"] += quote - known
    e["realized"] += known - t["basis"]
    e["freeSold"] += quote * (t["free"] / amt)
    e["sold"] += amt
    if is_disposal:
        e["disposals"] += 1
    else:
        e["sells"] += 1
    return {"pnl": known - t["basis"] if known_share > 1e-9 or t["basis"] > 0 else None, "cost": t["basis"], "knownUsd": known, "unknownShare": 1 - known_share}


def transfer_out(e, amt):
    e["transferredOutBasis"] += take(e, amt)["basis"]


def build_positions(rows, log=None):
    """Rows must be in time order. `log` gets one entry per buy, sale and coin-to-coin swap, in order."""
    cur = [None]

    def note(x):
        if log is not None:
            log.append(x)

    def log_disposal(m, amt, x, **extra):
        r = cur[0]
        if x["cost"] >= 1:
            note({"t": r["t"], "sig": r["sig"], "mint": m, "side": "sell", "tokens": amt, "usd": 0, "dumped": True, **x, **extra})
    Pm = {}

    def get(m):
        if m not in Pm:
            Pm[m] = blank()
        return Pm[m]
    score = {}
    for r in rows:
        for m in r["memes"]:
            score[m] = score.get(m, 0) + (1000 if r["kind"] in ("buy", "sell") else 1)

    def pick(lst):
        best = lst[0]
        for x in lst[1:]:
            if score.get(x[0], 0) > score.get(best[0], 0):
                best = x
        return best
    for r in rows:
        cur[0] = r
        entries = list(r["memes"].items())
        if not entries or r["kind"] in ("deposit", "withdraw", "convert", "dust"):
            continue
        for m, _ in entries:
            e = get(m)
            if e["first"] is None:
                e["first"] = r["t"]
            e["last"] = r["t"]
        pos = [(m, a) for m, a in entries if a > 0]
        neg = [(m, a) for m, a in entries if a < 0]
        if pos and neg and r.get("swapGiven"):
            value, sold = 0.0, {}
            main_m0 = pick(pos)[0]
            for m, a in neg:
                v = r["swapGiven"].get(m, 0)
                e = get(m)
                cash_before = e["proceeds"]
                x = {"t": r["t"], "sig": r["sig"], "mint": m, "side": "sell", "tokens": -a, "usd": v, **sell_(e, -a, v, False), "via": main_m0}
                e["sells"] -= 1
                e["swapsOut"] += 1
                sold[m] = x
                note(x)
                e["swapProceeds"] += e["proceeds"] - cash_before
                e["proceeds"] = cash_before
                value += v
            if r["quote"] > 0:
                m_neg = pick(neg)[0]
                e = get(m_neg)
                e["realized"] += r["quote"]
                e["proceeds"] += r["quote"]
                x = sold[m_neg]
                x["usd"] += r["quote"]
                x["knownUsd"] += r["quote"]
                x["pnl"] = (x["pnl"] or 0) + r["quote"]
            main_m, main_a = pick(pos)
            eb = get(main_m)
            cost = value + (-r["quote"] if r["quote"] < 0 else 0)
            eb["swapsIn"] += 1
            eb["qty"] += main_a
            eb["bought"] += main_a
            eb["basis"] += cost
            eb["basisIn"] += cost
            note({"t": r["t"], "sig": r["sig"], "mint": main_m, "side": "buy", "tokens": main_a, "usd": cost, "via": pick(neg)[0]})
            for m, a in pos:
                if m != main_m:
                    free_(get(m), a)
        elif pos and neg:
            moved, unknown_frac, free_frac, cash = 0.0, 0.0, 0.0, None
            for m, a in neg:
                e = get(m)
                t = take(e, -a)
                moved += t["basis"]
                unknown_frac += t["unknown"] / -a / len(neg)
                free_frac += t["free"] / -a / len(neg)
                e["swapsOut"] += 1
                e["sold"] -= a
            if r["quote"] > 0:
                e = get(pick(neg)[0])
                known = r["quote"] * (1 - unknown_frac)
                e["realized"] += known
                e["proceeds"] += known
                e["unknownProceeds"] += r["quote"] - known
                cash = known
            main_m, main_a = pick(pos)
            eb = get(main_m)
            extra = -r["quote"] if r["quote"] < 0 else 0
            eb["swapsIn"] += 1
            eb["qty"] += main_a
            eb["bought"] += main_a
            eb["basis"] += moved + extra
            eb["basisIn"] += moved
            eb["cost"] += extra
            eb["unknownQty"] += main_a * unknown_frac
            eb["freeQty"] += main_a * free_frac
            note({"t": r["t"], "sig": r["sig"], "mint": main_m, "side": "swap", "tokens": main_a, "usd": None, "via": pick(neg)[0], **({"cashPnl": cash} if cash is not None else {})})
            for m, a in pos:
                if m != main_m:
                    free_(get(m), a)
        elif pos:
            if r["quote"] < -DUST:
                main_m, main_a = pick(pos)
                buy_(get(main_m), main_a, -r["quote"])
                note({"t": r["t"], "sig": r["sig"], "mint": main_m, "side": "buy", "tokens": main_a, "usd": -r["quote"]})
                for m, a in pos:
                    if m != main_m:
                        free_(get(m), a)
            else:
                for m, a in pos:
                    free_(get(m), a)
        elif r.get("plainTransfer"):
            for m, a in neg:
                transfer_out(get(m), -a)
        elif r.get("basicOnly"):
            for m, a in neg:
                log_disposal(m, -a, sell_(get(m), -a, 0, True), burned=True)
        elif r["quote"] > DUST:
            main_m, main_a = pick(neg)
            note({"t": r["t"], "sig": r["sig"], "mint": main_m, "side": "sell", "tokens": -main_a, "usd": r["quote"], **sell_(get(main_m), -main_a, r["quote"], False)})
            for m, a in neg:
                if m != main_m:
                    log_disposal(m, -a, sell_(get(m), -a, 0, True))
        else:
            main_m, main_a = pick(neg)
            note({"t": r["t"], "sig": r["sig"], "mint": main_m, "side": "sell", "tokens": -main_a, "usd": r["quote"], "dumped": True, **sell_(get(main_m), -main_a, r["quote"], True)})
            for m, a in neg:
                if m != main_m:
                    log_disposal(m, -a, sell_(get(m), -a, 0, True))
    return Pm


def valuate(positions, held, meta, now=None, unpriced=frozenset()):
    now = now or time.time()
    out = []
    for mint, e in positions.items():
        if not (e["buys"] or e["sells"] or e["swapsIn"] or e["swapsOut"]):
            continue
        info = meta.get(mint) or {}
        priced = mint not in unpriced
        held_amt = held.get(mint, 0)
        price = (info.get("price") or 0) if priced else 0
        share = min(held_amt, e["qty"]) / e["qty"] if e["qty"] > 0 else 0
        rem_basis = e["basis"] * share
        untracked = e["basis"] * (1 - share) if e["qty"] > 0 else 0
        unknown_share = e["unknownQty"] / e["qty"] if e["qty"] > 0 else 0
        held_value = held_amt * price
        known_value = min(held_amt, max(0, e["qty"])) * price * (1 - unknown_share)
        unrealized = known_value - rem_basis if priced else None
        status = "unpriced" if held_amt > 0 and not priced else "open" if held_value >= 1 else "dead" if rem_basis >= 1 else "closed"
        invested = e["cost"] + e["basisIn"]
        total = e["realized"] - untracked + (unrealized or 0)
        end = now if status in ("open", "dead", "unpriced") else e["last"]
        out.append({"mint": mint, "symbol": info.get("symbol"), "status": status, "priced": priced, "buys": e["buys"], "sells": e["sells"], "disposals": e["disposals"],
                    "swapsIn": e["swapsIn"], "swapsOut": e["swapsOut"], "cost": e["cost"], "basisIn": e["basisIn"], "invested": invested, "proceeds": e["proceeds"],
                    "swapProceeds": e["swapProceeds"], "realized": e["realized"] - untracked, "unrealized": unrealized, "total": total,
                    "roi": total / invested * 100 if invested > 1 else None, "heldValue": held_value, "freeSold": e["freeSold"], "unknownProceeds": e["unknownProceeds"],
                    "transferredOutBasis": e["transferredOutBasis"], "untracked": untracked, "first": e["first"], "last": e["last"],
                    "holdHours": (end - e["first"]) / 3600, "liquidity": info.get("liquidity"), "createdAt": info.get("createdAt")})
    return sorted(out, key=lambda t: t["total"])


def usd0(n):
    a, s = abs(n), "−" if n < 0 else "+" if n > 0 else ""
    return s + "$" + (f"{a / 1e6:.2f}M" if a >= 1e6 else re.sub(r"\.0$", "", f"{a / 1e3:.1f}") + "K" if a >= 1e4 else f"{int(a + 0.5):,}")


def short(a):
    return a[:4] + "…" + a[-4:]


def summarize(tokens, rows, sol_balance=0, sol_price_now=0, start_sol=None):
    S = lambda xs, f: sum(f(x) for x in xs)  # noqa: E731
    closed = [t for t in tokens if t["status"] == "closed" and not (t["swapsOut"] > 0 and abs(t["total"]) < 1)]
    open_ = [t for t in tokens if t["status"] == "open"]
    dead = [t for t in tokens if t["status"] == "dead"]
    unpriced = [t for t in tokens if t["status"] == "unpriced"]
    totals = {"invested": S(tokens, lambda t: t["invested"]), "bought": S(tokens, lambda t: t["cost"]), "proceeds": S(tokens, lambda t: t["proceeds"]),
              "realized": S(tokens, lambda t: t["realized"]), "unrealized": S(tokens, lambda t: t["unrealized"] or 0), "heldValue": S(tokens, lambda t: t["heldValue"]),
              "total": S(tokens, lambda t: t["total"])}
    gross_win = S([t for t in closed if t["total"] > 0], lambda t: t["total"])
    gross_loss = S([t for t in closed if t["total"] <= 0], lambda t: t["total"])
    closed_pnl = gross_win + gross_loss
    by_total = sorted(tokens, key=lambda t: -t["total"])
    top2 = [t for t in by_total[:2] if t["total"] > 0]
    dead_pnl = S(dead, lambda t: t["total"])
    buys = [r for r in rows if r["kind"] == "buy"]
    sells = [r for r in rows if r["kind"] == "sell"]
    sizes = sorted(-r["quote"] for r in buys)
    per_day = {}
    for r in buys:
        per_day[day(r["t"])] = per_day.get(day(r["t"]), 0) + 1
    hours = [0] * 24
    for r in buys:
        hours[datetime.datetime.utcfromtimestamp(r["t"]).hour] += 1
    lags = [(t["first"] * 1000 - t["createdAt"]) / 60000 for t in tokens if t.get("createdAt") and t["first"] * 1000 >= t["createdAt"]]

    def bucket(label, lo, hi):
        s = [t for t in closed if lo <= t["holdHours"] < hi]
        return {"label": label, "tokens": len(s), "pnl": S(s, lambda t: t["total"])}
    holds = [bucket("Under 5 minutes", 0, 1 / 12), bucket("5–60 minutes", 1 / 12, 1), bucket("1–24 hours", 1, 24), bucket("Over 1 day", 24, math.inf)]
    avg_down = [t for t in closed if t["buys"] >= 3 and t["sells"] + t["disposals"] == 1]
    swap_losers = sorted([t for t in closed if t["basisIn"] > 100 and t["swapsOut"] == 0 and t["total"] <= -0.95 * t["invested"]], key=lambda t: t["total"])
    monthly = {}
    for t in tokens:
        monthly[month(t["last"])] = monthly.get(month(t["last"]), 0) + t["total"]
    flows = {}
    for kind in ("deposit", "withdraw"):
        rs = [r for r in rows if r["kind"] == kind]
        cp = {}
        for r in rs:
            for c in (r["cps"] or ["unknown"]):
                e = cp.setdefault(c, {"address": c, "count": 0, "usd": 0.0})
                e["count"] += 1
                e["usd"] += r["quote"]
        flows[kind] = {"count": len(rs), "usd": S(rs, lambda r: r["quote"]), "sol": S(rs, lambda r: r["sol"]), "stable": S(rs, lambda r: r["stable"]),
                       "top": sorted(cp.values(), key=lambda c: -abs(c["usd"]))[:5]}
    programs = {}
    for r in rows:
        for p in r.get("programs") or []:
            programs[p] = programs.get(p, 0) + 1

    paper_heavy = totals["unrealized"] > 0 and totals["unrealized"] > 2 * max(0, totals["realized"])
    if totals["total"] > 0 and totals["realized"] > 0 and not paper_heavy:
        verdict = {"sev": "ok", "label": "Profitable"}
    elif totals["total"] > 0:
        verdict = {"sev": "warn", "label": "Up on paper"}
    elif totals["total"] > -0.05 * max(totals["invested"], 1):
        verdict = {"sev": "warn", "label": "Roughly break-even"}
    else:
        verdict = {"sev": "crit", "label": "Losing"}

    findings = []

    def add(sev, title, metric, detail):
        findings.append({"sev": sev, "title": title, "metric": metric, "detail": detail})
    if paper_heavy and totals["total"] > 0:
        add("warn", "Profit depends on open positions", usd0(totals["unrealized"]),
            f"Unrealized gains of {usd0(totals['unrealized'])} versus {usd0(totals['realized'])} realized. Meme coin prices can reverse within hours.")
    if top2 and totals["total"] > 0 and S(top2, lambda t: t["total"]) > totals["total"]:
        w = totals["total"] - S(top2, lambda t: t["total"])
        add("warn", "A couple of trades carry everything", usd0(w), f"Without its two best tokens ({', '.join(t['symbol'] or short(t['mint']) for t in top2)}) the wallet is at {usd0(w)}.")
    if len(avg_down) >= 5 and S(avg_down, lambda t: t["total"]) < 0:
        p = S(avg_down, lambda t: t["total"])
        add("crit" if -p > abs(gross_loss) * 0.3 else "warn", "Averaging down", usd0(p),
            f"{len(avg_down)} tokens bought 3+ times and sold once lost {usd0(p)}" + (f" ({round(p / gross_loss * 100)}% of losses on finished trades)" if gross_loss < 0 else "") + ".")
    if swap_losers:
        p = S(swap_losers, lambda t: t["total"])
        add("crit" if -p > 1000 else "warn", "Swaps into coins that collapsed", usd0(p),
            f"{len(swap_losers)} coin-to-coin swap{'s' if len(swap_losers) > 1 else ''} into tokens later sold for under 5% of their value. That pattern fits fake copycats or rugs.")
    if dead and dead_pnl < -100:
        add("crit" if -dead_pnl > abs(totals["total"]) * 0.25 else "warn", "Still holding dead coins", usd0(dead_pnl),
            f"{len(dead)} token{'s are' if len(dead) > 1 else ' is'} still in the wallet but now worth under $1, a loss of {usd0(dead_pnl)}.")
    worst_hold = sorted(holds, key=lambda h: h["pnl"])[0]
    best_hold = sorted(holds, key=lambda h: -h["pnl"])[0]
    if worst_hold["tokens"] and worst_hold["pnl"] < 0:
        add("warn", f"Worst holding period: {worst_hold['label'].lower()}", usd0(worst_hold["pnl"]), f"{worst_hold['tokens']} finished tokens held this long lost {usd0(worst_hold['pnl'])}.")
    if best_hold["tokens"] and best_hold["pnl"] > 0:
        add("ok", f"Best holding period: {best_hold['label'].lower()}", usd0(best_hold["pnl"]), f"{best_hold['tokens']} finished tokens held this long made {usd0(best_hold['pnl'])}.")
    win_rate = sum(1 for t in closed if t["total"] > 0) / len(closed) * 100 if closed else None
    if win_rate is not None:
        add("warn" if win_rate < 30 else "ok" if win_rate >= 50 else "info", "Win rate on finished trades", f"{win_rate:.0f}%",
            f"{sum(1 for t in closed if t['total'] > 0)} of {len(closed)} finished tokens made money. Profit factor " + (f"{gross_win / -gross_loss:.2f}" if gross_loss else "∞") + ".")
    dead_count = sum(1 for t in tokens if (t["liquidity"] or 0) < 1000)
    if tokens:
        add("warn" if dead_count / len(tokens) > 0.4 else "info", "Tokens now dead", f"{dead_count}/{len(tokens)}", f"{dead_count} of the tokens it traded have under $1K of liquidity today.")
    findings.sort(key=lambda f: {"crit": 0, "warn": 1, "info": 2, "ok": 3}[f["sev"]])

    net_sol = S(rows, lambda r: r["sol"])
    dispose_n = sum(1 for r in rows if r["kind"] == "dispose")
    convert_rows = [r for r in rows if r["kind"] == "convert"]
    return {
        "verdict": verdict, "totals": totals, "findings": findings,
        "counts": {"tokens": len(tokens), "closed": len(closed), "open": len(open_), "dead": len(dead), "unpriced": len(unpriced), "buys": len(buys), "sells": len(sells),
                   "disposals": dispose_n, "swaps": S(tokens, lambda t: t["swapsIn"])},
        "closedPnl": closed_pnl, "grossWin": gross_win, "grossLoss": gross_loss, "winRate": win_rate, "profitFactor": gross_win / -gross_loss if gross_loss else None,
        "deadHeld": {"tokens": len(dead), "pnl": dead_pnl}, "medianRoi": median([t["roi"] for t in tokens if t["roi"] is not None]),
        "medianHoldMinutes": (median([t["holdHours"] for t in closed]) or 0) * 60, "withoutTop2": totals["total"] - S(top2, lambda t: t["total"]),
        "best": [t for t in by_total if t["total"] >= 1][:8], "worst": [t for t in tokens if t["total"] <= -1][:8],
        "open": sorted(open_ + unpriced, key=lambda t: -t["heldValue"]),
        "monthly": [{"month": m, "pnl": p} for m, p in sorted(monthly.items())],
        "style": {"medianBuy": median(sizes), "p90Buy": sizes[int(0.9 * (len(sizes) - 1))] if sizes else None, "maxBuy": sizes[-1] if sizes else None,
                  "roundShare": sum(1 for s in sizes if abs(s - round(s / 50) * 50) < 2) / len(sizes) * 100 if sizes else None,
                  "paidStable": sum(1 for r in buys if r["stable"] < -DUST), "paidSol": sum(1 for r in buys if r["stable"] >= -DUST),
                  "selfSigned": sum(1 for r in buys if r["payer"]) / len(buys) * 100 if buys else None, "buysPerDay": median(list(per_day.values())),
                  "entryLagMinutes": median(lags), "entryUnder5": sum(1 for l in lags if l < 5) / len(lags) * 100 if lags else None, "hours": hours,
                  "topPrograms": [{"id": i, "n": n} for i, n in sorted(programs.items(), key=lambda kv: -kv[1])[:6]]},
        "holds": holds, "averagingDown": {"tokens": len(avg_down), "pnl": S(avg_down, lambda t: t["total"])},
        "singleBuy": {"tokens": sum(1 for t in closed if t["buys"] == 1), "pnl": S([t for t in closed if t["buys"] == 1], lambda t: t["total"])},
        "adjustments": {"freeSold": S(tokens, lambda t: t["freeSold"]), "unknownProceeds": S(tokens, lambda t: t["unknownProceeds"]),
                        "transferredOutBasis": S(tokens, lambda t: t["transferredOutBasis"]), "untracked": S(tokens, lambda t: t["untracked"])},
        "deadTokens": dead_count, "flows": flows, "converts": {"count": len(convert_rows), "usd": S(convert_rows, lambda r: r["quote"])},
        "activity": {"first": rows[0]["t"] if rows else None, "last": rows[-1]["t"] if rows else None, "daysActive": len({day(r["t"]) for r in rows})},
        "holdings": {"sol": sol_balance, "solUsd": sol_balance * sol_price_now, "tokensUsd": totals["heldValue"]},
        "reconcile": None if start_sol is None else {"startSol": start_sol, "endSol": sol_balance, "netSolFlows": net_sol, "gap": sol_balance - (start_sol + net_sol)},
    }


def pnl_series(log):
    run, pts = 0.0, []
    for x in log:
        v = x.get("pnl") if x["side"] == "sell" and x.get("pnl") is not None else x.get("cashPnl")
        if v is None or not math.isfinite(v):
            continue
        run += v
        pts.append([x["t"], round(run * 100) / 100])
    if len(pts) <= SERIES_POINTS:
        return pts
    step = len(pts) / SERIES_POINTS
    return [pts[int(i * step)] for i in range(SERIES_POINTS - 1)] + [pts[-1]]


def token_meta(mints):
    """Names/prices, plus the mints whose lookup failed (as opposed to mints DexScreener does not list, which are dead)."""
    meta, failed = {}, set()
    for i in range(0, len(mints), 30):
        batch = mints[i:i + 30]
        try:
            data = coins.get_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch), 20)
            for p in data if isinstance(data, list) else []:
                m = (p.get("baseToken") or {}).get("address")
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if m in batch and (m not in meta or liq > meta[m]["liquidity"]):
                    meta[m] = {"symbol": p["baseToken"].get("symbol"), "price": coins.num(p.get("priceUsd")) or 0, "liquidity": liq, "createdAt": p.get("pairCreatedAt")}
        except coins.LookupFailed:
            failed.update(batch)
    return meta, failed


def holdings(data, wallet):
    held = {}
    for program in TOKEN_PROGRAMS:
        r = solana.call(data, "getTokenAccountsByOwner", [wallet, {"programId": program}, {"encoding": "jsonParsed"}])
        for a in (r or {}).get("value", []):
            info = a["account"]["data"]["parsed"]["info"]
            held[info["mint"]] = held.get(info["mint"], 0.0) + (float(info["tokenAmount"].get("uiAmountString") or 0))
    bal = solana.call(data, "getBalance", [wallet])
    return held, ((bal or {}).get("value") or 0) / 1e9


def gecko_price_history(mint, oldest, max_calls=3):
    """Hourly USD price history of the coin's deepest pool from GeckoTerminal: (at(t) -> price or None, calls)."""
    calls = 0
    if not BASE58.fullmatch(str(mint)):
        return None, calls
    G = coins.GECKO
    try:
        if calls >= max_calls or not coins.gecko_allow():
            return None, calls
        calls += 1
        pools = (coins.get_json(f"{G}/networks/solana/tokens/{mint}/pools", 300).get("data")) or []
        pools = [p for p in pools if BASE58.fullmatch(str((p.get("attributes") or {}).get("address", "")))]
        if not pools:
            return None, calls
        top = max(pools, key=lambda p: coins.num(p["attributes"].get("reserve_in_usd")) or 0)
        lst, before = [], int(time.time()) + 3600
        while before > oldest:
            if calls >= max_calls or not coins.gecko_allow():
                break
            calls += 1
            url = f"{G}/networks/solana/pools/{top['attributes']['address']}/ohlcv/hour?aggregate=1&limit=1000&currency=usd&token={mint}&before_timestamp={before}"
            page = ((coins.get_json(url, 300).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            if not page:
                break
            for c in page:
                p = ((coins.num(c[1]) or 0) + (coins.num(c[4]) or 0)) / 2
                if (coins.num(c[0]) or 0) > 0 and p > 0:
                    lst.append((int(c[0]), p))
            earliest = min(int(c[0]) for c in page)
            if not earliest < before or len(page) < 1000:
                break
            before = earliest
        lst.sort()
        if not lst:
            return None, calls
        import bisect
        ts = [t for t, _ in lst]

        def at(t):
            i = bisect.bisect_right(ts, t) - 1
            return lst[i][1] if i >= 0 and t - lst[i][0] < 3 * 3600 else None
        return at, calls
    except coins.LookupFailed as e:
        if e.status == 429:
            coins._gecko["paused"] = time.time() + 60
        return None, calls


def review_wallet(data, wallet, days=90, max_tx=20000, progress=lambda **k: None, cancel=None, all_transfers=False):
    if not BASE58.fullmatch(wallet):
        raise RuntimeError("That isn't a valid Solana wallet address.")
    since = int(time.time()) - days * 86400
    notes = []

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled("Review cancelled.")
    if not solana.rpc_url(data)[1]:
        notes.append("Using the public Solana RPC, which is heavily rate-limited. Save an RPC key on the Settings page for faster reviews.")
    progress(stage="Finding transactions")
    sigs, truncated, oldest = solana.get_signatures_all(data, wallet, since, max_tx, cancel)
    check()
    if not sigs:
        raise RuntimeError(f"No successful transactions in the last {days} days.")
    effective_since = oldest if truncated else since
    if truncated:
        notes.append(f"This wallet is very active. Only its newest {max_tx:,} transactions were reviewed, covering {time.strftime('%Y-%m-%d', time.gmtime(oldest))} onward.")
    progress(stage="Loading SOL prices")
    at, now_price, historical = solana.sol_prices(effective_since)
    if not historical:
        notes.append("Historical SOL prices were unavailable, so every trade was valued at today's SOL price.")
    check()
    parsed, accts = {}, set()
    pos = {s: i for i, s in enumerate(sigs)}

    def take_tx(s, tx):
        parsed[s] = {"t": tx.get("blockTime"), "slot": tx.get("slot") or 0, "idx": tx.get("transactionIndex") or -pos.get(s, -1),
                     "row": P.parse_tx(tx, wallet, at), "preSol": P.pre_sol(tx, wallet)}
        accts.update(P.quote_accounts(tx, wallet))
    stage = lambda name: (lambda d, t: progress(stage=name, done=d, total=t))  # noqa: E731
    unreadable = solana.get_transactions_stream(data, sigs, take_tx, progress=stage("Downloading transactions"), cancel=cancel)
    check()
    progress(stage="Checking incoming transfers")
    extra, sig_set, acct_truncated = set(), set(sigs), False
    for a in accts:
        s2, tr, _ = solana.get_signatures_all(data, a, effective_since, max_tx, cancel)
        acct_truncated = acct_truncated or tr
        extra.update(x for x in s2 if x not in sig_set)
    check()
    skipped = len(extra) if not all_transfers and len(extra) > TRANSFER_LIMIT else 0
    if skipped:
        notes.append(f"The wallet's SOL or stablecoin account received {skipped:,} incoming transfers the wallet didn't sign (busy wallets get many small fee or reward payments). They were skipped to keep the review fast.")
    else:
        if acct_truncated:
            notes.append("One of the wallet's SOL or stablecoin accounts is very busy, so some older transfers to it may be missing.")
        if extra:
            unreadable += solana.get_transactions_stream(data, list(extra), take_tx, progress=stage("Downloading incoming transfers"), cancel=cancel)
        check()
    wanted = len(sig_set) + (0 if skipped else len(extra))
    got = len(parsed) + unreadable
    if got < wanted:
        notes.append(f"{wanted - got} of {wanted} transactions couldn't be downloaded, so a few trades may be missing.")
    if unreadable:
        notes.append(f"{unreadable} transaction{'s' if unreadable > 1 else ''} couldn't be read and {'were' if unreadable > 1 else 'was'} left out.")
    progress(stage="Analyzing trades")
    ordered = sorted(parsed.values(), key=lambda x: (x["slot"], x["idx"], x["t"] or 0))
    rows = [x["row"] for x in ordered]
    deferred = P.link_deferred_proceeds(rows)
    if deferred:
        notes.append(f"{deferred} sale{'s were' if deferred > 1 else ' was'} paid out in a separate transaction a little later (the router settles it afterwards); {'they are' if deferred > 1 else 'it is'} counted with the sale.")
    unpriced_swaps = [r for r in rows if r["kind"] == "multi" and not r.get("swapGiven") and not r.get("plainTransfer")
                      and any(v < 0 for v in r["memes"].values()) and any(v > 0 for v in r["memes"].values())]
    if unpriced_swaps:
        progress(stage="Pricing coin-to-coin swaps")
        count, old = {}, {}
        for r in unpriced_swaps:
            for m in r["memes"]:
                count[m] = count.get(m, 0) + 1
                old[m] = min(old.get(m, math.inf), r["t"])
        history, budget = {}, 8
        for m in sorted(count, key=lambda m: -count[m]):
            if budget < 2:
                break
            h, calls = gecko_price_history(m, old[m] - 3600, min(3, budget))
            budget -= calls
            if h:
                history[m] = h
            check()

        def at_price(m, t):
            return history[m](t) if m in history else None
        priced = 0
        for r in unpriced_swaps:
            given = [(m, v) for m, v in r["memes"].items() if v < 0]
            got_ = [(m, v) for m, v in r["memes"].items() if v > 0]
            gave = sum(-v * at_price(m, r["t"]) for m, v in given) if all((at_price(m, r["t"]) or 0) > 0 for m, _ in given) else None
            received = sum(v * at_price(m, r["t"]) for m, v in got_) if all((at_price(m, r["t"]) or 0) > 0 for m, _ in got_) else None
            if gave is not None and received is not None and max(gave, received) > 3 * min(gave, received):
                continue
            if gave is not None:
                r["swapGiven"] = {m: -v * at_price(m, r["t"]) for m, v in given}
            elif len(given) == 1 and received is not None:
                r["swapGiven"] = {given[0][0]: received}
            if r.get("swapGiven"):
                r["pricedBy"] = "history"
                priced += 1
        if priced:
            notes.append(f"{priced} coin-to-coin swap{'s were' if priced > 1 else ' was'} valued at the coins' hourly prices (GeckoTerminal), since no SOL or stablecoin changed hands.")
        if priced < len(unpriced_swaps):
            n = len(unpriced_swaps) - priced
            notes.append(f"{n} coin-to-coin swap{'s' if n > 1 else ''} couldn't be valued, so the coins received count as free (zero cost).")
    start_sol = next((x["preSol"] for x in ordered if x["preSol"] is not None), None)
    trade_log = []
    positions = build_positions(rows, trade_log)
    progress(stage="Pricing open positions")
    check()
    held, sol_bal = holdings(data, wallet)
    meta, failed = token_meta(list(positions))
    for m in [m for m in positions if m not in meta and m not in failed and held.get(m, 0) > 0][:60]:
        one, fl = token_meta([m])
        if m in one:
            meta[m] = one[m]
        failed |= fl
    check()
    unpriced = {m for m in failed if held.get(m, 0) > 0}
    if unpriced:
        notes.append(f"Prices for {len(unpriced)} held token{'s' if len(unpriced) > 1 else ''} couldn't be loaded, so their unrealized P&L is left out.")
    tokens = valuate(positions, held, meta, time.time(), unpriced)
    summary = summarize(tokens, rows, sol_bal, now_price, start_sol)
    if summary["reconcile"] and skipped:
        summary["reconcile"]["partial"] = True
    adj = summary["adjustments"]
    if adj["unknownProceeds"] >= 50:
        notes.append(f"${round(adj['unknownProceeds']):,} came from selling tokens bought before this window. Their cost is unknown, so it's left out of P&L.")
    recent = []
    for x in reversed(trade_log[-RECENT_TRADES:]):
        recent.append({**x, "symbol": (meta.get(x["mint"]) or {}).get("symbol"), **({"viaSymbol": (meta.get(x["via"]) or {}).get("symbol")} if x.get("via") else {}),
                       "pnlPct": x["pnl"] / x["cost"] * 100 if x.get("pnl") is not None and (x.get("cost") or 0) >= 1 else None})
    now_s = time.time()
    activity = []
    for x in trade_log:
        if x["t"] >= now_s - 8 * 86400 and (x["side"] in ("buy", "sell") or x.get("cashPnl") is not None):
            if x["side"] == "swap":
                activity.append({"t": x["t"], "side": "cash", "usd": 0, "pnl": round(x["cashPnl"] * 100) / 100})
            else:
                activity.append({"t": x["t"], "side": x["side"], "usd": round((x.get("usd") or 0) * 100) / 100, "pnl": None if x.get("pnl") is None else round(x["pnl"] * 100) / 100})
    return {"wallet": wallet, "days": days, "since": effective_since, "generatedAt": int(time.time() * 1000), "notes": notes,
            "txCount": {"wallet": len(sigs), "tokenAccounts": 0 if skipped else len(extra), "skippedTransfers": skipped, "downloaded": got}, "allTransfers": all_transfers,
            "summary": summary,
            "tokens": (lambda ts: ts if len(ts) <= 1500 else ts[:750] + ts[-750:])(sorted(tokens, key=lambda t: -t["total"])), "recentTrades": recent, "tradeCount": len(trade_log), "pnlSeries": pnl_series(trade_log),
            "pnlSales": sum(1 for x in trade_log if (x["side"] == "sell" and x.get("pnl") is not None) or x.get("cashPnl") is not None), "recentActivity": activity}

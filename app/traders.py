"""Who made money on a token, from its full on-chain history (Rug Radar's src/traders/). Downloads every transaction that touched
the mint, works out each wallet's profit, then finds the insider groups: the launch block, bag-splitting distributors, same-size
holder clusters and shared funders. Run by jobs.py (a heavy job, so not in the web service).
"""
import json
import time
import urllib.request

import solana

MAX_TX = 40000
WSOL = "So11111111111111111111111111111111111111112"
STABLES = ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")
DUST_SOL = 0.0005


def _amt(b):
    t = b.get("uiTokenAmount") or {}
    try:
        return float(t.get("uiAmountString") or 0)
    except (TypeError, ValueError):
        return 0.0


def compact_tx(tx, mint, idx=0):
    """Everything the analysis needs from one transaction, a few hundred bytes instead of 20-50 KB of JSON: slot, position in the
    block, time, fee payer, and per wallet that touched the token [owner, token change, token balance after (None: closed),
    signed?, SOL change (wrapped SOL included), stablecoin change]."""
    msg, meta = tx["transaction"]["message"], tx["meta"]
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg["accountKeys"]]
    signers = {k["pubkey"] for k in msg["accountKeys"] if not isinstance(k, str) and k.get("signer")}
    pre, post_b = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []
    deltas, post = {}, {}
    for b in pre:
        if b.get("mint") == mint:
            deltas[b.get("owner")] = deltas.get(b.get("owner"), 0.0) - _amt(b)
    for b in post_b:
        if b.get("mint") == mint:
            deltas[b.get("owner")] = deltas.get(b.get("owner"), 0.0) + _amt(b)
            post[b.get("owner")] = post.get(b.get("owner"), 0.0) + _amt(b)
    moves = []
    for owner, d in deltas.items():
        i = keys.index(owner) if owner in keys else -1
        sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9 if i >= 0 else 0.0
        stable = 0.0
        for b in pre:
            if b.get("owner") == owner:
                if b["mint"] == WSOL:
                    sol -= _amt(b)
                elif b["mint"] in STABLES:
                    stable -= _amt(b)
        for b in post_b:
            if b.get("owner") == owner:
                if b["mint"] == WSOL:
                    sol += _amt(b)
                elif b["mint"] in STABLES:
                    stable += _amt(b)
        moves.append((owner, d, post.get(owner), owner in signers, sol, stable))
    return {"slot": tx.get("slot", 0), "idx": tx.get("transactionIndex") or idx, "t": tx.get("blockTime") or 0, "payer": keys[0] if keys else None, "moves": moves}


def compute_traders(recs, mint, known_pools, price_usd, sol_usd_at):
    ordered = sorted(recs, key=lambda r: (r["slot"], r["idx"]))
    T = {}

    def get(o):
        if o not in T:
            T[o] = {"owner": o, "buys": 0, "sells": 0, "spentUsd": 0.0, "gotUsd": 0.0, "spentSol": 0.0, "gotSol": 0.0, "tokBought": 0.0, "tokSold": 0.0,
                    "tokIn": 0.0, "tokOut": 0.0, "txs": 0, "signed": 0, "first": None, "last": None, "firstBuy": None, "bal": 0.0, "launchBlock": False}
        return T[o]
    launch_slot = ordered[0]["slot"] if ordered else None
    launch_time = ordered[0]["t"] if ordered else None
    creator = ordered[0]["payer"] if ordered else None
    curve = max(ordered[0]["moves"], key=lambda m: m[1])[0] if ordered and ordered[0]["moves"] else None
    for tx in ordered:
        sol_usd = sol_usd_at(tx["t"])
        threshold = DUST_SOL * sol_usd
        for owner, d, post, signed, sol, stable in tx["moves"]:
            e = get(owner)
            e["txs"] += 1
            e["signed"] += 1 if signed else 0
            if e["first"] is None:
                e["first"] = tx["t"]
            e["last"] = tx["t"]
            e["bal"] = post or 0.0           # chain order: a missing post balance means the account was closed
            if abs(d) < 1e-9:
                continue
            usd = sol * sol_usd + stable
            if d > 0 and usd < -threshold:
                e["buys"] += 1
                e["spentUsd"] -= usd
                e["spentSol"] -= sol
                e["tokBought"] += d
                if e["firstBuy"] is None:
                    e["firstBuy"] = tx["t"]
                if tx["slot"] == launch_slot:
                    e["launchBlock"] = True
            elif d < 0 and usd > threshold:
                e["sells"] += 1
                e["gotUsd"] += usd
                e["gotSol"] += sol
                e["tokSold"] -= d
            elif d > 0:
                e["tokIn"] += d
                if tx["slot"] == launch_slot:
                    e["launchBlock"] = True
            else:
                e["tokOut"] -= d
                if usd < 0:
                    e["spentUsd"] -= usd
                    e["spentSol"] -= sol
    busy = max(50, len(ordered) * 0.2)
    pools = [e for e in T.values() if e["owner"] in known_pools or e["owner"] == curve or (e["signed"] == 0 and e["txs"] > busy)]
    for p in pools:
        T.pop(p["owner"], None)
    traders = []
    for e in T.values():
        if not (e["buys"] or e["sells"] or e["tokIn"]):
            continue
        held = e["bal"] * price_usd
        realized = e["gotUsd"] - e["spentUsd"]
        traders.append({**e, "heldUsd": held, "realizedUsd": realized, "pnlUsd": realized + held, "realizedSol": e["gotSol"] - e["spentSol"],
                        "roi": (realized + held) / e["spentUsd"] * 100 if e["spentUsd"] > 1 else None,
                        "entryMinutes": (e["firstBuy"] - launch_time) / 60 if e["firstBuy"] is not None and launch_time is not None else None})
    traders.sort(key=lambda t: -t["pnlUsd"])
    pool_set = {p["owner"] for p in pools}
    sequence = []
    for tx in [t for t in ordered if t["slot"] == launch_slot][:12]:
        mv = [{"owner": o, "delta": d, "pool": o in pool_set} for o, d, *_ in tx["moves"] if abs(d) >= 1]
        if mv:
            sequence.append({"index": tx["idx"], "payer": tx["payer"], "moves": mv})
    return {"launch": {"time": launch_time, "slot": launch_slot, "creator": creator, "curve": curve},
            "pools": [{"owner": p["owner"], "txs": p["txs"]} for p in pools], "traders": traders, "sequence": sequence}


def find_distributors(recs, traders, pools):
    """Wallets that paid the fees to move tokens out of holders' wallets into 10+ other wallets (splitting a bag across fresh wallets)."""
    pool_set = {p["owner"] for p in pools}
    free = {t["owner"] for t in traders if t["tokIn"] > 0}
    payers = {}
    for tx in recs:
        payer = tx["payer"]
        recips = [(o, v) for o, v, *_ in tx["moves"] if v > 0 and o != payer and o in free and o not in pool_set]
        senders = [(o, v) for o, v, *_ in tx["moves"] if v < 0 and o not in pool_set]
        if not recips or not senders:
            continue
        p = payers.setdefault(payer, {"payer": payer, "txs": 0, "recipients": set(), "senders": {}, "tokens": 0.0, "first": tx["t"], "last": tx["t"]})
        p["txs"] += 1
        p["first"], p["last"] = min(p["first"], tx["t"]), max(p["last"], tx["t"])
        for o, v in recips:
            p["recipients"].add(o)
            p["tokens"] += v
        for o, v in senders:
            p["senders"][o] = p["senders"].get(o, 0.0) - v
    out = []
    for p in sorted(payers.values(), key=lambda p: -len(p["recipients"])):
        if len(p["recipients"]) >= 10:
            out.append({**p, "recipients": list(p["recipients"]), "senders": [{"owner": o, "tokens": t} for o, t in sorted(p["senders"].items(), key=lambda kv: -kv[1])]})
    return out


def find_same_size_clusters(traders, supply, width=1.05, min_size=10, density=2):
    """Groups of 10+ holders (each with at least 0.05% of supply) whose balances sit within a narrow band that is far more crowded than
    the bands just below and above it."""
    big = sorted((t for t in traders if t["bal"] >= supply * 0.0005), key=lambda t: t["bal"])
    bals = [t["bal"] for t in big]

    def count_in(lo, hi):
        return sum(1 for b in bals if lo <= b < hi)
    windows = []
    for i in range(len(big)):
        lo = bals[i]
        hi = lo * width
        j = i
        while j + 1 < len(big) and bals[j + 1] < hi:
            j += 1
        n = j - i + 1
        if n < min_size:
            continue
        below, above = count_in(lo / width, lo), count_in(hi, hi * width)
        if n >= density * max(below, above, 1):
            windows.append([i, j])
    clusters = []
    for i, j in windows:
        if clusters and i <= clusters[-1][1] + 1:
            clusters[-1][1] = max(clusters[-1][1], j)
        else:
            clusters.append([i, j])
    return [big[i:j + 1] for i, j in clusters]


def group_stats(members):
    s = lambda k: sum(t[k] for t in members)  # noqa: E731
    return {"count": len(members), "spentUsd": s("spentUsd"), "gotUsd": s("gotUsd"), "realizedUsd": s("realizedUsd"), "heldUsd": s("heldUsd"),
            "pnlUsd": s("pnlUsd"), "tokens": s("bal")}


def funder_of(data, wallet):
    """The account whose SOL paid for a wallet's first funded transaction."""
    sigs = solana.call(data, "getSignaturesForAddress", [wallet, {"limit": 1000}]) or []
    if not sigs:
        return {"funder": None, "walletTxs": 0, "ageHours": None}
    out = {"funder": None, "walletTxs": len(sigs), "ageHours": (time.time() - (sigs[-1].get("blockTime") or time.time())) / 3600, "historyCapped": len(sigs) == 1000}
    for s in list(reversed(sigs))[:4]:
        tx = solana.get_transaction(data, s["signature"])
        if not tx:
            continue
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        if wallet not in keys:
            continue
        i = keys.index(wallet)
        if (tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]) / 1e9 <= 0.01:
            continue
        best = None
        for j, k in enumerate(keys):
            d = (tx["meta"]["postBalances"][j] - tx["meta"]["preBalances"][j]) / 1e9
            if k != wallet and d < 0 and (best is None or d < best[1]):
                best = (k, d)
        out["funder"] = best[0] if best else None
        break
    return out


def pairs_for(mint):
    req = urllib.request.Request(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", headers={"User-Agent": "pumpgrad/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        pairs = [p for p in (json.loads(r.read()).get("pairs") or []) if p.get("chainId") == "solana" and (p.get("baseToken") or {}).get("address") == mint]
    return sorted(pairs, key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0))


class Cancelled(Exception):
    pass


def review_traders(data, mint, progress=lambda **k: None, cancel=None, funders_top=25):
    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled("Analysis cancelled.")
    notes = []
    progress(stage="Looking up the token")
    try:
        pairs = pairs_for(mint)
    except Exception as e:
        raise RuntimeError("DexScreener could not be reached (%s)." % type(e).__name__)
    if not pairs:
        raise RuntimeError("DexScreener has no Solana pair for this token.")
    main = pairs[0]
    try:
        price_usd = float(main.get("priceUsd") or 0)
    except ValueError:
        price_usd = 0.0
    progress(stage="Listing the token's transactions")
    sigs, truncated, _ = solana.get_signatures_all(data, mint, 0, MAX_TX, cancel)
    check()
    if truncated:
        raise RuntimeError(f"This token has more than {MAX_TX:,} transactions. A full analysis needs every trade since launch, which would take too long.")
    if not sigs:
        raise RuntimeError("No transactions found for this token.")
    recs = []
    pos = {s: len(sigs) - i for i, s in enumerate(sigs)}      # getTransaction has no position in the block: older = smaller
    failed = solana.get_transactions_stream(data, sigs, lambda s, tx: recs.append(compact_tx(tx, mint, pos.get(s, 0))), workers=4,
                                            progress=lambda d, t: progress(stage="Downloading trades", done=d, total=t), cancel=cancel)
    check()
    if not recs:
        raise RuntimeError(f"None of the token's {len(sigs):,} transactions could be read.")
    if len(sigs) - len(recs) - failed > 0 or failed:
        notes.append(f"{len(sigs) - len(recs):,} of {len(sigs):,} transactions couldn't be downloaded, so a few trades may be missing.")
    progress(stage="Working out profits")
    oldest = min(r["t"] for r in recs)
    at, now_price, _hist = solana.sol_prices(oldest)
    sup = solana.call(data, "getTokenSupply", [mint])
    supply = float(((sup or {}).get("value") or {}).get("uiAmountString") or 0)
    r = compute_traders(recs, mint, {p["pairAddress"] for p in pairs}, price_usd, at)
    by_owner = {t["owner"]: t for t in r["traders"]}
    groups = []

    def tag(owner, gid):
        t = by_owner.get(owner)
        if t is not None and gid not in t.setdefault("tags", []):
            t["tags"].append(gid)
    launch_set = {t["owner"] for t in r["traders"] if t["launchBlock"]}
    if r["launch"]["creator"] and r["launch"]["creator"] in by_owner:
        launch_set.add(r["launch"]["creator"])
    if launch_set:
        groups.append({"id": "launch", "kind": "launch", "label": "Launch block", "wallets": list(launch_set),
                       "note": "Bought or received tokens in the same block the token was created, usually the creator and a bundle."})
    for i, d in enumerate(find_distributors(recs, r["traders"], r["pools"])):
        groups.append({"id": f"dist{i + 1}", "kind": "distribution", "label": f"Split across {len(d['recipients'])} wallets", "distributor": d["payer"],
                       "note": f"{d['payer']} paid the fees to move {round(d['tokens']):,} tokens from {len(d['senders'])} holder wallets into {len(d['recipients'])} other wallets between "
                               f"{time.strftime('%m-%d %H:%M', time.gmtime(d['first']))} and {time.strftime('%m-%d %H:%M', time.gmtime(d['last']))} UTC. Splitting a bag across fresh wallets hides how concentrated ownership is.",
                       "wallets": list(dict.fromkeys([s["owner"] for s in d["senders"]] + d["recipients"]))})
    for i, c in enumerate(find_same_size_clusters(r["traders"], supply)):
        groups.append({"id": f"size{i + 1}", "kind": "cluster", "label": f"{len(c)} same-size holders",
                       "note": f"{len(c)} wallets each hold {round(c[0]['bal']):,}–{round(c[-1]['bal']):,} tokens ({sum(t['bal'] for t in c) / supply * 100 if supply else 0:.1f}% of supply together). "
                               "Near-identical bags usually mean one operator split a purchase across wallets.",
                       "wallets": [t["owner"] for t in c]})
    for g in groups:
        for o in g["wallets"]:
            tag(o, g["id"])
    outside = [t for t in r["traders"] if not t.get("tags")]
    to_check = list(dict.fromkeys([t["owner"] for t in outside[:funders_top]] + [t["owner"] for t in sorted(outside, key=lambda t: -t["bal"])[:funders_top]] +
                                  [o for g in groups if g["kind"] == "cluster" for o in g["wallets"][:5]]))
    funded = {}
    for i, o in enumerate(to_check):
        check()
        progress(stage="Checking who funded the top wallets", done=i, total=len(to_check))
        t = by_owner[o]
        try:
            t.update(funder_of(data, o))
        except (solana.RPCError, Exception):
            pass
        if t.get("funder"):
            funded.setdefault(t["funder"], []).append(o)
    fi = 0
    for funder, ws in sorted(funded.items(), key=lambda kv: -len(kv[1])):
        if len(ws) < 3:
            continue
        fi += 1
        gid = f"fund{fi}"
        linked = [g for g in groups if funder in g["wallets"] or g.get("distributor") == funder or any(w in g["wallets"] for w in ws)]
        groups.append({"id": gid, "kind": "funder", "label": f"Funded by {funder[:4]}…{funder[-4:]}", "funder": funder, "wallets": ws,
                       "note": f"{len(ws)} of the wallets checked got their first SOL from {funder}." + (f" Overlaps with: {', '.join(g['label'] for g in linked)}." if linked else "") +
                               " Only the top wallets were checked, so the real group may be bigger."})
        for o in ws:
            tag(o, gid)
    group_out = [{**g, **group_stats([by_owner[o] for o in g["wallets"] if o in by_owner])} for g in groups]
    outsiders = [t for t in r["traders"] if not t.get("tags")]
    return {"mint": mint, "symbol": (main.get("baseToken") or {}).get("symbol") or "", "name": (main.get("baseToken") or {}).get("name") or "", "pair": main.get("pairAddress"),
            "priceUsd": price_usd, "supply": supply, "liquidityUsd": (main.get("liquidity") or {}).get("usd") or 0, "generatedAt": int(time.time() * 1000), "notes": notes,
            "txCount": len(recs), "launch": {**r["launch"], "sequence": r["sequence"]}, "pools": r["pools"],
            "totals": {"traders": len(r["traders"]), "profitable": sum(1 for t in r["traders"] if t["pnlUsd"] > 0), "all": group_stats(r["traders"]),
                       "outsiders": {**group_stats(outsiders), "profitable": sum(1 for t in outsiders if t["pnlUsd"] > 0)}},
            "groups": sorted(group_out, key=lambda g: -g["realizedUsd"]), "traders": r["traders"][:300]}

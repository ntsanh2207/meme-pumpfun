"""Turn one parsed Solana transaction (getTransaction, encoding jsonParsed) into a wallet-level row: what the wallet gained
or lost and what kind of event it was. A port of Rug Radar's src/wallet/parse.js, shared by the wallet monitor and the
wallet review.

Row kinds: buy / sell (one coin against SOL or a stablecoin), tok_in / tok_out (a coin moved without a price), dispose
(a coin dumped for dust or burned), multi (several coins; swap_given prices a coin-to-coin swap), convert (SOL <-> stablecoin),
deposit / withdraw (SOL or stablecoins only), dust, other, settled (see link_deferred_proceeds).
"""
WSOL = "So11111111111111111111111111111111111111112"
STABLES = ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB")   # USDT
DUST_USD = 0.5
PUMP_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Programs that appear in plain transfers. Anything else (a DEX, an aggregator, a launchpad) means a trade.
BASIC_PROGRAMS = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "ComputeBudget111111111111111111111111111111",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDqGmVhJRs", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95",                                                # Lighthouse (wallet transaction guard)
    "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf", "SMPLecH534NA9acpos4G6x7uf3LWbCAwZQE9e8ZekMu",   # Squads multisig
}


def _key(k):
    return k if isinstance(k, str) else k.get("pubkey")


def _amount(b):
    t = b.get("uiTokenAmount") or {}
    v = t.get("uiAmountString")
    if v is None:
        v = t.get("uiAmount")
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _instructions(tx):
    msg, meta = tx["transaction"]["message"], tx["meta"]
    inner = [ix for g in (meta.get("innerInstructions") or []) for ix in g.get("instructions", [])]
    return list(msg.get("instructions", [])) + inner


def parse_tx(tx, wallet, sol_usd):
    """sol_usd(t) returns the SOL/USD price at unix time t."""
    m, msg = tx["meta"], tx["transaction"]["message"]
    keys = [_key(k) for k in msg["accountKeys"]]
    wi = keys.index(wallet) if wallet in keys else -1
    sol = (m["postBalances"][wi] - m["preBalances"][wi]) / 1e9 if wi >= 0 else 0.0
    price = sol_usd(tx.get("blockTime"))

    tok = {}
    for b in m.get("preTokenBalances") or []:
        if b.get("owner") == wallet:
            tok[b["mint"]] = tok.get(b["mint"], 0.0) - _amount(b)
    for b in m.get("postTokenBalances") or []:
        if b.get("owner") == wallet:
            tok[b["mint"]] = tok.get(b["mint"], 0.0) + _amount(b)

    sol += tok.pop(WSOL, 0.0)
    stable = 0.0
    for s in STABLES:
        stable += tok.pop(s, 0.0)
    memes = {k: v for k, v in tok.items() if abs(v) > 1e-9}
    quote = sol * price + stable

    # counterparties of plain SOL and token transfers (token accounts mapped back to their owners)
    owners = {}
    for b in (m.get("preTokenBalances") or []) + (m.get("postTokenBalances") or []):
        owners[keys[b["accountIndex"]]] = b.get("owner")
    cps, token_sent_out = [], False
    ixs = _instructions(tx)
    for ix in ixs:
        p = ix.get("parsed")
        if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked"):
            continue
        inf = p.get("info") or {}
        sys = ix.get("program") == "system"
        src = inf.get("source") if sys else owners.get(inf.get("source"), inf.get("source"))
        dst = inf.get("destination") if sys else owners.get(inf.get("destination"), inf.get("destination"))
        if dst == wallet and src and src != wallet and src not in cps:
            cps.append(src)
        if src == wallet and dst and dst != wallet:
            if dst not in cps:
                cps.append(dst)
            if not sys:
                token_sent_out = True
    all_programs = {ix.get("programId") for ix in ixs}
    basic_only = all(p in BASIC_PROGRAMS for p in all_programs)
    plain_transfer = token_sent_out and basic_only

    vals = list(memes.values())
    kind = "other"
    if len(vals) == 1:
        amt = vals[0]
        if amt > 0:
            kind = "buy" if quote < -DUST_USD else "tok_in" if abs(quote) <= DUST_USD else "other"
        else:
            kind = "tok_out" if plain_transfer else "sell" if quote > DUST_USD and not basic_only else "dispose"
    elif not vals:
        sol_leg_usd = sol * price
        if abs(sol_leg_usd) > DUST_USD and abs(stable) > DUST_USD and (sol > 0) != (stable > 0):
            kind = "convert"
        elif abs(quote) > DUST_USD:
            kind = "deposit" if quote > 0 else "withdraw"
        else:
            kind = "dust"
    else:
        kind = "multi"

    # coin-to-coin swaps are usually routed sales: coin A is sold and coin B bought, the proceeds never touching the wallet
    swap_given = None
    if kind == "multi":
        prices = route_prices(tx, price, wallet)
        given = [(k, v) for k, v in memes.items() if v < 0]
        got = [(k, v) for k, v in memes.items() if v > 0]
        if given and got:
            if all(k in prices for k, _ in given):
                swap_given = {k: -v * prices[k] for k, v in given}
            elif len(given) == 1 and all(k in prices for k, _ in got):
                swap_given = {given[0][0]: sum(v * prices[k] for k, v in got)}

    # deferred proceeds (intent-style routers such as DFlow): see link_deferred_proceeds
    delta = {}
    for b in m.get("preTokenBalances") or []:
        delta[keys[b["accountIndex"]]] = {"owner": b.get("owner"), "mint": b["mint"], "pre": _amount(b), "post": 0.0}
    for b in m.get("postTokenBalances") or []:
        a = keys[b["accountIndex"]]
        d = delta.get(a) or {"owner": b.get("owner"), "mint": b["mint"], "pre": 0.0, "post": 0.0, "fresh": True}
        d["post"] = _amount(b)
        delta[a] = d
    payer = keys[0] == wallet
    paid_for = {(ix.get("parsed") or {}).get("info", {}).get("account") for ix in ixs
                if ix.get("program") == "spl-associated-token-account" and isinstance(ix.get("parsed"), dict)
                and (ix["parsed"].get("info") or {}).get("source") == wallet and (ix["parsed"].get("info") or {}).get("wallet") != wallet}
    unpaid_sale = payer and not plain_transfer and not basic_only and quote <= DUST_USD and any(v < 0 for v in vals)
    escrow_out = [{"account": a, "mint": d["mint"], "amount": d["post"] - d["pre"]} for a, d in delta.items()
                  if unpaid_sale and (d.get("fresh") or a in paid_for) and d["owner"] != wallet and d["post"] > d["pre"]
                  and not memes.get(d["mint"], 0) < 0]
    escrow_in = [{"account": a, "mint": d["mint"], "amount": d["pre"] - d["post"]} for a, d in delta.items()
                 if (not payer and quote > DUST_USD and not any(v < 0 for v in vals)) and d["owner"] != wallet and d["post"] < d["pre"]]

    pump_mints = [mint for mint in memes
                  if any(ix.get("programId") in (PUMP_CURVE, PUMP_AMM) and mint in (ix.get("accounts") or []) for ix in ixs)]
    row = {"t": tx.get("blockTime"), "sig": tx["transaction"]["signatures"][0], "kind": kind, "sol": sol, "stable": stable,
           "quote": quote, "memes": memes, "plainTransfer": plain_transfer, "basicOnly": basic_only, "swapGiven": swap_given,
           "payer": payer, "cps": cps, "programs": list(dict.fromkeys(ix.get("programId") for ix in msg.get("instructions", []))),
           "pumpMints": pump_mints}
    if escrow_out:
        row["escrowOut"] = escrow_out
    if escrow_in:
        row["escrowIn"] = escrow_in
    return row


def link_deferred_proceeds(rows, window=900):
    """Joins deferred sale proceeds to their sale (rows in chain order): a row paying the wallet without its signature that
    takes exactly what the wallet's own unpaid sale put into an escrow account (same account, token and amount) within 15
    minutes is that sale's proceeds. Returns how many were joined."""
    funded, joined = {}, 0

    def same(a, b):
        return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b)))
    for r in rows:
        for e in r.get("escrowOut") or []:
            funded.setdefault(e["account"], []).append({"sale": r, "mint": e["mint"], "amount": e["amount"]})
        if not r.get("escrowIn"):
            continue
        sale = None
        for e in r["escrowIn"]:
            lst = funded.get(e["account"], [])
            for i, x in enumerate(lst):
                if (not x["sale"].get("deferredProceeds") and x["mint"] == e["mint"] and same(x["amount"], e["amount"])
                        and x["sale"]["t"] <= r["t"] and r["t"] - x["sale"]["t"] <= window):
                    sale = x["sale"]
                    del lst[i]
                    break
            if sale:
                break
        if not sale:
            continue
        sale["sol"] += r["sol"]
        sale["stable"] += r["stable"]
        sale["quote"] += r["quote"]
        sale["deferredProceeds"] = (sale.get("deferredProceeds") or []) + [r["sig"]]
        coins = list(sale["memes"].values())
        if sale["kind"] == "dispose" and len(coins) == 1 and coins[0] < 0 and sale["quote"] > DUST_USD:
            sale["kind"] = "sell"
        r["settles"], r["kind"], r["sol"], r["stable"], r["quote"] = sale["sig"], "settled", 0.0, 0.0, 0.0
        joined += 1
    return joined


def route_prices(tx, sol_price, wallet):
    """USD price of every token whose price can be chained, hop by hop, from SOL or a stablecoin inside this transaction.
    A hop is a transfer of token X into a pool followed shortly by a transfer of a different token Y out of that same pool;
    the two legs have equal value."""
    m, msg = tx["meta"], tx["transaction"]["message"]
    keys = [_key(k) for k in msg["accountKeys"]]
    acct = {}
    for b in (m.get("preTokenBalances") or []) + (m.get("postTokenBalances") or []):
        acct[keys[b["accountIndex"]]] = {"mint": b["mint"], "owner": b.get("owner"), "decimals": (b.get("uiTokenAmount") or {}).get("decimals")}
    legs = []
    for ix in _instructions(tx):
        p = ix.get("parsed")
        if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked"):
            continue
        i = p.get("info") or {}
        if ix.get("program") == "system":
            if (i.get("lamports") or 0) > 0:
                legs.append({"mint": WSOL, "amount": i["lamports"] / 1e9, "from": i.get("source"), "to": i.get("destination")})
            continue
        s, d = acct.get(i.get("source")), acct.get(i.get("destination"))
        mint = i.get("mint") or (s or {}).get("mint") or (d or {}).get("mint")
        decimals = (i.get("tokenAmount") or {}).get("decimals")
        if decimals is None:
            decimals = (s or {}).get("decimals")
        if decimals is None:
            decimals = (d or {}).get("decimals")
        amount = None
        try:
            if i.get("tokenAmount"):
                ta = i["tokenAmount"]
                amount = float(ta.get("uiAmountString") if ta.get("uiAmountString") is not None else ta.get("uiAmount"))
            elif decimals is not None:
                amount = float(i.get("amount")) / 10 ** decimals
        except (TypeError, ValueError):
            amount = None
        if not mint or not (amount and amount > 0):
            continue
        legs.append({"mint": mint, "amount": amount, "from": (s or {}).get("owner") or i.get("source"),
                     "to": (d or {}).get("owner") or i.get("destination")})

    hops, routers = [], {keys[0], wallet}

    def scan(accept):
        for a in range(len(legs)):
            for b in range(a + 1, min(len(legs), a + 4)):
                x, y = legs[a], legs[b]
                if x["mint"] != y["mint"] and x["to"] == y["from"] and x["from"] != x["to"] and y["to"] != x["to"] and accept(x, y):
                    hops.append((x, y))
                    break
    scan(lambda x, y: x["from"] == y["to"])
    for x, _ in hops:
        routers.add(x["from"])
    scan(lambda x, y: x["from"] != y["to"] and x["to"] not in routers)

    price = {WSOL: sol_price, **{s: 1.0 for s in STABLES}}
    for _ in range(10):
        changed = False
        for x, y in hops:
            if x["mint"] in price and y["mint"] not in price:
                price[y["mint"]] = price[x["mint"]] * x["amount"] / y["amount"]
                changed = True
            elif y["mint"] in price and x["mint"] not in price:
                price[x["mint"]] = price[y["mint"]] * y["amount"] / x["amount"]
                changed = True
        if not changed:
            break
    return price


def pre_sol(tx, wallet):
    """The wallet's SOL balance before this transaction, if the wallet is in it."""
    keys = [_key(k) for k in tx["transaction"]["message"]["accountKeys"]]
    return tx["meta"]["preBalances"][keys.index(wallet)] / 1e9 if wallet in keys else None


def quote_accounts(tx, wallet):
    """Token accounts the wallet owns for SOL or stablecoins. Transfers into these skip the owner's signature list."""
    keys = [_key(k) for k in tx["transaction"]["message"]["accountKeys"]]
    out = set()
    for b in (tx["meta"].get("preTokenBalances") or []) + (tx["meta"].get("postTokenBalances") or []):
        if b.get("owner") == wallet and (b["mint"] == WSOL or b["mint"] in STABLES):
            out.add(keys[b["accountIndex"]])
    return out

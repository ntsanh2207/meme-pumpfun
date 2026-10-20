"""Coin review for the Coins page: DexScreener + GeckoTerminal + RugCheck, scored by fixed thresholds, plus a Jupiter
tradability check. A port of Rug Radar's src/sources.js, analyze.js, prices.js, tradability.js and review.js. Result keys keep
Rug Radar's names (camelCase) so the page code could be ported as it was.
"""
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUG = "https://api.rugcheck.xyz/v1"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
JUP = "https://lite-api.jup.ag/swap/v1"
WSOL = "So11111111111111111111111111111111111111112"
BASE58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
TIMEOUT = 12


class LookupFailed(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


_cache, _cache_lock = {}, threading.Lock()


def get_json(url, ttl=20.0, timeout=TIMEOUT):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    host = url.split("/")[2]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "pumpgrad/1.0"}), timeout=timeout) as r:
            raw = r.read(16 << 20)
            size = len(raw)
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise LookupFailed(f"{host} is rate-limiting requests. Wait a minute and try again.", 429)
        raise LookupFailed(f"{host} returned HTTP {e.code}.", 404 if e.code == 404 else 502)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LookupFailed(f"{host} didn't respond ({type(e).__name__}).", 502)
    with _cache_lock:
        if size < (1 << 20):                 # big answers (a long RugCheck report) are not kept: the web service has a small memory limit
            _cache[url] = (now, data)
            for k in [k for k, v in _cache.items() if now - v[0] > 600]:
                del _cache[k]
            while len(_cache) > 120:
                _cache.pop(next(iter(_cache)))
    return data


def solana_pairs_of(pairs):
    return [p for p in (pairs or []) if isinstance(p, dict) and p.get("chainId") == "solana"]


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def card(p):
    return {"mint": p["baseToken"]["address"], "symbol": p["baseToken"].get("symbol"), "name": p["baseToken"].get("name"),
            "image": (p.get("info") or {}).get("imageUrl"), "priceUsd": num(p.get("priceUsd")) or None,
            "marketCap": p.get("marketCap") or p.get("fdv") or None, "liquidity": (p.get("liquidity") or {}).get("usd") or None,
            "change24h": (p.get("priceChange") or {}).get("h24"), "createdAt": p.get("pairCreatedAt"), "dexId": p.get("dexId")}


def resolve(q):
    """Whatever the user typed -> {"mint"} or {"candidates"}."""
    q = str(q or "").strip()
    if not q:
        raise LookupFailed("Enter a mint address, pair address, DexScreener link or ticker.")
    m = (re.search(r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})", q, re.I)
         or re.search(r"(?:solscan\.io/token|rugcheck\.xyz/tokens|pump\.fun/coin|birdeye\.so/token)/([1-9A-HJ-NP-Za-km-z]{32,44})", q, re.I))
    ident = m.group(1) if m else (q if BASE58.fullmatch(q) else None)
    if ident:
        try:
            data = get_json(f"{DEX}/latest/dex/tokens/{ident}")
        except LookupFailed as e:
            if e.status == 404:
                raise
            return {"mint": ident}      # DexScreener down: review the address as a mint and let GeckoTerminal supply the pools
        if [p for p in solana_pairs_of(data.get("pairs")) if (p.get("baseToken") or {}).get("address") == ident]:
            return {"mint": ident}
        try:
            by_pair = get_json(f"{DEX}/latest/dex/pairs/solana/{ident}")
        except LookupFailed:
            by_pair = None
        pair = (by_pair or {}).get("pair") or next(iter((by_pair or {}).get("pairs") or []), None)
        if pair and BASE58.fullmatch(str((pair.get("baseToken") or {}).get("address", ""))):
            return {"mint": pair["baseToken"]["address"]}
        raise LookupFailed(f"No Solana trading pair found for {ident[:4]}…{ident[-4:]}. Check the address, or the token may not be trading yet.", 404)
    found = solana_pairs_of(get_json(f"{DEX}/latest/dex/search?q=" + urllib.parse.quote(q[1:] if q.startswith("$") else q)).get("pairs"))
    by_mint = {}
    for p in found:
        mint = (p.get("baseToken") or {}).get("address")
        if not mint or mint == WSOL:
            continue
        e = by_mint.setdefault(mint, {"best": p, "volume": 0})
        e["volume"] += (p.get("volume") or {}).get("h24") or 0
        if ((p.get("liquidity") or {}).get("usd") or 0) > ((e["best"].get("liquidity") or {}).get("usd") or 0):
            e["best"] = p
    cands = [{**card(e["best"]), "volume24h": e["volume"]} for e in sorted(by_mint.values(), key=lambda e: -e["volume"])[:12]]
    if not cands:
        raise LookupFailed(f'No Solana tokens match "{q}".', 404)
    return {"candidates": cands}


# GeckoTerminal's free API allows about 30 calls a minute: reviews use at most 20, and a 429 pauses it for a minute.
_gecko = {"calls": [], "paused": 0.0}
GECKO_DEX = {"pump-fun": "pumpfun", "raydium-launchlab": "launchlab", "letsbonk-fun": "launchlab", "boop-fun": "boop", "moonit": "moonshot"}


def gecko_allow():
    now = time.time()
    if now < _gecko["paused"]:
        return False
    _gecko["calls"] = [t for t in _gecko["calls"] if now - t < 60]
    if len(_gecko["calls"]) >= 20:
        return False
    _gecko["calls"].append(now)
    return True


def gecko_pairs(js, mint):
    """GeckoTerminal pools where `mint` is the base token, shaped like DexScreener pairs."""
    out = []
    tokens = {x["id"]: x.get("attributes") or {} for x in (js or {}).get("included", []) if isinstance(x, dict) and x.get("type") == "token"}
    for pool in (js or {}).get("data", []):
        a, r = pool.get("attributes") or {}, pool.get("relationships") or {}
        if not BASE58.fullmatch(str(a.get("address", ""))) or ((r.get("base_token") or {}).get("data") or {}).get("id") != "solana_" + mint:
            continue
        quote_key = str(((r.get("quote_token") or {}).get("data") or {}).get("id") or "")
        quote_id = re.sub(r"^solana_", "", quote_key)
        bm, qm = tokens.get("solana_" + mint, {}), tokens.get(quote_key, {})
        dex_id = str(((r.get("dex") or {}).get("data") or {}).get("id") or "unknown")
        price, liq = num(a.get("base_token_price_usd")), num(a.get("reserve_in_usd")) or 0

        def tx(k):
            t = (a.get("transactions") or {}).get(k) or {}
            return {"buys": int(num(t.get("buys")) or 0), "sells": int(num(t.get("sells")) or 0), "buyers": int(num(t.get("buyers")) or 0), "sellers": int(num(t.get("sellers")) or 0)}

        def vol(k):
            return num((a.get("volume_usd") or {}).get(k)) or 0

        def ch(k):
            return num((a.get("price_change_percentage") or {}).get(k))
        created = None
        try:
            from datetime import datetime
            created = int(datetime.fromisoformat(str(a.get("pool_created_at")).replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, TypeError):
            pass
        p = {"source": "geckoterminal", "chainId": "solana", "dexId": GECKO_DEX.get(dex_id, dex_id), "pairAddress": a["address"],
             "url": f"https://www.geckoterminal.com/solana/pools/{a['address']}",
             "baseToken": {"address": mint, "symbol": str(bm.get("symbol") or ""), "name": str(bm.get("name") or bm.get("symbol") or "")},
             "quoteToken": {"address": quote_id, "symbol": str(qm.get("symbol") or "")},
             "liquidity": {"usd": liq, "base": liq / 2 / price if price and price > 0 else 0},
             "fdv": num(a.get("fdv_usd")), "marketCap": num(a.get("market_cap_usd")),
             "volume": {k: vol(k) for k in ("m5", "h1", "h6", "h24")}, "txns": {k: tx(k) for k in ("m5", "h1", "h6", "h24")},
             "priceChange": {k: ch(k) for k in ("m5", "h1", "h6", "h24")}}
        if price is not None:
            p["priceUsd"] = str(price)
        if created:
            p["pairCreatedAt"] = created
        out.append(p)
    return out


def merge_pairs(dex_pairs, gecko_list):
    """DexScreener pairs plus GeckoTerminal pools. A pool both list keeps DexScreener's entry unless it has no liquidity figure."""
    by_addr = {p["pairAddress"]: p for p in gecko_list}
    out = []
    for p in dex_pairs:
        g = by_addr.pop(p.get("pairAddress"), None)
        if g and not ((p.get("liquidity") or {}).get("usd") or 0) > 0 and g["liquidity"]["usd"] > 0:
            out.append({**g, "info": p.get("info"), "baseToken": {**g["baseToken"], **p["baseToken"]}, "dexId": p.get("dexId") or g["dexId"]})
        else:
            out.append({**p, "source": "dexscreener"})
    return out + list(by_addr.values())


def fetch_gecko(mint):
    url = f"{GECKO}/networks/solana/tokens/{mint}/pools?include=base_token,quote_token,dex"
    with _cache_lock:
        hit = _cache.get(url)
    stale = gecko_pairs(hit[1], mint) if hit and time.time() - hit[0] < 300 else None
    if not (hit and time.time() - hit[0] < 60) and not gecko_allow():
        if stale is not None:
            return stale
        raise LookupFailed("GeckoTerminal was skipped to stay under its rate limit.", 429)
    try:
        return gecko_pairs(get_json(url, 60), mint)
    except LookupFailed as e:
        if e.status == 429:
            _gecko["paused"] = time.time() + 60
        if stale is not None:
            return stale
        raise


def fetch_token(mint):
    results = {}

    def run(key, fn):
        try:
            results[key] = ("ok", fn())
        except LookupFailed as e:
            results[key] = ("err", e)
        except Exception as e:
            results[key] = ("err", LookupFailed(f"unexpected answer ({type(e).__name__})", 502))
    threads = [threading.Thread(target=run, args=(k, f)) for k, f in (
        ("dex", lambda: get_json(f"{DEX}/latest/dex/tokens/{mint}")), ("rug", lambda: get_json(f"{RUG}/tokens/{mint}/report", 30)), ("gecko", lambda: fetch_gecko(mint)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)
    dex, rug, gk = (results.get(k, ("err", LookupFailed("timed out", 502))) for k in ("dex", "rug", "gecko"))
    dex_pairs = [p for p in solana_pairs_of(dex[1].get("pairs")) if (p.get("baseToken") or {}).get("address") == mint] if dex[0] == "ok" else []
    if dex[0] == "err" and not (gk[0] == "ok" and gk[1]):
        raise dex[1]
    return {"dex": {"pairs": merge_pairs(dex_pairs, gk[1] if gk[0] == "ok" else [])}, "rug": rug[1] if rug[0] == "ok" else None,
            "rugError": str(rug[1]) if rug[0] == "err" else None, "geckoError": str(gk[1]) if gk[0] == "err" else None, "fetchedAt": int(time.time() * 1000)}


# ---- Jupiter: reference price and tradability ---------------------------------------------------------------------------

_jup = {"prices": {}, "calls": [], "paused": 0.0, "bad": {}, "checks": [], "tpaused": 0.0, "tcache": {}}


def jupiter_prices(mints):
    """{mint: price} for the coins Jupiter prices right now (each used for 30 s; at most 10 requests a minute)."""
    now = time.time()
    want = list(dict.fromkeys(mints))
    need = [m for m in want if now - _jup["prices"].get(m, (0, 0))[0] >= 30]
    for i in range(0, len(need), 50):
        _jup["calls"] = [t for t in _jup["calls"] if now - t < 60]
        if now < _jup["paused"] or len(_jup["calls"]) >= 10:
            break
        _jup["calls"].append(now)
        batch = need[i:i + 50]
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{JUP_PRICE}?ids={','.join(batch)}", headers={"User-Agent": "pumpgrad/1.0"}), timeout=8) as r:
                data = json.loads(r.read()) or {}
            for m in batch:
                _jup["prices"][m] = (time.time(), num((data.get(m) or {}).get("usdPrice")) or 0)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _jup["paused"] = time.time() + 30
                break
        except (urllib.error.URLError, OSError, ValueError):
            pass
    now = time.time()
    return {m: _jup["prices"][m][1] for m in want if m in _jup["prices"] and _jup["prices"][m][1] > 0 and now - _jup["prices"][m][0] < 30}


def near_ref(price, ref, ratio=2.0):
    if not ref > 0:
        return True
    p = num(price)
    return bool(p and p > 0 and ref / ratio <= p <= ref * ratio)


def pool_ok(p, ref, remember=True):
    addr, price = p.get("pairAddress"), num(p.get("priceUsd"))
    if not (ref and ref > 0):
        return not (addr and _jup["bad"].get(addr, 0) > time.time())
    if not (price and price > 0):
        return True
    if near_ref(price, ref):
        if remember and addr:
            _jup["bad"].pop(addr, None)
        return True
    if remember:
        mark_bad(addr)
    return False


def mark_bad(addr):
    if addr:
        _jup["bad"][addr] = time.time() + 1800


SIZE_SOL = 1
NO_ROUTE = re.compile(r"COULD_NOT_FIND_ANY_ROUTE|NO_ROUTES_FOUND|TOKEN_NOT_TRADABLE|not tradable|no route", re.I)


def _quote(inp, out, amount):
    url = f"{JUP}/quote?inputMint={inp}&outputMint={out}&amount={amount}&slippageBps=100&swapMode=ExactIn"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pumpgrad/1.0"}), timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except ValueError:
            data = {}
        if e.code == 429:
            raise LookupFailed("Jupiter is rate-limiting requests.", 429)
        if NO_ROUTE.search(f"{data.get('errorCode', '')} {data.get('error', '')}"):
            return None
        raise LookupFailed(f"Jupiter: {data['error']}" if data.get("error") else f"Jupiter returned HTTP {e.code}.", 502)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LookupFailed(f"Jupiter didn't answer ({type(e).__name__}).", 502)
    if data.get("inputMint") != inp or data.get("outputMint") != out or str(data.get("inAmount")) != str(amount) or not int(data.get("outAmount") or 0) > 0:
        raise LookupFailed("Jupiter returned a quote for something else.", 502)
    return {"amount": int(data["outAmount"]), "impactPct": abs(num(data.get("priceImpactPct")) or 0) * 100,
            "route": [s["swapInfo"]["label"] for s in data.get("routePlan", []) if (s.get("swapInfo") or {}).get("label")]}


def tradability(mint, priority=False):
    """What trading the coin through Jupiter really costs: quote a buy of 1 SOL, then the sale of those tokens. Nothing is signed."""
    now = time.time()
    hit = _jup["tcache"].get(mint)
    if hit and now - hit[0] < 30:
        return hit[1]
    if now < _jup["tpaused"]:
        return {"sizeSol": SIZE_SOL, "error": "Jupiter is busy. Try again in a minute."}
    _jup["checks"] = [t for t in _jup["checks"] if now - t < 60]
    if len(_jup["checks"]) >= 5 and not priority:
        return {"sizeSol": SIZE_SOL, "error": "Too many checks this minute. Try again shortly."}
    _jup["checks"].append(now)
    try:
        lamports = int(SIZE_SOL * 1e9)
        buy = _quote(WSOL, mint, lamports)
        if not buy:
            result = {"sizeSol": SIZE_SOL, "noRoute": True}
        else:
            sell = _quote(mint, WSOL, buy["amount"])
            result = {"sizeSol": SIZE_SOL, "buy": {"tokensRaw": str(buy["amount"]), "impactPct": buy["impactPct"], "route": buy["route"]},
                      "sell": {"outSol": sell["amount"] / 1e9, "impactPct": sell["impactPct"], "route": sell["route"]} if sell else None,
                      "roundTripLossPct": max(0, (1 - sell["amount"] / lamports) * 100) if sell else 100}
    except LookupFailed as e:
        if e.status == 429:
            _jup["tpaused"] = time.time() + 30
        result = {"sizeSol": SIZE_SOL, "error": str(e)}
        _jup["tcache"][mint] = (time.time() - 20, result)
        return result
    _jup["tcache"][mint] = (time.time(), result)
    if len(_jup["tcache"]) > 200:
        _jup["tcache"].pop(next(iter(_jup["tcache"])))
    return result


# ---- scoring ------------------------------------------------------------------------------------------------------------

SEV_WEIGHT = {"crit": 2, "warn": 1, "ok": 0, "info": 0}
SEV_ORDER = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
LAUNCHPAD_URL = re.compile(r"pump\.fun|agencypad|/coin/|bonk\.fun|letsbonk|moonshot|believe\.app|bags\.fm", re.I)
BONDING_CURVE_DEXES = {"pumpfun", "moonshot", "launchlab", "boop", "meteora-dbc"}
NO_LP_TOKEN = re.compile(r"orca|whirlpool|dlmm|clmm|manifest|phoenix|openbook|serum", re.I)
COVERED_RISKS = re.compile(r"mutable metadata|mint authority|freeze authority|transfer fee|lp unlocked|lp locked", re.I)


def _fixed(n, d):
    return f"{n:.{d}f}"


def usd(n):
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "—"
    a = abs(n)
    if a >= 1e9:
        return f"${n / 1e9:.2f}B"
    if a >= 1e6:
        return f"${n / 1e6:.2f}M"
    if a >= 1e4:
        return "$" + re.sub(r"\.0$", "", f"{n / 1e3:.1f}") + "K"
    if a >= 1:
        return "$" + f"{n:,.2f}".rstrip("0").rstrip(".")
    if a == 0:
        return "$0"
    s = f"{n:.4g}"
    if "e" in s:
        s = f"{float(s):.12f}".rstrip("0")
    return "$" + s


def pct(n, d=1):
    return "—" if n is None or (isinstance(n, float) and math.isnan(n)) else ("+" if n >= 0 else "") + f"{n:.{d}f}%"


def plain(n, d=1):
    return "—" if n is None or (isinstance(n, float) and math.isnan(n)) else f"{n:.{d}f}%"


def integer(n):
    return "—" if n is None or (isinstance(n, float) and math.isnan(n)) else f"{math.floor(n + 0.5):,}"


def short(a):
    return a[:4] + "…" + a[-4:] if a else ""


def age_str(ms):
    if not ms or ms <= 0:
        return "—"
    m = int(ms // 60000)
    h, d = m // 60, m // 1440
    return f"{d}d {h % 24}h" if d >= 1 else f"{h}h {m % 60}m" if h >= 1 else f"{m}m"


def trusted(x):
    if x.get("source") != "geckoterminal":
        return True
    t = (x.get("txns") or {}).get("h24") or {}
    return (t.get("buyers", 0) + t.get("sellers", 0)) >= 20 and ((x.get("volume") or {}).get("h24") or 0) >= 0.1 * ((x.get("liquidity") or {}).get("usd") or 0)


def analyze(dex, rug, fetched_at=None, trade=None, ref_price=None):
    fetched_at = fetched_at or int(time.time() * 1000)
    listed = sorted(solana_pairs_of(dex.get("pairs")), key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0))
    if not listed:
        raise LookupFailed("No Solana trading pair found for this token.")
    first_listed = min([x.get("pairCreatedAt") or math.inf for x in listed])
    young = math.isfinite(first_listed) and fetched_at - first_listed < 2 * 3.6e6
    near = listed if young else [x for x in listed if pool_ok(x, ref_price, remember=False)]
    would_win = next((x for x in near if trusted(x)), near[0] if near else None)
    if near and len(near) < len(listed) and would_win["dexId"] in BONDING_CURVE_DEXES and any(x["dexId"] not in BONDING_CURVE_DEXES for x in listed):
        near = listed
    all_pairs = near or listed
    ignored = [x for x in listed if x not in near] if near else []
    if ref_price and ref_price > 0:
        for x in ignored:
            mark_bad(x.get("pairAddress"))
    p = next((x for x in all_pairs if trusted(x)), all_pairs[0])
    mint = p["baseToken"]["address"]
    pairs = [x for x in all_pairs if (x.get("baseToken") or {}).get("address") == mint]
    every_pair = [x for x in listed if (x.get("baseToken") or {}).get("address") == mint]
    meta = next((x for x in pairs if x.get("source") != "geckoterminal"), p)
    dex_best = next((x for x in pairs if x.get("source") != "geckoterminal"), None)
    has_rug = isinstance(rug, dict) and "mintAuthority" in rug
    if has_rug and rug.get("mint") and rug["mint"] != mint:
        raise LookupFailed(f"DexScreener and RugCheck returned different tokens ({short(mint)} vs {short(rug['mint'])}).")
    R = rug if has_rug else {}

    dec = (R.get("token") or {}).get("decimals", 6)
    if dec is None:
        dec = 6
    supply_raw = (R.get("token") or {}).get("supply") or 0
    supply = supply_raw / 10 ** dec
    price = num(p.get("priceUsd")) or 0
    liq = (p.get("liquidity") or {}).get("usd") or 0 if trusted(p) else 0
    liq_base = (p.get("liquidity") or {}).get("base") or 0 if trusted(p) else 0
    dex_liq_sum = sum(((x.get("liquidity") or {}).get("usd") or 0) for x in pairs if trusted(x))
    ignored_liq = sum(((x.get("liquidity") or {}).get("usd") or 0) for x in ignored)
    total_liq = max((R.get("totalMarketLiquidity") or 0) - ignored_liq, dex_liq_sum, liq)
    dex_price = num((dex_best or {}).get("priceUsd")) or 0
    mcap = (p.get("marketCap") or (dex_best["marketCap"] / dex_price * price if p.get("source") == "geckoterminal" and dex_best and dex_best.get("marketCap") and dex_price > 0 else 0)
            or p.get("fdv") or 0)
    created = min([x.get("pairCreatedAt") or math.inf for x in every_pair])
    age_ms = fetched_at - created if math.isfinite(created) else None
    age_h = age_ms / 3.6e6 if age_ms is not None else None
    tx, vol, ch = p.get("txns") or {}, p.get("volume") or {}, p.get("priceChange") or {}
    vol24 = sum(((x.get("volume") or {}).get("h24") or 0) for x in pairs)
    tx24 = sum((((x.get("txns") or {}).get("h24") or {}).get("buys") or 0) + (((x.get("txns") or {}).get("h24") or {}).get("sells") or 0) for x in pairs)
    on_curve = p.get("dexId") in BONDING_CURVE_DEXES

    markets = R.get("markets") or []
    pool_keys = {m.get("pubkey") for m in markets} | {x.get("pairAddress") for x in every_pair}
    holders = [{"owner": h.get("owner"), "pct": h.get("pct") or 0, "insider": bool(h.get("insider")), "pool": h.get("owner") in pool_keys, "cluster": False}
               for h in (R.get("topHolders") or [])]
    wallets = [h for h in holders if not h["pool"]]
    top10 = sum(h["pct"] for h in wallets[:10])
    top1 = wallets[0]["pct"] if wallets else 0

    big = sorted(h["pct"] for h in wallets if h["pct"] >= 0.5)
    cluster, i = {"n": 0, "lo": 0, "hi": 0}, 0
    for j in range(len(big)):
        while big[j] > big[i] * 1.15:
            i += 1
        if j - i + 1 > cluster["n"]:
            cluster = {"n": j - i + 1, "lo": big[i], "hi": big[j]}
    if cluster["n"] >= 5:
        for h in wallets:
            h["cluster"] = cluster["lo"] <= h["pct"] <= cluster["hi"]

    nets = R.get("insiderNetworks") or []
    ins_raw = sum(n.get("currentHolding") or 0 for n in nets)
    ins_recv_raw = sum(n.get("tokenAmount") or 0 for n in nets)
    ins_wallets = sum(n.get("activeAccounts") or n.get("size") or 0 for n in nets)
    ins_pct = ins_raw / supply_raw * 100 if supply_raw else 0
    ins_recv_pct = ins_recv_raw / supply_raw * 100 if supply_raw else 0
    ins_tokens = ins_raw / 10 ** dec
    ins_usd = ins_tokens * price

    def dump_into(base):
        return ((base / (base + ins_tokens)) ** 2 - 1) * 100 if base > 0 and ins_tokens > 0 else 0
    dump_main = dump_into(liq_base)
    dump_all = dump_into(liq_base * (total_liq / liq) if liq > 0 else liq_base)

    checks = []
    if has_rug:
        main_market = next((m for m in markets if m.get("pubkey") == p.get("pairAddress")), markets[0] if markets else None)
        lp_locked = ((main_market or {}).get("lp") or {}).get("lpLockedPct")
        no_lp = bool(NO_LP_TOKEN.search((main_market or {}).get("marketType") or p.get("dexId") or ""))
        old = age_h is not None and age_h > 24 * 30
        fee = (R.get("transferFee") or {}).get("pct") or 0
        creator_pct = (R["creatorBalance"] / 10 ** dec) / supply * 100 if supply and R.get("creatorBalance") else 0
        if on_curve:
            lp = {"what": "Main LP locked or burned", "why": "Still on the launchpad bonding curve, so there is no LP to pull yet.", "sev": "info", "res": "curve"}
        elif no_lp:
            lp = {"what": "Main LP locked or burned", "why": "Concentrated-liquidity pool: liquidity comes from many separate positions, so there's no single LP to lock.", "sev": "info", "res": "n/a"}
        elif lp_locked is not None and lp_locked >= 95:
            lp = {"what": "Main LP locked or burned", "why": "The dev can't pull the main pool.", "sev": "ok", "res": plain(lp_locked, 0)}
        else:
            lp = {"what": "Main LP locked or burned", "why": "Unlocked, but the token is over a month old and liquidity is spread across many providers." if old else "The pool's liquidity can be withdrawn.",
                  "sev": "warn" if lp_locked is None else "warn" if old else "warn" if lp_locked >= 50 else "crit", "res": "unknown" if lp_locked is None else plain(lp_locked, 0)}
        checks += [
            {"what": "Mint authority revoked", "why": "Nobody can print new tokens.", "sev": "crit" if R.get("mintAuthority") else "ok", "res": "ACTIVE" if R.get("mintAuthority") else "revoked"},
            {"what": "Freeze authority revoked", "why": "Nobody can freeze your wallet.", "sev": "crit" if R.get("freezeAuthority") else "ok", "res": "ACTIVE" if R.get("freezeAuthority") else "revoked"},
            lp,
            {"what": "No transfer tax", "why": "Buys and sells aren't taxed by the token.", "sev": ("crit" if fee > 5 else "warn") if fee > 0 else "ok", "res": plain(fee, 1) if fee > 0 else "0%"},
            {"what": "Metadata locked", "why": "Name, symbol and image can't change.", "sev": "warn" if (R.get("tokenMeta") or {}).get("mutable") else "ok", "res": "mutable" if (R.get("tokenMeta") or {}).get("mutable") else "immutable"},
            {"what": "Creator sold or holds little", "why": "The deployer wallet's current share.", "sev": "crit" if creator_pct > 5 else "warn" if creator_pct > 1 else "ok", "res": plain(creator_pct, 2)},
        ]
        if R.get("rugged"):
            checks.insert(0, {"what": "Marked as rugged", "why": "RugCheck has flagged this token as rugged.", "sev": "crit", "res": "RUGGED"})
        for r in R.get("risks") or []:
            if COVERED_RISKS.search(r.get("name") or ""):
                continue
            lvl = (r.get("level") or "").lower()
            checks.append({"what": r.get("name") or "RugCheck risk", "why": r.get("description") or "", "sev": "crit" if lvl == "danger" else "warn" if lvl == "warn" else "info", "res": r.get("value") or lvl or "flag"})
    else:
        checks.append({"what": "RugCheck data unavailable", "why": "Mint, freeze, LP and holder checks could not run. Try again in a minute.", "sev": "warn", "res": "missing"})

    flags = []

    def add(sev, t, m, d):
        flags.append({"sev": sev, "t": t, "m": m, "d": d})
    if on_curve:
        add("info", "Still on the bonding curve", p.get("dexId"), "The token hasn't graduated to a DEX pool yet. Price and liquidity follow the launchpad's curve.")
    if nets:
        add("crit" if ins_pct >= 10 else "warn" if ins_pct >= 5 else "info", "Insider network still holds supply", plain(ins_pct, 1),
            f"{ins_wallets} linked wallets received {plain(ins_recv_pct, 1)} of supply and still hold {plain(ins_pct, 1)} ({usd(ins_usd)}).")
        if not on_curve:
            add("crit" if dump_all <= -50 else "warn" if dump_all <= -25 else "info", "Insiders can crash the price", pct(dump_main, 0),
                f"Selling their whole bag into the main pool would move price {pct(dump_main, 0)}. Across all pools, about {pct(dump_all, 0)}.")
    mature = age_h is not None and age_h > 24 * 30
    if cluster["n"] >= 5:
        add("info" if mature else "crit" if cluster["n"] >= 8 else "warn", "Look-alike wallet sizes", f"{cluster['n']} wallets",
            f"{cluster['n']} top wallets each hold {cluster['lo']:.2f}–{cluster['hi']:.2f}%. " +
            ("On a token this old, even sizes are less likely to be a launch bundle." if mature else "Even splits like this at launch often mean one buyer (a bundle)."))
    if has_rug:
        add("crit" if top10 >= 35 else "warn" if top10 >= 20 else "ok", "Top 10 wallet concentration", plain(top10, 1),
            f"The 10 largest wallets outside the pools hold {plain(top10, 1)}. The biggest single wallet holds {plain(top1, 2)}." +
            (" On older tokens, some of these are usually exchange wallets." if age_h is not None and age_h > 24 * 30 and top10 >= 20 else ""))
    vl = vol24 / total_liq if total_liq else 0
    if total_liq:
        add("crit" if vl >= 30 else "warn" if vl >= 10 else "ok", "Volume far above liquidity", f"{vl:.0f}× liq",
            f"{usd(vol24)} traded in 24h against {usd(total_liq)} of liquidity." + (" Real demand is much smaller than the volume suggests." if vl >= 10 else ""))
    win_min = max(1, min(age_h, 24) * 60) if age_h is not None else 1440
    per_min = tx24 / win_min
    avg_trade = vol24 / tx24 if tx24 else 0
    add("crit" if per_min >= 200 else "warn" if per_min >= 50 else "ok", "Bot-like trade rate", f"{integer(per_min)}/min",
        f"{integer(tx24)} trades averaging {usd(avg_trade)} each." + (" Very high counts of small trades point to bots and wash trading." if per_min >= 50 else ""))
    if age_h is not None:
        add("crit" if age_h < 6 else "warn" if age_h < 72 else "ok", "Pair age", age_str(age_ms),
            "Very new. Most launches give back most of their gains within hours to days." if age_h < 72 else "The token has survived its first few days.")
    run = ch.get("h24")
    if run is not None:
        add("crit" if run >= 1000 else "warn" if run >= 300 else "crit" if run <= -70 else "warn" if run <= -40 else "ok",
            "Already dumped" if run <= -40 else "Already pumped", pct(run, 0),
            f"Up {math.floor(run / 100 + 1.5)}× {'since launch' if age_h is not None and age_h < 24 else 'in 24h'}. New buyers are likely buying from early holders taking profit." if run >= 300
            else f"Down {abs(run):.0f}% in 24h. Holders are exiting." if run <= -40 else "No extreme price move in the last 24h.")
    lm = total_liq / mcap * 100 if mcap else 0
    if mcap and not on_curve:
        add(("warn" if mature else "crit") if lm < 5 else "warn" if lm < 10 else "ok", "Liquidity vs market cap", plain(lm, 1),
            f"On-chain pool depth is {plain(lm, 1)} of market cap." + (" Large holders can't exit on-chain without crashing the price." if lm < 10 else "") +
            (" Older tokens often trade mostly on centralized exchanges." if mature and lm < 10 else ""))
    unlocked = [m for m in markets if m.get("pubkey") != p.get("pairAddress") and not NO_LP_TOKEN.search(m.get("marketType") or "") and not (((m.get("lp") or {}).get("lpLockedPct") or 0) >= 95)]
    if unlocked:
        add("info" if age_h is not None and age_h > 24 * 30 else "warn", "Unlocked side pools", f"{len(unlocked)} pool" + ("" if len(unlocked) == 1 else "s"),
            f"{', '.join(dict.fromkeys(str(m.get('marketType')) for m in unlocked))}. Liquidity there can be withdrawn at any time.")
    if trade and trade.get("buy"):
        loss = trade.get("roundTripLossPct")
        sell = trade.get("sell")
        add("crit" if sell is None else "crit" if loss >= 20 else "warn" if loss >= 5 else "ok", "Real tradability (Jupiter)",
            "can't sell" if sell is None else plain(loss, 1) + " loss",
            f"Jupiter can buy {trade['sizeSol']} SOL worth but found no route to sell it back." if sell is None else
            f"Buying {trade['sizeSol']} SOL and selling it straight back would return {sell['outSol']:.3f} SOL (buy impact {plain(trade['buy']['impactPct'], 2)}, sell impact {plain(sell['impactPct'], 2)}, fees included)." +
            (" Liquidity Jupiter can reach is thin for this size." if loss >= 5 else ""))
    elif trade and trade.get("noRoute"):
        add("crit", "Real tradability (Jupiter)", "no route", f"Jupiter found no route to buy {trade['sizeSol']} SOL of this coin.")
    sites, socials = (meta.get("info") or {}).get("websites") or [], (meta.get("info") or {}).get("socials") or []
    launchpad_only = bool(sites) and all(LAUNCHPAD_URL.search(s.get("url") or "") for s in sites)
    thin = not sites or launchpad_only or len(socials) < 1
    add("warn" if thin else "ok", "Project footprint", f"{len(sites) + len(socials)} links",
        (("Only a launchpad page and " if launchpad_only else "Only ") + (", ".join(s.get("type", "") for s in socials) if socials else "no socials") + ". No own site, docs or team info.") if thin else "Has its own website and socials.")
    h1 = tx.get("h1")
    if h1 and h1.get("sells"):
        br = h1["buys"] / h1["sells"]
        add("warn" if br < 0.8 else "info", "Buy pressure, last hour", f"{br:.2f} b/s", f"{integer(h1['buys'])} buys vs {integer(h1['sells'])} sells. Price {pct(ch.get('h1'))} over the hour.")
    flags.sort(key=lambda f: SEV_ORDER[f["sev"]])

    c_crit, c_warn = sum(1 for c in checks if c["sev"] == "crit"), sum(1 for c in checks if c["sev"] == "warn")
    contract = ({"sev": "warn", "v": "Unknown", "lvl": 2} if not has_rug else {"sev": "crit", "v": "High", "lvl": 4} if c_crit
                else {"sev": "warn", "v": "Medium", "lvl": 2} if c_warn else {"sev": "ok", "v": "Low", "lvl": 1})
    score = sum(SEV_WEIGHT[f["sev"]] for f in flags)
    market = ({"sev": "crit", "v": "Very high", "lvl": 4} if score >= 10 else {"sev": "crit", "v": "High", "lvl": 3} if score >= 6
              else {"sev": "warn", "v": "Elevated", "lvl": 2} if score >= 3 else {"sev": "ok", "v": "Low", "lvl": 1})
    market["score"] = score

    Q = liq / 2
    impact = []
    if Q > 0:
        for x in (100, 500, 1000, 2500, 5000, 10000):
            move = (((Q + x) / Q) ** 2 - 1) * 100
            impact.append({"size": x, "move": move, "share": x / Q * 100, "sev": "crit" if move >= 10 else "warn" if move >= 3 else "ok",
                           "label": "heavy" if move >= 10 else "noticeable" if move >= 3 else "small"})
    safe_order = Q * (math.sqrt(1.02) - 1) if Q > 0 else 0

    crit_names = [f["t"].lower() for f in flags if f["sev"] == "crit"]
    summary = (("Contract checks couldn't run because RugCheck didn't respond." if not has_rug
                else "The contract looks clean: the dev can't mint, freeze or pull the main pool." if contract["sev"] == "ok"
                else "The contract has weak spots worth checking before you buy." if contract["sev"] == "warn"
                else "The contract has dangerous permissions. The dev can still hurt holders directly.") + " " +
               ("Trading looks orderly for a meme coin." if market["sev"] == "ok"
                else f"Trading risk is {market['v'].lower()}" + (f", driven by {', '.join(crit_names[:3])}." if crit_names else ".")))

    return {
        "fetchedAt": fetched_at, "summary": summary, "hasRug": has_rug,
        "token": {"mint": mint, "symbol": (meta["baseToken"].get("symbol") or p["baseToken"].get("symbol")), "name": (meta["baseToken"].get("name") or p["baseToken"].get("name")),
                  "image": (meta.get("info") or {}).get("imageUrl")},
        "pair": {"address": p.get("pairAddress"), "dexId": p.get("dexId"), "url": p.get("url"), "createdAt": created if math.isfinite(created) else None, "count": len(pairs),
                 "source": p.get("source") or "dexscreener", "quote": (p.get("quoteToken") or {}).get("symbol") or "",
                 "dexScreener": ({"address": dex_best["pairAddress"], "liq": (dex_best.get("liquidity") or {}).get("usd") or 0, "price": num(dex_best.get("priceUsd")) or 0, "url": dex_best.get("url")}
                                 if dex_best else {"missing": True}) if p.get("source") == "geckoterminal" else None},
        "priceCheck": ({"jupiter": ref_price if ref_price and ref_price > 0 else None, "allOff": not near,
                        "ignored": [{"address": x.get("pairAddress"), "dexId": x.get("dexId"), "priceUsd": num(x.get("priceUsd")) or 0, "liq": (x.get("liquidity") or {}).get("usd") or 0,
                                     "quote": (x.get("quoteToken") or {}).get("symbol") or ""} for x in ignored[:5]], "ignoredCount": len(ignored)}
                       if (ref_price and ref_price > 0) or ignored else None),
        "sources": {"dexscreener": sum(1 for x in pairs if x.get("source") != "geckoterminal"), "geckoterminal": sum(1 for x in pairs if x.get("source") == "geckoterminal")},
        "trade": trade, "links": {"websites": sites, "socials": socials},
        "rug": {"score": R.get("score"), "scoreNormalised": R.get("score_normalised"), "totalHolders": R.get("totalHolders")},
        "price": price, "liq": liq, "liqBase": liq_base, "totalLiq": total_liq, "vol24": vol24, "tx24": tx24, "mcap": mcap, "fdv": p.get("fdv"), "ageMs": age_ms,
        "tx": tx, "vol": vol, "ch": ch, "supply": supply, "holders": holders, "cluster": cluster, "top10": top10, "insPct": ins_pct, "insUsd": ins_usd, "insTokens": ins_tokens,
        "insWallets": ins_wallets, "nets": len(nets), "dumpMain": dump_main, "dumpAll": dump_all, "checks": checks, "flags": flags, "contract": contract, "market": market,
        "impact": impact, "safeOrder": safe_order, "quoteSide": Q,
    }


def review(q, trade=True):
    """Review whatever the user typed: {review, rugError, geckoError} or, for an ambiguous ticker, {candidates}."""
    r = resolve(q)
    if "candidates" in r:
        return {"candidates": r["candidates"]}
    out = {}

    def run_trade():
        out["trade"] = tradability(r["mint"]) if trade else None
    t = threading.Thread(target=run_trade)
    t.start()
    ref = jupiter_prices([r["mint"]]).get(r["mint"])
    fetched = fetch_token(r["mint"])
    t.join(30)
    return {"review": analyze(fetched["dex"], fetched["rug"], fetched["fetchedAt"], trade=out.get("trade"), ref_price=ref),
            "rugError": fetched["rugError"], "geckoError": fetched["geckoError"]}

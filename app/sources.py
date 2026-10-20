"""Market data for Solana coins from DexScreener (and later GeckoTerminal and RugCheck): the Coins, Wallets and Monitor pages.
A port of Rug Radar's src/sources.js, built up page by page."""
import json
import urllib.error
import urllib.request

DEX = "https://api.dexscreener.com"
UA = {"User-Agent": "pumpgrad/1.0", "Accept": "application/json"}
TIMEOUT = 12


class LookupError_(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def get_json(url, timeout=TIMEOUT):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return json.loads(r.read(8 << 20))
    except urllib.error.HTTPError as e:
        host = url.split("/")[2]
        if e.code == 429:
            raise LookupError_(f"{host} is rate-limiting requests. Wait a minute and try again.", 429)
        raise LookupError_(f"{host} returned HTTP {e.code}.", 404 if e.code == 404 else 502)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LookupError_(f"{url.split('/')[2]} did not respond ({type(e).__name__}).", 502)


def market_of(p):
    """The fields the pages show for a coin, from one DexScreener pair."""
    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0
    out = {"symbol": (p.get("baseToken") or {}).get("symbol"), "priceUsd": f(p.get("priceUsd")),
           "marketCap": p.get("marketCap") or p.get("fdv") or 0, "liquidity": (p.get("liquidity") or {}).get("usd") or 0, "url": p.get("url")}
    if (p.get("dexId") or "").lower() in ("pumpfun", "pumpswap"):
        out["pumpfun"] = True
    return out


def lookup_tokens(mints):
    """{mint: market} for the coins DexScreener lists, 15 per request, one pool per coin (a pump.fun curve pair only when
    nothing else exists; a coin that only has its old curve pair there is looked up with the full pair list). The result
    has a `failed` set: coins asked about but not answered (an error, not 'not listed')."""
    out, failed, dex_ids = {}, set(), {}
    for i in range(0, len(mints), 15):
        batch = mints[i:i + 15]
        try:
            pairs = get_json(f"{DEX}/tokens/v1/solana/" + ",".join(batch))
        except LookupError_:
            failed.update(batch)
            continue
        for p in pairs if isinstance(pairs, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            if m not in batch:
                continue
            curve = p.get("dexId") == "pumpfun" and not ((p.get("liquidity") or {}).get("usd") or 0) > 0
            if curve and m in out:
                continue
            if m not in out or dex_ids.get(m) == "pumpfun" or ((p.get("liquidity") or {}).get("usd") or 0) > (out[m].get("liquidity") or 0):
                out[m], dex_ids[m] = market_of(p), p.get("dexId") or ""
    for m in [m for m in mints if dex_ids.get(m) == "pumpfun"][:5]:
        try:
            best = None
            for p in (get_json(f"{DEX}/latest/dex/tokens/{m}").get("pairs") or []):
                if p.get("chainId") != "solana" or (p.get("baseToken") or {}).get("address") != m or p.get("dexId") == "pumpfun":
                    continue
                if not ((p.get("liquidity") or {}).get("usd") or 0) > 0:
                    continue
                if best is None or p["liquidity"]["usd"] > best["liquidity"]["usd"]:
                    best = p
            if best:
                out[m] = {**market_of(best), "symbol": out[m].get("symbol") or (best.get("baseToken") or {}).get("symbol")}
        except LookupError_:
            pass
    return out, failed

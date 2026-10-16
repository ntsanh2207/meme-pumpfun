"""Token-selection rules: the conditions a rule may use, validation, and the starter rules.

A rule is a dict:
  {"id": str, "name": str, "enabled": bool, "live": bool (follow it in the live watch),
   "entry": minutes after graduation to buy,
   "conditions": [{"feature": name, "op": ">=" | "<=" | "=", "value": number}, ...],
   "tp": take profit as a fraction (0.4 = +40%), "sl": stop loss as a fraction,
   "hold": time stop in minutes}
Shared by the web server (which saves rules) and analyze.py (which backtests them).
"""
import json
import os
import re
import tempfile

# name -> (plain-language label, kind). Values are stored in native units:
# pct as a fraction (0.25 = 25%), usd in dollars, sol in SOL, min in minutes, num as a count.
FEATURES = {
    "ret_since_grad": ("Price change since graduation", "pct"),
    "drawdown_from_high": ("Distance below the highest price since graduation", "pct"),
    "ret_last_5m": ("Price change over the last 5 minutes", "pct"),
    "mcap_usd": ("Market cap", "usd"),
    "vol_usd_total": ("Trading volume since graduation", "usd"),
    "vol_usd_last_5m": ("Trading volume in the last 5 minutes", "usd"),
    "trades_total": ("Number of trades since graduation", "num"),
    "trades_last_5m": ("Number of trades in the last 5 minutes", "num"),
    "avg_trade_usd_5m": ("Average trade size over the last 5 minutes", "usd"),
    "dev_buy_sol": ("Creator's own buy at launch", "sol"),
    "mins_to_grad": ("Minutes from creation to graduation", "min"),
    "socials": ("Has a Twitter, Telegram or website link", "flag"),
    "has_twitter": ("Has an X (Twitter) link", "flag"),
    "has_website": ("Has a website link", "flag"),
    "has_telegram": ("Has a Telegram link", "flag"),
    # recorded live from the moment the recorder started; older tokens have none and are skipped
    "holders": ("Number of holders", "num"),
    "top10_pct": ("Share of supply held by the top 10 wallets", "pct"),
    "dev_pct": ("Share of supply held by the creator", "pct"),
    "snipers_pct": ("Share of supply held by snipers", "pct"),
    "bundlers_pct": ("Share of supply held by bundled wallets", "pct"),
    "dev_pct_at_grad": ("Share of supply the creator still held at graduation", "pct"),
    # pump.fun often leaves the creator's share blank; this is the latest reading that was not blank
    "dev_pct_last": ("Share of supply the creator held in the latest reading before buying", "pct"),
    "holders_change_5m": ("Change in the number of holders over the last 5 minutes", "pct"),
    # from the list of the 50 largest holders (the trading pool is not in it); recorded since 12 October 2026
    "top1_pct": ("Share of supply held by the largest wallet", "pct"),
    "top5_pct": ("Share of supply held by the top 5 wallets", "pct"),
    "top50_pct": ("Share of supply held by the top 50 wallets", "pct"),
    "new_top_pct": ("Share of supply held by top-50 wallets that arrived in the last 5 minutes", "pct"),
    "top_snipers": ("Snipers among the 50 largest holders", "num"),
    "top_bundlers": ("Bundled wallets among the 50 largest holders", "num"),
    "fees_sol": ("Trading fees paid on the token so far", "sol"),
}

# entry + hold may not pass the price history kept after each graduation (295 minutes)
LIMITS = {"max_rules": 40, "max_conditions": 8, "entry": [5, 240], "hold": [5, 240],
          "max_entry_plus_hold": 295, "tp": [0.05, 10.0], "sl": [0.05, 0.95]}

PRESETS = [
    {"id": "momentum-near-high", "name": "Momentum near the high", "enabled": True, "entry": 20,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": 1.0},
                    {"feature": "drawdown_from_high", "op": ">=", "value": -0.10}],
     "tp": 0.4, "sl": 0.3, "hold": 30},
    {"id": "small-creator-buy", "name": "Small creator buy, slower graduation", "enabled": True, "entry": 15,
     "conditions": [{"feature": "dev_buy_sol", "op": "<=", "value": 2.0},
                    {"feature": "mins_to_grad", "op": ">=", "value": 5.0},
                    {"feature": "mcap_usd", "op": ">=", "value": 40000.0}],
     "tp": 0.6, "sl": 0.3, "hold": 60},
    {"id": "real-volume-not-bots", "name": "Real volume, not bot trades", "enabled": True, "entry": 30,
     "conditions": [{"feature": "vol_usd_last_5m", "op": ">=", "value": 2000.0},
                    {"feature": "trades_last_5m", "op": "<=", "value": 500.0},
                    {"feature": "drawdown_from_high", "op": ">=", "value": -0.30}],
     "tp": 0.4, "sl": 0.3, "hold": 60},
    {"id": "socials-and-rising", "name": "Has socials and still rising", "enabled": True, "entry": 20,
     "conditions": [{"feature": "socials", "op": "=", "value": 1.0},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.0},
                    {"feature": "mcap_usd", "op": ">=", "value": 60000.0}],
     "tp": 0.6, "sl": 0.3, "hold": 60},
    {"id": "dip-buy", "name": "Buy the dip of a strong token", "enabled": True, "entry": 30,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": 0.5},
                    {"feature": "drawdown_from_high", "op": "<=", "value": -0.30},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.0}],
     "tp": 0.4, "sl": 0.2, "hold": 30},
    {"id": "website-and-x", "name": "Has website and X", "enabled": True, "entry": 20,
     "conditions": [{"feature": "has_website", "op": "=", "value": 1.0},
                    {"feature": "has_twitter", "op": "=", "value": 1.0}],
     "tp": 0.4, "sl": 0.3, "hold": 30},
    {"id": "website-x-real-volume", "name": "Website and X, with real volume", "enabled": True, "entry": 30,
     "conditions": [{"feature": "has_website", "op": "=", "value": 1.0},
                    {"feature": "has_twitter", "op": "=", "value": 1.0},
                    {"feature": "vol_usd_last_5m", "op": ">=", "value": 2000.0},
                    {"feature": "trades_last_5m", "op": "<=", "value": 500.0},
                    {"feature": "drawdown_from_high", "op": ">=", "value": -0.30}],
     "tp": 0.4, "sl": 0.3, "hold": 60},
    {"id": "late-survivor", "name": "Late survivor (60 minutes in)", "enabled": True, "entry": 60,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": 0.0},
                    {"feature": "vol_usd_last_5m", "op": ">=", "value": 1000.0}],
     "tp": 0.4, "sl": 0.3, "hold": 60},
]

# Clusters: kinds of token at the moment of buying, written in the same condition language
# as rules. A token belongs to the first cluster whose conditions all hold; the last one
# takes whatever is left. analyze.py reports every cluster and splits each rule's round
# trips by cluster, so a cluster's conditions can be copied into a rule to target or avoid it.
CLUSTERS = [
    {"id": "inflated", "name": "Creator bought the curve: parked at an inflated price",
     "note": "The price sits 50 times or more above graduation on almost no real trading. "
             "The market cap is a number on a screen; there is nobody to sell to.",
     "conditions": [{"feature": "dev_buy_sol", "op": ">=", "value": 80.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": 50.0}]},
    {"id": "pulled", "name": "Creator bought the curve: already pulled",
     "note": "The creator's wallets have sold. Buying the drop is buying what they left behind.",
     "conditions": [{"feature": "dev_buy_sol", "op": ">=", "value": 80.0},
                    {"feature": "drawdown_from_high", "op": "<=", "value": -0.5}]},
    {"id": "staircase", "name": "Creator bought the curve: climbing in bot-sized trades",
     "note": "A steady climb made of thousands of trades of a few dollars. It looks like the "
             "healthiest chart on the page and ends in a one-second drop of about 90%.",
     "conditions": [{"feature": "dev_buy_sol", "op": ">=", "value": 80.0},
                    {"feature": "avg_trade_usd_5m", "op": "<=", "value": 15.0}]},
    {"id": "creator-other", "name": "Creator bought the curve: other",
     "note": "Creator-owned supply with normal-sized trades. Few tokens.",
     "conditions": [{"feature": "dev_buy_sol", "op": ">=", "value": 80.0}]},
    {"id": "instant", "name": "Graduated within its first minute",
     "note": "The curve was filled in one burst by a few wallets instead of by the creator alone.",
     "conditions": [{"feature": "mins_to_grad", "op": "<=", "value": 1.0}]},
    {"id": "organic-strong", "name": "Organic launch: holding above graduation",
     "note": "Took minutes or hours to graduate, trades above its graduation price and "
             "within 30% of its high.",
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": 0.3},
                    {"feature": "drawdown_from_high", "op": ">=", "value": -0.3}]},
    {"id": "organic-faded", "name": "Organic launch: faded",
     "note": "Half or more below its high. Rarely collapses further in one move; it bleeds.",
     "conditions": [{"feature": "drawdown_from_high", "op": "<=", "value": -0.5}]},
    {"id": "organic-mid", "name": "Organic launch: in between",
     "note": "Everything else.", "conditions": []},
]

# Strategies the live watch (watch.py) always runs beside the saved rules, and analyze.py
# backtests with them. They live here, not in the settings, so editing rules in the web app
# never removes them. Each targets tokens the creator does not own (creator's buy under 80 SOL).
_NO_CREATOR = {"feature": "dev_buy_sol", "op": "<=", "value": 79.0}
WATCH_EXTRA = [
    {"id": "live-first-minute-snipers", "name": "Built-in: first-minute graduate, snipers 3%+ (15 min)",
     "enabled": True, "entry": 15,
     "conditions": [{"feature": "mins_to_grad", "op": "<=", "value": 1.0}, _NO_CREATOR,
                    {"feature": "snipers_pct", "op": ">=", "value": 0.03}],
     "tp": 0.4, "sl": 0.3, "hold": 60},
    {"id": "live-real-trades-near-high", "name": "Built-in: real-size trades near the high (15 min)",
     "enabled": True, "entry": 15,
     "conditions": [_NO_CREATOR, {"feature": "avg_trade_usd_5m", "op": ">=", "value": 30.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": 0.3},
                    {"feature": "drawdown_from_high", "op": ">=", "value": -0.2},
                    {"feature": "vol_usd_last_5m", "op": ">=", "value": 10000.0}],
     "tp": 0.4, "sl": 0.3, "hold": 60},
    {"id": "live-organic-survivor", "name": "Built-in: organic survivor above graduation (30 min)",
     "enabled": True, "entry": 30,
     "conditions": [_NO_CREATOR, {"feature": "mins_to_grad", "op": ">=", "value": 5.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": 0.0},
                    {"feature": "drawdown_from_high", "op": ">=", "value": -0.3},
                    {"feature": "vol_usd_last_5m", "op": ">=", "value": 5000.0}],
     "tp": 0.4, "sl": 0.3, "hold": 60},
]


def with_extras(saved):
    """The saved rules followed by the built-in live strategies (a saved rule with the same id wins)."""
    ids = {r["id"] for r in saved}
    return saved + [r for r in validate(WATCH_EXTRA) if r["id"] not in ids]


def _number(x, lo, hi, what):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x:
        raise ValueError(f"{what} must be a number")
    if not lo <= x <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return x


def validate(rules):
    """Return a cleaned copy of a list of rules, or raise ValueError saying what is wrong."""
    if not isinstance(rules, list) or len(rules) > LIMITS["max_rules"]:
        raise ValueError(f"expected a list of at most {LIMITS['max_rules']} rules")
    out, ids = [], set()
    for n, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise ValueError(f"rule {n} is not an object")
        name = r.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            raise ValueError(f"rule {n}: name must be 1 to 60 characters")
        name = name.strip()
        rid = r.get("id")
        if not isinstance(rid, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", rid):
            rid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or f"rule-{n}"
        base, k = rid, 2
        while rid in ids:
            rid, k = f"{base[:34]}-{k}", k + 1
        ids.add(rid)
        conds = r.get("conditions", [])
        if not isinstance(conds, list) or len(conds) > LIMITS["max_conditions"]:
            raise ValueError(f"'{name}': at most {LIMITS['max_conditions']} conditions")
        clean = []
        for c in conds:
            if not isinstance(c, dict) or c.get("feature") not in FEATURES:
                raise ValueError(f"'{name}': unknown condition")
            kind = FEATURES[c["feature"]][1]
            if kind == "flag":
                clean.append({"feature": c["feature"], "op": "=", "value": 1.0})
                continue
            if c.get("op") not in (">=", "<="):
                raise ValueError(f"'{name}': a condition must be 'at least' or 'at most'")
            clean.append({"feature": c["feature"], "op": c["op"],
                          "value": float(_number(c.get("value"), -1e12, 1e12, f"'{name}': condition value"))})
        entry = _number(r.get("entry"), *LIMITS["entry"], f"'{name}': entry minute")
        hold = _number(r.get("hold"), *LIMITS["hold"], f"'{name}': time stop")
        if entry != int(entry) or hold != int(hold):
            raise ValueError(f"'{name}': entry minute and time stop must be whole minutes")
        if entry + hold > LIMITS["max_entry_plus_hold"]:
            raise ValueError(f"'{name}': entry minute plus time stop must be at most "
                             f"{LIMITS['max_entry_plus_hold']} (prices are kept for that long after graduation)")
        out.append({
            "id": rid, "name": name, "enabled": bool(r.get("enabled", True)),
            # recorded by the live watch; once any rule has this, the watch follows only those
            "live": bool(r.get("live", False)),
            "entry": int(entry), "conditions": clean,
            "tp": float(_number(r.get("tp"), *LIMITS["tp"], f"'{name}': take profit")),
            "sl": float(_number(r.get("sl"), *LIMITS["sl"], f"'{name}': stop loss")),
            "hold": int(hold),
        })
    return out


def load(settings_dir):
    """The saved rules, or the starter rules when nothing has been saved yet."""
    try:
        with open(os.path.join(settings_dir, "rules.json"), encoding="utf-8") as f:
            return validate(json.load(f))
    except (OSError, ValueError):
        return validate(PRESETS)


def save(settings_dir, rules):
    path = os.path.join(settings_dir, "rules.json")
    # a temp file of its own per save, so two saves at once cannot write into each other
    fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix="rules.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)

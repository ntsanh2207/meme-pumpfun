"""Token-selection rules: the conditions a rule may use, validation, and the starter rules.

A rule is a dict:
  {"id": str, "name": str, "enabled": bool, "live": bool (follow it in the live watch),
   "entry": minutes after graduation to buy,
   "conditions": [{"feature": name, "op": ">=" | "<=" | "=", "value": number}, ...],
   "tp": take profit as a fraction (0.4 = +40%), "sl": stop loss as a fraction,
   "be": optional (0 = none): once the price has been this far above the entry (0.5 = +50%), the stop moves up to the entry price plus "be_stop" (0.1 = +10%; default 0 = the entry price),
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
    "avg_trade_usd_total": ("Average trade size since graduation", "usd"),
    "mins_since_high": ("Minutes since the highest price", "min"),
    "hour_utc": ("Hour of graduation (UTC, 0-23)", "num"),
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
          "max_entry_plus_hold": 295, "tp": [0.05, 10.0], "sl": [0.05, 0.95], "be": [0.05, 10.0], "be_stop": [0.0, 9.0]}

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
# never removes them.
WATCH_EXTRA = [
    {"id": "strategy-a", "name": "Strategy A: creator out, X and website (15 min)",
     "enabled": True, "entry": 15,
     "conditions": [{"feature": "dev_pct_at_grad", "op": "<=", "value": 0.0},
                    # pump.fun often shows the creator's share as 0% before it has counted it, then
                    # 20-40% minutes later: the latest reading before buying must say 0% too
                    {"feature": "dev_pct_last", "op": "<=", "value": 0.0},
                    {"feature": "has_twitter", "op": "=", "value": 1.0},
                    {"feature": "has_website", "op": "=", "value": 1.0},
                    {"feature": "avg_trade_usd_5m", "op": ">=", "value": 30.0},
                    {"feature": "mcap_usd", "op": ">=", "value": 20000.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": -0.5}],
     "tp": 0.4, "sl": 0.3, "hold": 15},
    {"id": "strategy-b", "name": "Strategy B: heavy volume, few trades (15 min)",
     "enabled": True, "entry": 15,
     "conditions": [{"feature": "vol_usd_last_5m", "op": ">=", "value": 33700.0},
                    {"feature": "trades_total", "op": "<=", "value": 2805.0},
                    # the volume above is also met by a rug pull's own dump; this keeps those out
                    {"feature": "ret_since_grad", "op": ">=", "value": -0.5}],
     # held up to 30 minutes since 19 Oct 2026 (was 240): on 5,315 coins the 30-minute hold did better on both the
     # older (+3.6% a trade) and the newest coins (+4.3%)
     "tp": 1.0, "sl": 0.5, "hold": 30},
    # found by the research of 19 Oct 2026 (picked on the older 70% of 5,314 coins, +5% a trade on the newest 30%,
    # about 0% without its 3 best trades): paper only until the live record says otherwise
    {"id": "strategy-c", "name": "Strategy C: late momentum (8 min)",
     "enabled": True, "entry": 8,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": 3.0},
                    {"feature": "vol_usd_last_5m", "op": ">=", "value": 40000.0}],
     "tp": 0.6, "sl": 0.5, "hold": 30},
    # Strategy C narrowed by the deeper research of 19 Oct 2026: an X link, not graduated 18:00-23:59 UTC, held up to
    # 60 minutes. These filters were tried on the newest coins too, so only live paper results can confirm them.
    {"id": "strategy-c-plus", "name": "Strategy C+: late momentum, X link, daytime (8 min)",
     "enabled": True, "entry": 8,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": 3.0},
                    {"feature": "vol_usd_last_5m", "op": ">=", "value": 40000.0},
                    {"feature": "has_twitter", "op": "=", "value": 1.0},
                    {"feature": "hour_utc", "op": "<=", "value": 17.0}],
     "tp": 0.6, "sl": 0.5, "hold": 60},
    # the user's rule of 19 Oct 2026: Strategy A's coins with 16%+ of the supply in bundled wallets. Backtested on only
    # 5 trades (+49% a trade, all on the newest coins: the creator and bundler readings exist only since 16 Oct), so it
    # is paper only until the live record has a few dozen trades
    {"id": "strategy-d", "name": "Strategy D: creator out, X, web, bundled 16%+ (15 min)",
     "enabled": True, "entry": 15,
     "conditions": [{"feature": "dev_pct_at_grad", "op": "<=", "value": 0.0},
                    {"feature": "has_twitter", "op": "=", "value": 1.0},
                    {"feature": "has_website", "op": "=", "value": 1.0},
                    {"feature": "avg_trade_usd_5m", "op": ">=", "value": 34.0},
                    {"feature": "mcap_usd", "op": ">=", "value": 20000.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": -0.5},
                    {"feature": "bundlers_pct", "op": ">=", "value": 0.16}],
     "tp": 1.0, "sl": 0.4, "hold": 15},
    # found on 19 Oct 2026 by studying Strategy B's trades (all coins seen, so the newest 30% is not an unseen test):
    # the coins Strategy B passes at minute 15, bought 5 minutes later if the price held steady (0 to +20%) meanwhile.
    # Backtest: 21 trades, +24% a trade. Paper only until the live record agrees. (Until 19 Oct 2026 it measured B's
    # conditions at minute 20 instead, which also bought coins Strategy B never passed.)
    {"id": "strategy-b20", "name": "Strategy B20: Strategy B, then a steady price (20 min)",
     "enabled": True, "entry": 20,
     "conditions": [{"feature": "vol_usd_last_5m", "op": ">=", "value": 33700.0, "at": 15},
                    {"feature": "trades_total", "op": "<=", "value": 2805.0, "at": 15},
                    {"feature": "ret_since_grad", "op": ">=", "value": -0.5, "at": 15},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.0},
                    {"feature": "ret_last_5m", "op": "<=", "value": 0.2}],
     "tp": 1.0, "sl": 0.5, "hold": 30},
    # Strategy B with the same steady-price condition at 15 minutes (same study): 33 trades, +24% a trade, 9 stop
    # losses, and a 1 s delay costs it about 8 points a trade. Paper only, to compare with B and B20 live
    {"id": "strategy-b-plus", "name": "Strategy B+: heavy volume, steady price (15 min)",
     "enabled": True, "entry": 15,
     "conditions": [{"feature": "vol_usd_last_5m", "op": ">=", "value": 33700.0},
                    {"feature": "trades_total", "op": "<=", "value": 2805.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": -0.5},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.0},
                    {"feature": "ret_last_5m", "op": "<=", "value": 0.2}],
     "tp": 1.0, "sl": 0.5, "hold": 30},
    # The first version of Strategy B20 (19 Oct 2026), kept as its own strategy: Strategy B's three conditions measured at
    # 20 minutes (not at 15), plus a steady price (0 to +20%) over the last 5 minutes. It is NOT a subset of Strategy B: it
    # also buys coins whose volume only reached the threshold by minute 20. Backtest: 17 trades, +46% a trade, no stop loss,
    # a 1-3 s delay costs little. Found by looking at all the coins, so paper only until the live record agrees
    {"id": "strategy-b20-direct", "name": "Strategy B20-direct: volume and steady price at 20 min",
     "enabled": True, "entry": 20,
     "conditions": [{"feature": "vol_usd_last_5m", "op": ">=", "value": 33700.0},
                    {"feature": "trades_total", "op": "<=", "value": 2805.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": -0.5},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.0},
                    {"feature": "ret_last_5m", "op": "<=", "value": 0.2}],
     "tp": 1.0, "sl": 0.5, "hold": 30,
     # 20 Oct 2026: once the price is 50% above the entry the stop moves up to entry +10%. Backtest (20 trades): +36.3% against
     # +35.9% without it (a stop at the entry price: +40.4%; at +20%: +28.3%); a trigger at +30% would cost 12 points a trade
     "be": 0.5, "be_stop": 0.1},
    # found on 19 Oct 2026 with the strategy-mining skill from Strategy C's trades (all coins seen): C's coins whose
    # price held steady (0 to +20%) over the last 5 minutes. Backtest: 29 trades, +27.5% a trade (newest 30%: +25.5%),
    # 4 stop losses; a 1-3 s delay costs about 5 points. Paper only until the live record agrees
    {"id": "strategy-c-steady", "name": "Strategy C-steady: late momentum, steady price (8 min)",
     "enabled": True, "entry": 8,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": 3.0},
                    {"feature": "vol_usd_last_5m", "op": ">=", "value": 40000.0},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.0},
                    {"feature": "ret_last_5m", "op": "<=", "value": 0.2}],
     "tp": 0.6, "sl": 0.5, "hold": 30},
    # found on 20 Oct 2026 with the strategy-mining skill, from a plain base rule (buy at 20 min if the volume of the last 5 minutes is at
    # least $23.5K and the trades since graduation at most 2,969: 112 trades, +9.0% a trade). Added to it: the coin graduated within 5
    # minutes of its creation (78 trades, +28.1%; a 1-3 s delay costs about 8 points) and it is not more than 70% below its high (the
    # coin has not collapsed). Backtest: 62 trades, +38.0% a trade, 68% won (earlier 70% of coins +41.0%, newest 30% +32.1%, without the
    # best 3 +35.1%, bootstrap range +19% to +55%), 9 of 10 days positive; the threshold moved by a quarter stays at +33% to +36%.
    # The filters were chosen after trying about 900 conditions on the same 10 days, so expect less; the delay test was not run on this
    # exact form. Paper only until the live record agrees
    {"id": "strategy-e", "name": "Strategy E: fast graduate, not collapsed (20 min)",
     "enabled": True, "entry": 20,
     "conditions": [{"feature": "vol_usd_last_5m", "op": ">=", "value": 23500.0},
                    {"feature": "trades_total", "op": "<=", "value": 2969.0},
                    {"feature": "mins_to_grad", "op": "<=", "value": 5.0},
                    {"feature": "drawdown_from_high", "op": ">=", "value": -0.7}],
     "tp": 1.0, "sl": 0.4, "hold": 60},
    # found on 21 Oct 2026 with the strategy-mining skill, from a rule the user gave (buy at 10 min if the price change since graduation is
    # at least -94%, the average trade size over the last 5 minutes at least $33.92 and the coin has a Telegram link; +100% / -50% / 30 min:
    # 178 trades, +11.6% a trade, newest 30% -1.6%). Added in two rounds (about 470 candidates tried): the market cap at least $90K
    # (123 trades, +18.5%) and the price at least 5% below its high so far (100 trades). Backtest: 100 trades, +25.8% a trade, 63% won
    # (earlier 70% +26.9%, newest 30% +21.6%, without the best 3 +23.6%, bootstrap range +10.8% to +40.0%), 9 of 11 days positive; thresholds
    # moved by a quarter stay at +21% to +26%; a 1-3 s delay costs 7-9 points (about +20% a trade). Weak spot: entry minutes 8, 9 and 11
    # are negative on the newest 30%, so it needs minute 10. The filters were chosen on the same 10 days they are measured on and the
    # newest 30% is only 21 trades. Paper only until 20-30 live trades agree
    {"id": "strategy-f", "name": "Strategy F: Telegram, market cap, off its high (10 min)",
     "enabled": True, "entry": 10,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": -0.94},
                    {"feature": "avg_trade_usd_5m", "op": ">=", "value": 33.92},
                    {"feature": "has_telegram", "op": ">=", "value": 1.0},
                    {"feature": "mcap_usd", "op": ">=", "value": 90000.0},
                    {"feature": "drawdown_from_high", "op": "<=", "value": -0.05}],
     "tp": 1.0, "sl": 0.5, "hold": 30},
]

# Every rule the system runs shows on the Lab page. analyze.py writes strategies/<id>.json (its backtest) and watch.py writes
# strategies/<id>.live.json (its live paper record) for each enabled one; the saved rules (or the starter ones) and the
# built-in ones in WATCH_EXTRA that no saved rule replaces make up the list (with_extras).


def cond_key(c):
    """A condition's measurement: its feature, and the minute it is measured at when that is not the entry minute
    ("vol_usd_last_5m@15"). Readings, failure counts and page columns are keyed by it."""
    return f"{c['feature']}@{c['at']}" if c.get("at") else c["feature"]


def cond_core(c):
    """What defines a condition, for telling one version of a rule from another."""
    return (c["feature"], c["op"], c["value"], c.get("at"))


def page_columns(conditions):
    """The readings a strategy page lists for each trade: one per measurement, except a condition
    that leaves every bought token the same value (a share of supply, never below 0, at most 0%)."""
    fixed = lambda c: "_pct" in c["feature"] and c["op"] == "<=" and c["value"] <= 0   # noqa: E731
    return list(dict.fromkeys(cond_key(c) for c in conditions if not fixed(c)))


def with_extras(saved, removed=()):
    """The saved rules followed by the built-in live strategies (a saved rule with the same id wins).
    A built-in rule deleted from the Lab (`removed`: see removed()) is left out, with any saved copy of it."""
    ids = {r["id"] for r in saved}
    gone = set(removed) & {b["id"] for b in WATCH_EXTRA}
    return [r for r in saved if r["id"] not in gone] + [r for r in validate(WATCH_EXTRA) if r["id"] not in ids | gone]


def removed(settings_dir):
    """The ids of built-in rules deleted from the Lab (settings/rules_removed.json); they come back with restore."""
    try:
        with open(os.path.join(settings_dir, "rules_removed.json"), encoding="utf-8") as f:
            ids = json.load(f)
    except (OSError, ValueError):
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def set_removed(settings_dir, ids):
    path = os.path.join(settings_dir, "rules_removed.json")
    fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix="rules_removed.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(sorted(set(ids)), f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


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
        entry = _number(r.get("entry"), *LIMITS["entry"], f"'{name}': entry minute")
        clean = []
        for c in conds:
            if not isinstance(c, dict) or c.get("feature") not in FEATURES:
                raise ValueError(f"'{name}': unknown condition")
            # "at": measured at that earlier minute instead of the entry minute (for example "Strategy B's
            # conditions at minute 15, then buy at minute 20")
            at = c.get("at")
            if at is not None:
                at = _number(at, LIMITS["entry"][0], entry - 1, f"'{name}': a condition's minute")
                if at != int(at):
                    raise ValueError(f"'{name}': a condition's minute must be a whole minute")
                at = int(at)
            extra = {"at": at} if at else {}
            kind = FEATURES[c["feature"]][1]
            if kind == "flag":
                clean.append({"feature": c["feature"], "op": "=", "value": 1.0, **extra})
                continue
            if c.get("op") not in (">=", "<="):
                raise ValueError(f"'{name}': a condition must be 'at least' or 'at most'")
            clean.append({"feature": c["feature"], "op": c["op"],
                          "value": float(_number(c.get("value"), -1e12, 1e12, f"'{name}': condition value")), **extra})
        hold = _number(r.get("hold"), *LIMITS["hold"], f"'{name}': time stop")
        if entry != int(entry) or hold != int(hold):
            raise ValueError(f"'{name}': entry minute and time stop must be whole minutes")
        if entry + hold > LIMITS["max_entry_plus_hold"]:
            raise ValueError(f"'{name}': entry minute plus time stop must be at most "
                             f"{LIMITS['max_entry_plus_hold']} (prices are kept for that long after graduation)")
        be = float(_number(r["be"], *LIMITS["be"], f"'{name}': breakeven trigger")) if r.get("be") else 0.0
        be_stop = float(_number(r.get("be_stop") or 0.0, *LIMITS["be_stop"], f"'{name}': breakeven stop level")) if be else 0.0
        if be and be_stop >= be:
            raise ValueError(f"'{name}': the breakeven stop level must be below its trigger")
        out.append({
            "id": rid, "name": name, "enabled": bool(r.get("enabled", True)),
            # recorded by the live watch; once any rule has this, the watch follows only those
            "live": bool(r.get("live", False)),
            "entry": int(entry), "conditions": clean,
            "tp": float(_number(r.get("tp"), *LIMITS["tp"], f"'{name}': take profit")),
            "sl": float(_number(r.get("sl"), *LIMITS["sl"], f"'{name}': stop loss")),
            "hold": int(hold),
            "be": be,
            "be_stop": be_stop,
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

"""Token-selection rules: the conditions a rule may use, validation, and the starter rules.

A rule is a dict:
  {"id": str, "name": str, "enabled": bool, "live": bool (follow it in the live watch),
   "entry": minutes after graduation to buy,
   "entry_to": optional, a later minute than "entry": the rule buys at the FIRST minute from "entry" to "entry_to" at which its conditions hold (one buy a token; added 22 Oct 2026 for the pattern crossing, which happens at a different minute for every coin),
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
import threading

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
    # shapes of the first minutes, from the 1-minute bars only (added 21 Oct 2026; the crossing pattern's own measures among them)
    "grad_high_vs_price": ("Price against the highest price of the graduation minute", "pct"),
    "dip_depth": ("Lowest close since the graduation minute, against that minute's high", "pct"),
    "mins_below_high": ("Minutes that closed below 97% of the graduation minute's high", "num"),
    "lower_high_minutes": ("Minutes whose high was no higher than the high of minute 1", "num"),
    "range_5m": ("Average candle range over the last 5 minutes", "pct"),
    "red_minutes_5m": ("Minutes that closed below their open in the last 5", "num"),
    "vol_trend_5m": ("Volume of the last 5 minutes against the 5 before", "pct"),
    "mins_since_creation": ("Minutes since the token was created", "min"),
}

# Patterns added to the Lab from the Pattern Discover page (23 Oct 2026): settings/lab_patterns.json holds {id: {"name", "spec"}}; each becomes a flag
# feature "pattern:<id>" (1 = the pattern fired in the last whole minute: analyze.pattern_features). load() reads the file first, so every
# process that loads the saved rules knows the features.
# Patterns that ship with the code (Strategies H and H+ use them): always known, never written to lab_patterns.json
BUILTIN_PATTERNS = {
    "quiet5k70": {"name": "Quiet start, bars within $5K, closes at least 70% of the level", "spec": {
        "family": "quiet", "Q": 3, "m0_close": [30000.0, 60000.0], "m0": [0.72, 0.92], "close_max": 0.93, "high_max": 1.0, "body_max": 0.25,
        "low_min": 25000.0, "vol_ratio": 0.75, "range_max": 5000.0, "close_min": 0.70}},
    # Pattern Discover's "Jump >=35%, 5 quiet minutes (range <=25%), from below" (found 22 Oct 2026; patterns.chosen_minutes family 'jump'): minute 0 closes
    # $35K-$50K; the mean candle range of 5 quiet minutes is at most 25%, then the first minute (6 to 12) that closes at least 35% above the previous
    # close while that close was still below the high of minute 0. Strategy I trades its trigger minute + 1
    "jump35": {"name": "Jump of 35% after 5 quiet minutes, from below the level", "spec": {
        "family": "jump", "m0": None, "Q": 5, "range_max": 0.25, "jump_min": 0.35, "from_below": True, "exit": "B"}},
}
PATTERNS = {}
_patterns_lock = threading.Lock()
_SPEC_NUMBERS = {"cross": ("Q", "close_max", "high_max", "body_max", "low_min"), "jump": ("Q", "range_max", "jump_min")}


def _spec_ok(spec):
    nums = _SPEC_NUMBERS.get(spec.get("family"))
    return bool(nums) and spec.get("exit") in ("A", "B", "C") and spec.get("Q") in (3, 4, 5) and all(
        isinstance(spec.get(k), (int, float)) and not isinstance(spec.get(k), bool) for k in nums)


def load_patterns(settings_dir):
    """Reads settings/lab_patterns.json into PATTERNS and the `pattern:<id>` features. A missing file means no patterns; an unreadable one leaves what was
    loaded before. Safe to call from several threads: the new table is built aside and swapped in, and a feature is deleted only when its pattern is gone."""
    global PATTERNS
    try:
        if settings_dir is None:
            raise FileNotFoundError
        with open(os.path.join(settings_dir, "lab_patterns.json"), encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise ValueError
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError):
        return PATTERNS
    with _patterns_lock:
        new = {k: dict(v) for k, v in BUILTIN_PATTERNS.items()}
        for pid, p in doc.items():
            if isinstance(p, dict) and isinstance(p.get("spec"), dict) and re.fullmatch(r"[0-9a-f]{10}", str(pid)) and _spec_ok(p["spec"]):
                new[pid] = {"name": str(p.get("name", pid))[:80], "spec": p["spec"]}
        for pid, p in new.items():
            FEATURES["pattern:" + pid] = (f"Pattern fired in the last minute: {p['name']}", "flag")
        for k in [k for k in list(FEATURES) if k.startswith("pattern:") and k[8:] not in new]:
            FEATURES.pop(k, None)
        PATTERNS = new
    return PATTERNS


load_patterns(None)         # (the built-in patterns' features exist as soon as this module is imported)


def save_patterns(settings_dir, doc):
    doc = {k: v for k, v in doc.items() if k not in BUILTIN_PATTERNS}
    fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix="lab_patterns.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(settings_dir, "lab_patterns.json"))


# entry + hold may not pass the price history kept after each graduation (120 minutes; 295 before 25 Oct 2026)
LIMITS = {"max_rules": 40, "max_conditions": 8, "entry": [4, 115], "max_entry_span": 20, "hold": [5, 116],
          "max_entry_plus_hold": 120, "tp": [0.05, 10.0], "sl": [0.05, 0.95], "be": [0.05, 10.0], "be_stop": [0.0, 9.0]}

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
    {"id": "strategy-h-plus", "name": "Strategy H+: quiet start, small bars, closes >= 70%",
     "enabled": True, "entry": 4,
     "conditions": [{"feature": "pattern:quiet5k70", "op": "=", "value": 1.0}],
     "tp": 1.0, "sl": 0.5, "hold": 30},
    # 23 Oct 2026, the strategy-mining skill on the Pattern Discover pattern 'jump35' (base: buy at the first of minutes 7 to 13 after the jump, +100% / -40% / 30
    # min: 60 trades +15.7%, earlier 70% +16.1%, newest 30% +15.2%, without the best 3 +11.6%). Round 1 tried 178 one-condition filters (57 beat the base by 5
    # points, 40 of them held on the newest 30%); chosen: the price has not fallen more than 3.3% under the graduation price (ret_since_grad >= -0.033).
    # Backtest: 47 trades, +29.5% a trade, 62% won (earlier 70% +37.7%, newest 30% +21.0%, without the best 3 +25.1%, bootstrap +12% to +47%), 10 of 13 days
    # positive; the threshold from -15% to +10% stays at +24% to +30%; the entry range 7-10 +35.5%, but starting at minute 8 the newest 30% falls to +0.7%; a
    # 60-minute hold +35.2%, +150% target +16.6%. A second round added range_5m <= 25% (34 trades +38.1%), not adopted: fewer trades. About 340 candidates were
    # tried on the same 13 days they are measured on and the 1-3 s delay replay cannot run on a range rule (stops cost about -60%), so expect less. Paper only
    {"id": "strategy-i", "name": "Strategy I: jump after 5 quiet minutes, not under grad price",
     "enabled": True, "entry": 7, "entry_to": 13,
     "conditions": [{"feature": "pattern:jump35", "op": "=", "value": 1.0},
                    {"feature": "ret_since_grad", "op": ">=", "value": -0.033}],
     "tp": 1.0, "sl": 0.4, "hold": 30},
    # 24 Oct 2026, the strategy-mining skill on Strategy F (8,523 coins: 134 trades, +18.6%, but earlier 70% +27.2% and newest 30% -3.2%: the edge had faded). Round 1 tried
    # 320 one-condition filters (79 beat F by 5 points on the earlier 70%, 36 of those held on the newest 30%); chosen: the price rose at least 13.4% over the last 5 minutes.
    # F+ = F plus that: 72 trades, +32.0% a trade, 68% won (earlier 70% +39.3%, newest 30% +17.4%, without the best 3 +29.3%, bootstrap +15% to +48%; the threshold
    # x0.75 / x1.25 stays +30% to +34%; entry 12 is -2.0%, 10 and 11 are fine). Round 2 tried 206 more: the price at minute 5 had not already pumped (price change since
    # graduation at minute 5 at most +226%, a condition measured at minute 5, the exact form). F++ = F+ plus that: 54 trades, +43.5%, 76% won (earlier 70% +45.8%,
    # newest 30% +38.1%, without the best 3 +40.5%, bootstrap +24% to +61%; the cap from +170% to +283% stays +41% to +48%). About 526 filters were tried on the same
    # 14 days, the 1-3 s delay replay was not run (1-second bars exist for about a week only) and stops lose about -85% when they gap. Paper only
    {"id": "strategy-f-momentum", "name": "Strategy F+: F with momentum (10 min)",
     "enabled": True, "entry": 10,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": -0.94},
                    {"feature": "avg_trade_usd_5m", "op": ">=", "value": 33.92},
                    {"feature": "has_telegram", "op": "=", "value": 1.0},
                    {"feature": "mcap_usd", "op": ">=", "value": 90000.0},
                    {"feature": "drawdown_from_high", "op": "<=", "value": -0.05},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.1337}],
     "tp": 1.0, "sl": 0.5, "hold": 30},
    {"id": "strategy-f-momentum-5", "name": "Strategy F++: F+ and not pumped by minute 5 (10 min)",
     "enabled": True, "entry": 10,
     "conditions": [{"feature": "ret_since_grad", "op": ">=", "value": -0.94},
                    {"feature": "avg_trade_usd_5m", "op": ">=", "value": 33.92},
                    {"feature": "has_telegram", "op": "=", "value": 1.0},
                    {"feature": "mcap_usd", "op": ">=", "value": 90000.0},
                    {"feature": "drawdown_from_high", "op": "<=", "value": -0.05},
                    {"feature": "ret_last_5m", "op": ">=", "value": 0.1337},
                    {"feature": "ret_since_grad", "op": "<=", "value": 2.261, "at": 5}],
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
        entry_to = None
        if r.get("entry_to") not in (None, "", 0):
            entry_to = _number(r.get("entry_to"), entry + 1, LIMITS["entry"][1], f"'{name}': last entry minute")
            if entry_to != int(entry_to) or entry_to - entry > LIMITS["max_entry_span"]:
                raise ValueError(f"'{name}': the entry minutes must be whole minutes, at most {LIMITS['max_entry_span']} apart")
            entry_to = int(entry_to)
        if (entry_to or entry) + hold > LIMITS["max_entry_plus_hold"]:
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
            "entry": int(entry), **({"entry_to": entry_to} if entry_to else {}), "conditions": clean,
            "tp": float(_number(r.get("tp"), *LIMITS["tp"], f"'{name}': take profit")),
            "sl": float(_number(r.get("sl"), *LIMITS["sl"], f"'{name}': stop loss")),
            "hold": int(hold),
            "be": be,
            "be_stop": be_stop,
        })
    return out


def load(settings_dir):
    """The saved rules, or the starter rules when nothing has been saved yet."""
    load_patterns(settings_dir)
    try:
        with open(os.path.join(settings_dir, "rules.json"), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, list):     # a rule that uses a pattern the Lab does not know (its file is gone) is left out; the others stay
            saved = [r for r in saved if not (isinstance(r, dict) and isinstance(r.get("conditions"), list) and any(
                isinstance(c, dict) and str(c.get("feature", "")).startswith("pattern:") and c.get("feature") not in FEATURES for c in r["conditions"]))]
        return validate(saved)
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

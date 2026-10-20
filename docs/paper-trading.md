# Paper trading: how the live watch simulates trades

Reference for AI agents working in this repository. It describes what `app/watch.py` does with each new pump.fun graduate, which code it shares with the backtest, and where the results are stored. File paths are relative to the repository root, and constants are quoted as they appear in the code. Re-check against the code before relying on a detail, because constants change.

## Summary

A paper trade is a simulated trade: nothing is signed or sent. For every strategy and every newly graduated token, `watch.py` decides "buy" or "pass" at the strategy's entry minute. The decision uses the same feature and exit code as the backtest (`app/analyze.py`). For a buy, the watch then follows the token's 1-minute bars until the take profit, stop loss or time stop, and records the net return after modeled costs. Passed tokens are followed to their time stop too, so you can see what buying them anyway would have done. The real-money executor, `app/trader.py`, only reads these decisions. With its mode `off` (the default in `trader.DEFAULTS`), it does nothing.

## Processes and data flow

```
holders.py (always on) ──► data/holders.db
   │  holder_watch: newly graduated tokens + listing JSON (info)
   │  holder_snaps: holder/creator/sniper/bundler shares over time
   ▼
watch.py (always on) ──► data/watch.db (tokens, measures, decisions, daily_sent)
   │                     data/watch.json, data/watch_tokens/<mint>.json   (web app: Record live tab)
   │                     data/strategies/<id>.live.json                   (web app: strategy pages)
   │                     data/reports/<YYYY-MM-DD>.csv                    (daily report files)
   ▼
trader.py (always on) ── reads watch.db decisions + watch.json, acts according to data/settings/trading.json
```

- **Discovery:** `holders.py` lists the newest graduates once a minute (`LIST_EVERY = 60`) from two pump.fun endpoints (`/coins/search-unrestricted` and `/coins`). It writes them to `holder_watch`. `watch.newly_listed()` reads that table. On start, `watch.py` also takes graduates first seen within `LOOKBACK = 3 h`. Tokens far past graduation (`CATCH_UP_AFTER = 6 h`) are caught up from history, at most `CATCH_UP_PER_ROUND = 4` per loop round.
- **Holder snapshots:** `holders.py` polls each coin that traded within `ALIVE = 600 s` of being listed. It polls every 2 minutes for the first 32 minutes, then every 5 minutes up to 62 minutes. Each snapshot is stored with the time it was read, and pump.fun's blank values are stored as NULL.
- **Strategies:** `watch.load_strategies()` returns three kinds of strategy, and all of them are recorded:
  - the automatic search's latest rule (`backtest_runs` in `data/pump.db`);
  - every enabled saved rule (`data/settings/rules.json`);
  - the built-ins `rules.WATCH_EXTRA`, which include `strategy-a` and `strategy-b`.

  "Shown" (on the Record live tab) means the rule is marked `live`, or that no rule is marked `live` at all.

## A rule

Rules are defined and validated in `app/rules.py`:

```
{"id", "name", "enabled", "live",
 "entry": minutes after graduation to buy,
 "conditions": [{"feature", "op": ">=" | "<=" | "=", "value"}],   # all must hold
 "tp": take profit as a fraction (0.4 = +40%), "sl": stop loss as a fraction, "hold": time stop in minutes}
```

Limits are in `rules.LIMITS`. In particular, `entry + hold <= 295`: bars are kept, and served, only up to 295 minutes after graduation.

## Timeline of one decision

Minute indices are relative to the graduation minute, `grad_min = graduated_ts // 60000 * 60000`. Minute 0 is the bar containing graduation. The backtest floors graduation to the minute the same way.

1. **Due time.** A decision for entry minute `e` is due `SETTLE = 2 s` after minute `e-1` ends, at `grad_min + e*60000 + 2000`. It stays due for `DUE_FOR = 180 s` (`watch.due()`).
2. **Fetch bars.** `watch.fetch_bars()` asks pump.fun for the 300 most recent 1-minute bars: `/candles/solana:<chain>/<mint>?res=1m&count=300&to=...&pool=...`. When a decision is due now, `fast_get()` retries after 0.5, 1 and 2 seconds. If the bar for minute `e-1` is missing but minute `e-2` exists, the source may be lagging, so it re-reads up to twice, 1 second apart. Bars are shaped into arrays by `watch.arrays()`, which carries the last close forward across minutes without trades. `analyze.load()` does the same for the backtest.
3. **Measure.** `watch.measure()` calls `analyze.features(tok, arr, vol, trd, e)`, the same function the backtest uses. Everything is computed from bars `0..e-1` and data known at that moment:
   - `ret_since_grad = close[e-1] / p_grad - 1`
   - `drawdown_from_high = close[e-1] / max(high[0:e]) - 1`
   - `ret_last_5m = close[e-1] / close[max(e-6, 0)] - 1`
   - `mcap_usd = close[e-1] * 1e9` (supply is 1 billion)
   - `vol_usd_last_5m` and `trades_last_5m`: the sum over minutes `e-5..e-1`. `avg_trade_usd_5m = vol5 / max(trd5, 1)`
   - `vol_usd_total` and `trades_total`: the sum over minutes `0..e-1`
   - `has_twitter`, `has_website`, `has_telegram`, `socials`: from the listing JSON (`info`)
   - `dev_buy_sol`: the creator's launch buy, from `first_trades.fetch()`
   - `mins_to_grad`: minutes from creation to graduation
   - Holder features (`analyze.holder_features`) come from `holders.db` snapshots taken at or before the buy moment `grad_min + e*60000`:
     - the last snapshot is used if it is at most `HOLDER_MAX_AGE = 6 min` old;
     - `dev_pct_at_grad` is the first non-blank `dev_pct` within 6 minutes of `grad_min`;
     - `dev_pct_last` is the latest non-blank `dev_pct`, at most 30 minutes old.

     Shares are stored in percent and used as fractions. Missing data is NaN.
4. **Tradable?** A token is tradable when `analyze.net_return()` is finite for it. That requires at least one trade in minutes `e-5..e-1`, and `stake_usd / depth(p0) <= MAX_IMPACT = 0.05`.
5. **Check.** `watch.check()` evaluates each condition with `analyze.matches()`. A NaN never passes any condition. `pick = tradable AND all conditions ok`.
6. **Record.** One row per `(mint, strategy)` is inserted into `decisions` (`INSERT OR IGNORE`):
   - `rule` is the rule JSON as it was at decision time;
   - `checks` holds `[{"feature", "op", "value", "actual", "ok"}]` for every condition;
   - `late = 1` when the decision was made more than `LATE = 180 s` after the buy minute. These are called "reconstructed" in the UI: correct data, but not actionable in time;
   - status is `open` if the token is tradable, otherwise `closed` with no result;
   - the row is committed at once, so `trader.py` sees a buy immediately.

   The measured features are stored in `measures` (one row per `(mint, entry)`).
7. **Follow.** On each loop, `watch.follow()` re-evaluates the exit on bars that are final: `done_minutes = (now - 2 s - grad_min) // 60000`. Bought tokens are re-fetched at most once a minute. Passed tokens are evaluated once their time stop has passed. The exit itself is `analyze.simulate()`, the same function the backtest uses (see below). A trade is finished when the stop or target is hit, or when `held >= hold`. While a bought trade is open, its interim return and its `path` (`[minute held, net return if sold then]`) are updated.
8. **Extras for bought tokens.**
   - `full_path`: the net return if simply held, minute by minute, for `FOLLOW = 120` minutes after the buy, ignoring the exits.
   - `alts`: the same buy with take profits from `ALT_TPS = (1.0, 2.0)`, the same stop, and a 120-minute time limit.

## Entry price, exits and costs

From `analyze.simulate(arr, entry, tp, sl, holds, stop_fill="close")` and `analyze.net_return()`:

- **Entry price:** `p0 = close[e-1]`, the close of the minute before the entry minute. There is no latency: the fill is assumed at that close.
- **Bars scanned:** minutes `k = e .. min(e + hold, 296) - 1`. Within each bar, the stop is checked before the target, which is the pessimistic order.
  - **Stop:** if `low[k]/p0 <= 1 - sl`, then `gross = min(min(open/p0, 1 - sl), close/p0) * (1 - SL_SLIP)` with `SL_SLIP = 0.03`. The stop never fills better than the stop level or the minute's close, and fills at the open if the bar gaps below the stop.
  - **Target:** if `high[k]/p0 >= 1 + tp`, then `gross = 1 + tp`.
  - **Time stop:** neither was hit by the last minute, so `gross = close[e + hold - 1] / p0`.
  - **Exit reason codes:** 1 is stop, 2 is target, 3 is time. `minutes_held = exit_minute - e + 1`.
- **Net return:**

  ```
  q_in  = depth(p0)          = POOL_TOKENS * sqrt(p0 * p_grad)            # USD depth of the constant-product pool
  q_out = depth(p0 * gross)
  r = gross * (1 - FEE)^2 / ((1 + stake/q_in) * (1 + stake*gross/q_out)) - 1
  ```

  - `FEE = 0.0135` per side: PumpSwap's 1.25% top tier plus priority fee or tip.
  - `POOL_TOKENS = 206.9e6`, the tokens a migration seeds into the pool.
  - `p_grad` is the classic curve's completion price in USD. `watch.py` reads it from `report.json` (`assumptions.grad_mcap_usd / 1e9`), with a default of `4.76e-5`.
  - `stake = STAKE_SOL * sol_usd(p_grad)`, with `STAKE_SOL = 0.1`. `sol_usd = p_grad / CURVE_SOL`, `CURVE_SOL = 115 / 279.9e6`.
  - The result shown in SOL is `net_return * 0.1`.

## Storage (`data/watch.db`, schema in `watch.SCHEMA`)

| Table | Key | Contents |
|---|---|---|
| `tokens` | mint | first_seen_ts, status (new/active/done/unchecked/failed), tries, info (JSON: listing, links, pool, created_ts, dev_buy_sol), graduated_ts |
| `measures` | (mint, entry) | measured_ts, tradable, traded, impact, entry_price, cluster, features (JSON of every feature) |
| `decisions` | (mint, strategy) | rule (JSON), entry, decided_ts, late, pick, checks (JSON), status (open/closed), exit_reason, minutes_held, net_return, path, full_path, alts, notified, notified_exit |

## Strategy pages (`#strategy/<id>`)

These apply to every enabled rule shown in the Lab (`#lab`; the old `#strategy/<id>` links open it).

- **Live record:** `watch.write_strategy_live()` writes `data/strategies/<id>.live.json` on every publish. It includes:
  - every bought decision, with `measured` values for `rules.page_columns(conditions)`. That function omits a condition that pins one value, for example a share of supply required to be at most 0%;
  - per-condition counts of "failed" and "no reading" among tradable passed tokens;
  - the count of untradable tokens;
  - the average result of passed tokens, had they been bought anyway.

  `server.py` serves it at `/api/strategy/<id>/live`.
- **Backtest:** `analyze.write_strategy_pages()` writes `data/strategies/<id>.json` on every analyze run, served at `/api/strategy/<id>`. The backtest needs at least 500 complete coins (`analyze.py` main, `if n < 500`). A complete coin is classic (`classic = 1`), has a known graduation time and has `candles_done = 1`.
- **UI:** `index.html`, functions `renderStrategy`, `liveSection`, `backtestSection`. Paper trades are sorted by buy time (`graduated_ts + entry`), newest first, not by decision time.

## How the trader uses decisions

`trader.py` treats as a buy signal any decision that meets all of these:
- `pick = 1` and `late = 0` (live, not reconstructed);
- made within `SIGNAL_FOR = 60 s`;
- by a strategy chosen for trading (`data/settings/trading.json`).

Its modes:
- `off`: no new buys; open positions are still managed;
- `dry`: real Jupiter quotes, but nothing is signed or sent;
- `live`: real transactions signed with `data/settings/wallet.json`.

In `dry` and `live`, exits are measured on Jupiter sell quotes, not on 1-minute bars. That makes `dry` the realistic counterpart of a paper trade.

## Verified behaviour (2026-10-17)

- **Recomputation:** six Strategy A paper trades were recomputed from freshly fetched pump.fun bars with independent code (buy at the minute-14 close, then stop, target or time stop). All six matched the recorded exit type and minutes held, and the net return within 0.5 percentage points. That gap is the price impact, which the check left out. Examples:
  - MAGA2028 (live): target in 1 minute, +36.0%;
  - FMR (live): target in 2 minutes, +35.7%;
  - Dopep (reconstructed): stop in 2 minutes, −34.1%.
- **No look-ahead:** features use bars `0..e-1` only, and holder snapshots taken at or before the buy moment. Reconstructed decisions use bars fetched with `to = grad_min + (296 + 3) min`.

## Known limitations

1. **The entry is optimistic.** The fill is at `close[e-1]`, with zero latency and no intra-minute slippage. A real order lands seconds after minute `e-1` ends. This matters most for tokens that hit the target within 1–2 minutes.
2. **1-minute resolution.** A crash inside one bar can make a real stop fill far below `-sl`. The simulation partly covers this with the min-with-close rule and `SL_SLIP`.
3. **Costs are modeled.** They come from pool depth, not from real quotes.
4. **Holder data is sparse.** `dev_pct_at_grad` is often missing. In one sample of 427 tokens Strategy A passed without a reading:
   - pump.fun's `devHoldingsPercent` was blank in every snapshot for 175;
   - `holders.py` took no snapshot at all for 134;
   - the snapshots in the first 6 minutes were blank but later ones were not, for 111;
   - the first snapshot came more than 6 minutes after graduation, for 7.

   Tokens without a reading never pass a condition on that feature.
5. **Social links are read late.** They come from the listing JSON as first seen (live) or as last collected (backtest), not as of the buy minute. The risk is low if links cannot change after launch.
6. **Candle access is rate-limited.** pump.fun's candle endpoint returns HTTP 429 per IP, possibly at Cloudflare. Requests failing all retries leave tokens unmeasured ("no bars" in the watch log).

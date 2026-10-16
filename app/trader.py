#!/usr/bin/env python3
"""Turn the live watch's buy signals into swaps, in a dry run or for real.

Runs forever, beside watch.py. A buy signal is a live decision (not reconstructed) by a strategy
shown on the Record live tab, made within the last SIGNAL_FOR seconds. Each one is bought with
SOL through Jupiter (which routes graduated pump.fun tokens through their PumpSwap pool). Every
open position is then priced every CHECK_EVERY seconds by asking Jupiter what selling all of it
would return, and sold at the strategy's take profit, stop loss or time stop measured on that
quote, so fees and slippage are already in it.

Modes (data/settings/trading.json, saved from the Trading tab):
  off   nothing is bought; open positions are still managed and sold by their rules
  dry   everything happens on quotes: no transaction is signed or sent
  live  real transactions, signed with the key in data/settings/wallet.json

Live orders are safe against the usual ways a bot loses money by accident:
  - every order runs in its own thread, so a slow transaction never delays another position's stop;
  - an order's outcome is read from its own transaction (tokens and SOL in and out of the wallet),
    never from a wallet balance that may lag or include other holdings;
  - a sent order is "sent" until its signature is confirmed or its blockhash has expired, so it is
    never declared failed while it can still land, and a landed sale is never sent twice;
  - before a sale the wallet's real token balance is read, and only the position's own tokens sold;
  - on start, orders cut short by a restart are looked up on chain and finished or released;
  - after a full sale the empty token account is closed, getting its rent (~0.002 SOL) back.

Every step of every order is timed (the latency columns): how late the signal was seen, the
quote, building and signing, landing on chain, and the whole order from signal to landed.

Writes data/trades.db and data/trading_state.json (for the Trading tab).

Usage: python3 trader.py [--data data]
"""
import argparse
import base64
import faulthandler
import json
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

import notify

SOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
JUP = "https://lite-api.jup.ag/swap/v1"
MIN = 60000
SIGNAL_FOR = 60          # a buy signal older than this is not acted on
CHECK_EVERY = 1.5        # seconds between price checks of an open position
LAND_FOR = 90            # longest wait for a transaction, whatever its blockhash says
WATCHDOG = 180           # seconds a round may take before the trader is restarted (normal: under 1 s)
HARD_MAX_SOL = 1.0       # no single order is ever larger than this, whatever the settings say
# a position exists (or may exist) in these states: they count for "one per token", "most open" and "sell all"
HELD_STATES = ("buying", "buy_sent", "open", "selling", "sell_sent", "check", "stuck")
MAX_SELL_FAILS = 5       # failed sales in a row before a position is left for a person
RENT = 0.00203928        # SOL held by a token account, returned when it is closed
RESERVE_PER_POSITION = 0.01   # SOL kept back per live position for the fees and temporary accounts of its sale
RETRY_ALERT_MS = 300000  # a sale still not out after this long is alerted
STUCK_RETRY_MS = 300000  # a position that could not be sold is tried again this often
DEFAULTS = {"mode": "off", "sol_per_trade": 0.2, "max_open": 2, "daily_loss_limit_sol": 1.0,
            "slippage_bps": 500, "sell_slippage_bps": 1000, "stop_slippage_bps": 2500,
            "priority_fee_lamports": 300000, "rpc_url": "https://api.mainnet-beta.solana.com", "sell_all": 0,
            "sell_ids": (),    # positions asked to be sold by hand: (id, unix ms of the request)
            "strategies": ()}  # ids of the strategies to trade; none: those ticked "Record live"
LIMITS = {"sol_per_trade": (0.01, HARD_MAX_SOL), "max_open": (1, 5), "daily_loss_limit_sol": (0.01, 10.0),
          "slippage_bps": (50, 3000), "sell_slippage_bps": (50, 5000), "stop_slippage_bps": (50, 5000),
          "priority_fee_lamports": (0, 5_000_000)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mint TEXT NOT NULL, symbol TEXT, strategy TEXT NOT NULL, mode TEXT NOT NULL,   -- dry or live
  signal_ts INTEGER, opened_ts INTEGER, buy_sig TEXT,
  sol_in REAL, tokens INTEGER,          -- SOL that left the wallet (fees and rent included), raw tokens received
  tp REAL, sl REAL, hold_until INTEGER,
  status TEXT NOT NULL,                 -- buying, buy_sent, open, selling, sell_sent, check, stuck, closed, failed
  last_value REAL, last_checked INTEGER, -- SOL that selling everything was quoted at
  sell_sig TEXT, sol_out REAL, closed_ts INTEGER, reason TEXT, error TEXT,
  UNIQUE (mint, strategy)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);   -- e.g. the last "sell all" request handled
CREATE TABLE IF NOT EXISTS wallet_tx (   -- every transaction of the wallet, read from the chain, for the balance chart
  sig TEXT PRIMARY KEY, slot INTEGER, ts INTEGER,   -- ts: unix ms of its block
  pre REAL, post REAL,                  -- the wallet's SOL before and after it
  signer INTEGER                        -- 1: signed by the wallet (a trade or a close); 0: SOL sent to it
);
"""
COLUMNS = {"sell_asked": "INTEGER NOT NULL DEFAULT 0",   # 1: "sell all" was asked, 2: sold by hand; sold at the next good quote
           "entry_ts": "INTEGER",          # end of the entry minute: when the signal was due
           "lat_seen_ms": "INTEGER",       # entry minute's end -> trader saw the signal
           "lat_quote_ms": "INTEGER",      # buy quote
           "lat_build_ms": "INTEGER",      # swap built and signed
           "lat_land_ms": "INTEGER",       # sent -> confirmed on chain
           "lat_total_ms": "INTEGER",      # entry minute's end -> buy confirmed
           "buy_quote_tokens": "INTEGER",  # tokens the quote promised: against tokens, the buy's slippage
           "sell_trigger_ts": "INTEGER",   # when the take profit / stop / time stop was seen
           "sell_quote_sol": "REAL",       # what the triggering quote said selling would return
           "lat_sell_ms": "INTEGER",       # trigger -> sale confirmed
           "rent_back_sol": "REAL",        # rent returned by closing the empty token account
           "fee_sol": "REAL",              # network fees paid by this trade's transactions
           "buy_last_valid": "INTEGER",    # block height after which the buy can no longer land
           "sell_last_valid": "INTEGER",
           "check_kind": "TEXT",           # 'buy' or 'sell': the order a 'check' row is waiting to learn about
           "sell_fails": "INTEGER NOT NULL DEFAULT 0",
           "next_sell_ts": "INTEGER",      # no new sale before this, after failed ones
           "empty_reads": "INTEGER NOT NULL DEFAULT 0",   # empty token reads in a row at a sale
           "retry_since": "INTEGER",       # first sale attempt in a row that did not go out, for the alert
           "last_empty_ts": "INTEGER"}     # when the wallet last showed none of the token


class SimError(RuntimeError):
    """Jupiter's simulation says the swap would fail: it is not sent."""


def http_json(url, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def retrying(fn, tries=4):
    """fn(), asked again on a busy or briefly unavailable service (429, 5xx, dropped connection)."""
    for wait in (0.4, 1.0, 2.0, None)[-tries:]:
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if wait is None or not (e.code == 429 or e.code >= 500):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if wait is None:
                raise
        time.sleep(wait)


# Jupiter's free API allows about 5 requests per 10 s from one address. Every call is counted,
# so price checks can keep to what is left over and never crowd out a real buy or sale.
_jup_calls, _jup_lock = [], threading.Lock()
JUP_WINDOW, JUP_FOR_CHECKS = 10.0, 2   # price checks only while fewer than this many calls in the window


def jup_call(fn):
    with _jup_lock:
        now = time.time()
        _jup_calls[:] = [t for t in _jup_calls if now - t < JUP_WINDOW] + [now]
    return fn()


def jup_recent():
    with _jup_lock:
        now = time.time()
        return sum(1 for t in _jup_calls if now - t < JUP_WINDOW)


def quote(input_mint, output_mint, amount, slippage_bps, tries=4):
    q = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint, "amount": int(amount),
                                "slippageBps": int(slippage_bps)})
    return retrying(lambda: jup_call(lambda: http_json(f"{JUP}/quote?{q}", timeout=10)), tries)


def rpc(cfg, method, params):
    r = retrying(lambda: http_json(cfg["rpc_url"], {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                   timeout=20))
    if r.get("error"):
        raise RuntimeError(f"{method}: {r['error'].get('message')}")
    return r.get("result")


CONFIRMED = {"commitment": "confirmed"}


class Wallet:
    """The signing key, from a file nobody else can read."""

    def __init__(self, path):
        if os.stat(path).st_mode & 0o077:
            raise PermissionError(f"{path} can be read by other users: run chmod 600 on it")
        from solders.keypair import Keypair            # imported here: dry runs do not need it
        with open(path, encoding="utf-8") as f:
            self.kp = Keypair.from_bytes(bytes(json.load(f)))
        self.pubkey = str(self.kp.pubkey())

    def sign(self, tx_b64):
        """(signed transaction in base64, its signature). The signature is known before sending,
        so an order is recorded under it before it can land."""
        from solders.transaction import VersionedTransaction
        raw = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        tx = VersionedTransaction(raw.message, [self.kp])
        return base64.b64encode(bytes(tx)).decode(), str(tx.signatures[0])


PUBLIC_RPC = "api.mainnet-beta.solana.com"
# free RPCs that need no key; with the public RPC, every transaction also goes out through them,
# since one free RPC alone often drops transactions (each send of the same signed transaction is
# the same transaction: it can only land once)
SEND_ALSO = ("https://solana-rpc.publicnode.com",)


def send(cfg, signed_b64):
    params = [signed_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 5, "preflightCommitment": "confirmed"}]
    if PUBLIC_RPC in cfg["rpc_url"]:
        for url in SEND_ALSO:
            def also(url=url):
                try:
                    http_json(url, {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": params}, timeout=10)
                except Exception:
                    pass
            threading.Thread(target=also, daemon=True).start()
    return rpc(cfg, "sendTransaction", params)


def build_swap(cfg, wallet, q):
    """Jupiter's swap transaction for a quote, signed: (base64 transaction, signature,
    lastValidBlockHeight). Refuses one that Jupiter's own simulation says would fail, so a
    doomed transaction never costs a fee."""
    body = {"quoteResponse": q, "userPublicKey": wallet.pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": int(cfg["priority_fee_lamports"]), "priorityLevel": "veryHigh"}}}
    r = retrying(lambda: jup_call(lambda: http_json(f"{JUP}/swap", body)))
    if r.get("simulationError"):
        raise SimError(f"Jupiter's simulation says this swap would fail: {r['simulationError']}")
    signed, sig = wallet.sign(r["swapTransaction"])
    if not r.get("lastValidBlockHeight"):
        raise SimError("Jupiter did not say until when the transaction can land: not sent")
    return signed, sig, int(r["lastValidBlockHeight"])


def tx_status(cfg, sig, last_valid):
    """'landed', 'failed' (landed, did nothing, fee spent), 'expired' (can no longer land) or
    'pending'. Raises when the RPC cannot be asked: the caller keeps the order as it is."""
    st = (rpc(cfg, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]) or {}).get("value", [None])[0]
    if st and st.get("err"):
        return "failed"
    if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
        return "landed"
    if last_valid and rpc(cfg, "getBlockHeight", [CONFIRMED]) > last_valid + 5:
        # past its blockhash, with a few blocks to spare for a slow node: ask once more
        st = (rpc(cfg, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]) or {}).get("value", [None])[0]
        if st and st.get("err"):
            return "failed"
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return "landed"
        return "expired"
    return "pending"


def wait_landed(cfg, sig, last_valid, signed_b64=None):
    """The transaction's fate, waiting up to LAND_FOR seconds and sending it again every few
    seconds while it can still land: 'landed', 'failed', 'expired' or 'unknown' (no answer: the
    order is then checked by signature later, never assumed either way)."""
    start, resent = time.time(), time.time()
    while time.time() - start < LAND_FOR:
        try:
            fate = tx_status(cfg, sig, last_valid)
            if fate != "pending":
                return fate
        except Exception:
            pass        # a busy RPC: ask again
        if signed_b64 and time.time() - resent > 3:
            resent = time.time()
            try:
                send(cfg, signed_b64)
            except Exception:
                pass
        time.sleep(0.7)
    return "unknown"


def tx_effect(cfg, sig, owner, mint):
    """(change in the wallet's raw tokens of `mint`, change in its SOL, network fee in SOL) from the
    transaction itself, read at confirmed commitment. Raises when it cannot be read yet."""
    tx = rpc(cfg, "getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                           "maxSupportedTransactionVersion": 0}])
    if not tx:
        raise RuntimeError(f"transaction {sig} not readable yet")
    meta, keys = tx["meta"], tx["transaction"]["message"]["accountKeys"]
    i = next(k for k, a in enumerate(keys) if (a["pubkey"] if isinstance(a, dict) else a) == owner)
    sol = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS

    def tokens(side):
        return sum(int(b["uiTokenAmount"]["amount"]) for b in meta.get(side) or []
                   if b.get("owner") == owner and b.get("mint") == mint)
    return tokens("postTokenBalances") - tokens("preTokenBalances"), sol, meta["fee"] / LAMPORTS


def tx_effect_patiently(cfg, sig, owner, mint, tries=10):
    for k in range(tries):
        try:
            return tx_effect(cfg, sig, owner, mint)
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(1)


def token_accounts(cfg, owner, mint):
    """[(account address, token program, raw amount)] of the wallet for one mint."""
    res = rpc(cfg, "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    return [(a["pubkey"], a["account"]["owner"], int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]))
            for a in res.get("value", [])]


def close_empty_accounts(cfg, wallet, mint):
    """Close the wallet's empty token accounts of `mint`: the rent comes back. Returns SOL recovered."""
    from solders.compute_budget import set_compute_unit_price
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    owner = wallet.kp.pubkey()
    ixs = [Instruction(Pubkey.from_string(program), bytes([9]),        # CloseAccount: account, destination, owner
                       [AccountMeta(Pubkey.from_string(acc), False, True), AccountMeta(owner, False, True),
                        AccountMeta(owner, True, False)])
           for acc, program, amount in token_accounts(cfg, wallet.pubkey, mint) if amount == 0]
    if not ixs:
        return 0.0
    bh = rpc(cfg, "getLatestBlockhash", [CONFIRMED])["value"]
    msg = MessageV0.try_compile(owner, [set_compute_unit_price(10_000)] + ixs, [], Hash.from_string(bh["blockhash"]))
    tx = VersionedTransaction(msg, [wallet.kp])
    signed = base64.b64encode(bytes(tx)).decode()
    sig = send(cfg, signed)
    if wait_landed(cfg, sig, int(bh["lastValidBlockHeight"]), signed) != "landed":
        return 0.0
    return max(0.0, tx_effect_patiently(cfg, sig, wallet.pubkey, mint)[1])


def sol_balance(cfg, owner):
    return rpc(cfg, "getBalance", [owner, CONFIRMED])["value"] / LAMPORTS


def clean_cfg(raw):
    """The settings with every value checked; anything missing or malformed takes its default,
    so a typo can never stop the trader from managing what it holds."""
    cfg = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    if raw.get("mode") in ("off", "dry", "live"):
        cfg["mode"] = raw["mode"]
    for k, (lo, hi) in LIMITS.items():
        try:
            v = float(raw[k])
            if v == v:
                cfg[k] = min(max(v, lo), hi)
        except (KeyError, TypeError, ValueError):
            pass
    for k in ("max_open", "slippage_bps", "sell_slippage_bps", "stop_slippage_bps", "priority_fee_lamports"):
        cfg[k] = int(cfg[k])
    if isinstance(raw.get("rpc_url"), str) and raw["rpc_url"].startswith("https://"):
        cfg["rpc_url"] = raw["rpc_url"]
    try:
        cfg["sell_all"] = int(raw.get("sell_all") or 0)
    except (TypeError, ValueError):
        pass
    chosen = raw.get("strategies")
    cfg["strategies"] = tuple(dict.fromkeys(x for x in (chosen if isinstance(chosen, list) else [])
                                            if isinstance(x, str) and 0 < len(x) <= 100))[:40]
    asks = raw.get("sell_ids")
    cfg["sell_ids"] = tuple((a[0], a[1]) for a in (asks if isinstance(asks, list) else []) if isinstance(a, list) and len(a) == 2
                            and all(isinstance(x, int) and not isinstance(x, bool) and 0 < x < 2 ** 62 for x in a))
    return cfg


def load_cfg(data):
    try:
        with open(os.path.join(data, "settings", "trading.json"), encoding="utf-8") as f:
            return clean_cfg(json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def trade_strategies(data, chosen=()):
    """({id: strategy} to trade, every strategy that could be chosen, a note when the choice is not
    usable). The strategies come from the watch's own file; with nothing chosen, or none of the chosen
    ones still recorded, they are the ones shown on the Record live tab (ticked "Record live")."""
    try:
        with open(os.path.join(data, "watch.json"), encoding="utf-8") as f:
            w = json.load(f)
        shown, every = w["strategies"], w.get("tradable", w["strategies"])
    except Exception:
        return {}, [], ""
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)   # noqa: E731
    ok = lambda s: (isinstance(s, dict) and isinstance(s.get("id"), str) and isinstance(s.get("name"), str)   # noqa: E731
                    and all(num(s.get(k)) for k in ("tp", "sl", "hold")) and s["tp"] > 0 and 0 < s["sl"] < 1 and 0 < s["hold"] < 10000)
    shown = [s for s in (shown if isinstance(shown, list) else []) if ok(s)]
    every = [s for s in (every if isinstance(every, list) else []) if ok(s)] or shown
    shown_ids = {s["id"] for s in shown}
    available = [{"id": s["id"], "name": s["name"], "record_live": s["id"] in shown_ids, "entry": s.get("entry"), "tp": s["tp"],
                  "sl": s["sl"], "hold": s["hold"], "backtest": s.get("backtest") if isinstance(s.get("backtest"), dict) else None}
                 for s in every]
    picked = {s["id"]: s for s in every if s["id"] in chosen}
    if chosen and not picked:
        return ({s["id"]: s for s in shown}, available,
                "none of the chosen strategies is recorded any more, so the ones ticked Record live are traded")
    return picked or {s["id"]: s for s in shown}, available, ""


def last_value_of(db, tid):
    """What the position was last worth by a price check (0 when never priced)."""
    v = db.execute("SELECT last_value FROM trades WHERE id = ?", (tid,)).fetchone()
    return v[0] if v and v[0] is not None else 0.0


def new_signals(data, since_ms):
    """(mint, symbol, strategy, decided_ts, entry minute end) of live buys made since since_ms."""
    try:
        with closing(sqlite3.connect(os.path.join(data, "watch.db"), timeout=30)) as w:
            return w.execute("SELECT d.mint, json_extract(t.info, '$.symbol'), d.strategy, d.decided_ts, "
                             "t.graduated_ts / 60000 * 60000 + d.entry * 60000 FROM decisions d JOIN tokens t USING (mint) "
                             "WHERE d.pick = 1 AND d.late = 0 AND d.decided_ts > ?", (since_ms,)).fetchall()
    except sqlite3.Error:
        return []


def tell(settings, text):
    """A Telegram message in the background: a slow Telegram never holds up an order."""
    def go():
        try:
            cfg = notify.load(settings)
            if notify.ready(cfg):
                notify.send(cfg, text)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def ms():
    return int(time.time() * 1000)


class Trader:
    def __init__(self, data):
        self.data, self.settings = data, os.path.join(data, "settings")
        self.path = os.path.join(data, "trades.db")
        db = self.db()
        db.executescript(SCHEMA)
        have = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
        for col, kind in COLUMNS.items():
            if col not in have:
                db.execute(f"ALTER TABLE trades ADD COLUMN {col} {kind}")
        row = db.execute("SELECT v FROM meta WHERE k = 'sell_all'").fetchone()
        self.handled_sell_all = int(row[0]) if row else 0
        row = db.execute("SELECT v FROM meta WHERE k = 'sell_one'").fetchone()
        self.handled_sell_one = int(row[0]) if row else 0
        self.done_sell_one = set()
        db.close()
        self.wallet, self.wallet_error, self.busy = None, "", set()   # busy: trade ids with an order in progress
        self.lock, self.alerted, self.next_check = threading.Lock(), set(), {}
        self.next_wallet_sync, self.wallet_syncing = 0.0, False
        self.price_try, self.price_logged = {}, {}   # trade id -> last price check attempt / last logged failure
        self.buy_lock, self.reserved, self.reserved_n = threading.Lock(), 0.0, 0   # SOL taken by live buys still in flight

    def db(self):
        """A connection that commits every statement at once: no write lock is ever held while
        waiting on the network."""
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def say(self, mode, text):
        tell(self.settings, f"{'🟢 LIVE' if mode == 'live' else '🧪 DRY RUN'} {text}")

    def alert_once(self, key, text):
        if key not in self.alerted:
            self.alerted.add(key)
            tell(self.settings, "⚠️ " + text)

    # ---- startup: orders cut short by a restart ----
    def recover(self, cfg=None):   # cfg: accepted for older callers, not needed
        db = self.db()
        db.execute("UPDATE trades SET status = 'failed', error = 'restart before the order was sent' "
                   "WHERE status = 'buying'")                                  # never signed or sent
        db.execute("UPDATE trades SET status = 'open' WHERE status = 'selling'")  # not sent: still held
        db.execute("UPDATE trades SET status = 'failed' WHERE mode = 'dry' AND status = 'buy_sent'")
        db.execute("UPDATE trades SET status = 'open' WHERE mode = 'dry' AND status = 'sell_sent'")
        # sent but not known: decided by the transaction's own signature
        db.execute("UPDATE trades SET status = 'check', check_kind = 'buy', error = 'restart while the buy was in flight' "
                   "WHERE status = 'buy_sent'")
        db.execute("UPDATE trades SET status = 'check', check_kind = 'sell', error = 'restart while the sale was in flight' "
                   "WHERE status = 'sell_sent'")
        db.close()

    # ---- an order whose outcome is not known: settled by its signature, every round until it is ----
    def settle(self, cfg, db, tid, kind, mint, symbol, sig, last_valid):
        if not sig:
            db.execute("UPDATE trades SET status = ? WHERE id = ?", ("failed" if kind == "buy" else "open", tid))
            return
        fate = tx_status(cfg, sig, last_valid)
        if fate == "pending" and not last_valid:
            # no expiry known for this order (should not happen): after two minutes the wallet decides
            since = db.execute("SELECT COALESCE(sell_trigger_ts, opened_ts, signal_ts) FROM trades WHERE id = ?", (tid,)).fetchone()[0]
            if since and ms() - since > 120000:
                held = sum(a[2] for a in token_accounts(cfg, self.wallet.pubkey, mint))
                own = db.execute("SELECT COALESCE(tokens, buy_quote_tokens) FROM trades WHERE id = ?", (tid,)).fetchone()[0]
                if held and own:   # never more than this order's own tokens: the rest may be someone else's
                    db.execute("UPDATE trades SET status = 'open', tokens = ?, sol_in = COALESCE(sol_in, ?), error = ? WHERE id = ?",
                               (min(held, own), cfg["sol_per_trade"], "outcome unknown: its tokens found in the wallet, managed again", tid))
                elif held:
                    db.execute("UPDATE trades SET status = 'stuck', error = 'outcome unknown and the amount is not known: check by hand' "
                               "WHERE id = ?", (tid,))
                    self.alert_once(f"unknown{tid}", f"An order for {notify.esc(symbol or mint[:8])} could not be confirmed. Check the wallet by hand.")
                else:
                    db.execute("UPDATE trades SET status = ?, sol_out = ?, closed_ts = ?, error = ? WHERE id = ?",
                               ("failed" if kind == "buy" else "closed", None if kind == "buy" else 0, ms(),
                                "outcome unknown: no tokens in the wallet", tid))
            return
        if fate == "pending":
            return
        if kind == "buy":
            if fate == "landed":
                got, sol, fee = tx_effect(cfg, sig, self.wallet.pubkey, mint)
                if got > 0:
                    db.execute("UPDATE trades SET status = 'open', opened_ts = COALESCE(opened_ts, ?), sol_in = ?, tokens = ?, "
                               "fee_sol = ?, error = 'confirmed by its signature after a delay' WHERE id = ?",
                               (ms(), -sol, got, fee, tid))
                    self.say("live", f"buy of {notify.esc(symbol or mint[:8])} confirmed late: now managed")
                    return
            db.execute("UPDATE trades SET status = 'failed', error = ? WHERE id = ?", (f"buy {fate}: nothing bought", tid))
            return
        if fate == "landed":
            _, sol, fee = tx_effect(cfg, sig, self.wallet.pubkey, mint)
            db.execute("UPDATE trades SET status = 'closed', sol_out = ?, closed_ts = ?, "
                       "fee_sol = COALESCE(fee_sol, 0) + ?, error = 'sale confirmed by its signature after a delay' WHERE id = ?",
                       (sol, ms(), fee, tid))
            # the emptied token account gives its rent back, as after a sale confirmed at once; in its
            # own thread, since waiting for that to land must not hold up the stops of other positions
            threading.Thread(target=self.close_late, args=(cfg, tid, mint), daemon=True).start()
        else:   # the sale did nothing: the tokens are still held and sold by the rules again
            self.sale_failed(db, tid, symbol or mint[:8], f"sale {fate}")

    def close_late(self, cfg, tid, mint):
        try:
            if sum(a[2] for a in token_accounts(cfg, self.wallet.pubkey, mint)) == 0:
                rent = close_empty_accounts(cfg, self.wallet, mint)
                if rent:
                    db = self.db()
                    try:
                        db.execute("UPDATE trades SET rent_back_sol = ?, sol_out = sol_out + ? WHERE id = ?", (rent, rent, tid))
                    finally:
                        db.close()
        except Exception:
            pass

    def sale_retry(self, db, tid, why, pause_ms=10000):
        """A sale that did not happen for a reason outside the chain (RPC or Jupiter busy, no
        route for a moment): back to open, tried again shortly, never counted as a failure.
        If it still has not gone out 5 minutes after it was first wanted, Telegram is told."""
        now = ms()   # attempts more than a minute apart are not one run (the sale was not wanted in between)
        db.execute("UPDATE trades SET status = 'open', next_sell_ts = ?, error = ?, retry_since = CASE WHEN retry_since IS NULL "
                   "OR next_sell_ts IS NULL OR next_sell_ts < ? THEN ? ELSE retry_since END "
                   "WHERE id = ? AND status IN ('selling', 'sell_sent', 'check')", (now + pause_ms, why[:500], now - 60000, now, tid))
        r = db.execute("SELECT retry_since, symbol, mint FROM trades WHERE id = ?", (tid,)).fetchone()
        if r and r[0] and ms() - r[0] > RETRY_ALERT_MS:
            self.alert_once(f"retry{tid}", f"{notify.esc(r[1] or r[2][:8])}: the sale has not gone out for 5 minutes and is "
                                           f"still being tried. Last error: {notify.esc(why[:200])}. Check it, or sell it by hand.")

    def sale_failed(self, db, tid, name, why):
        """Back to open with a growing pause; after MAX_SELL_FAILS the position is set aside as
        'stuck', alerted once, and still tried again every 5 minutes."""
        n = (db.execute("SELECT sell_fails FROM trades WHERE id = ?", (tid,)).fetchone()[0] or 0) + 1
        if n >= MAX_SELL_FAILS:
            db.execute("UPDATE trades SET status = 'stuck', sell_fails = ?, next_sell_ts = ?, error = ? WHERE id = ?",
                       (n, ms() + STUCK_RETRY_MS, f"{why}; {n} failed sales in a row: tried again every 5 minutes, or sell it by hand", tid))
            self.alert_once(f"stuck{tid}", f"{notify.esc(name)} could not be sold {n} times in a row ({notify.esc(why)}). "
                                           "It is left in the wallet: sell it by hand.")
        else:
            db.execute("UPDATE trades SET status = 'open', sell_fails = ?, next_sell_ts = ?, error = ? WHERE id = ?",
                       (n, ms() + min(5000 * 2 ** n, 120000), why, tid))

    # ---- orders, each in its own thread ----
    def buy(self, tid, mode, cfg, mint, symbol, strategy, entry_end):
        db, t0, sent, reserved = self.db(), time.time(), False, False
        try:
            lamports = int(cfg["sol_per_trade"] * LAMPORTS)
            q = quote(SOL, mint, lamports, cfg["slippage_bps"])
            t_quote = time.time()
            db.execute("UPDATE trades SET lat_quote_ms = ?, buy_quote_tokens = ? WHERE id = ?",
                       (int((t_quote - t0) * 1000), int(q["outAmount"]), tid))
            if mode == "dry":
                db.execute("UPDATE trades SET status = 'open', opened_ts = ?, sol_in = ?, tokens = ?, lat_total_ms = ? "
                           "WHERE id = ?", (ms(), cfg["sol_per_trade"], int(q["outAmount"]), ms() - entry_end, tid))
                self.say(mode, f"<b>bought</b> {notify.esc(symbol or mint[:8])} for {cfg['sol_per_trade']:g} SOL ({notify.esc(strategy)})")
                return
            with self.buy_lock:
                # positions that hold or may hold tokens, plus buys past this check and not yet sent
                held_live = db.execute(f"SELECT COUNT(*) FROM trades WHERE mode = 'live' AND id != ? AND status IN "
                                       f"({','.join('?' * (len(HELD_STATES) - 1))})",
                                       (tid, *[x for x in HELD_STATES if x != "buying"])).fetchone()[0] + self.reserved_n
                checking = db.execute("SELECT COALESCE(SUM(?), 0) FROM trades WHERE mode = 'live' AND id != ? AND "
                                      "status = 'check' AND check_kind = 'buy'", (cfg["sol_per_trade"], tid)).fetchone()[0]
                per = max(RESERVE_PER_POSITION, 0.006 + 2 * cfg["priority_fee_lamports"] / 1e9)
                need = cfg["sol_per_trade"] + self.reserved + checking + per * (held_live + 1)
                if sol_balance(cfg, self.wallet.pubkey) < need:
                    raise RuntimeError(f"not enough SOL in the wallet (needs {need:.3f} with fees for every open position's sale)")
                self.reserved += cfg["sol_per_trade"]
                self.reserved_n += 1
                reserved = True
            signed, sig, last_valid = build_swap(cfg, self.wallet, q)
            t_built = time.time()
            # recorded under its signature before it is sent: from here on it is never assumed to have failed
            db.execute("UPDATE trades SET status = 'buy_sent', buy_sig = ?, buy_last_valid = ?, lat_build_ms = ? WHERE id = ?",
                       (sig, last_valid, int((t_built - t_quote) * 1000), tid))
            sent = True
            try:
                send(cfg, signed)
            except Exception:
                pass        # it may still have reached the network: its signature decides
            fate = wait_landed(cfg, sig, last_valid, signed)
            if fate == "unknown":
                db.execute("UPDATE trades SET status = 'check', check_kind = 'buy', error = 'no answer yet: checked by signature' "
                           "WHERE id = ?", (tid,))
                return
            if fate != "landed":
                db.execute("UPDATE trades SET status = 'failed', error = ? WHERE id = ?",
                           ("the buy failed on chain: nothing bought" if fate == "failed" else
                            "the buy did not land before its blockhash expired: nothing bought", tid))
                return
            got, sol, fee = tx_effect_patiently(cfg, sig, self.wallet.pubkey, mint)
            if got <= 0:
                db.execute("UPDATE trades SET status = 'failed', error = 'the buy landed but no tokens arrived' WHERE id = ?", (tid,))
                return
            db.execute("UPDATE trades SET status = 'open', opened_ts = ?, sol_in = ?, tokens = ?, fee_sol = ?, "
                       "lat_land_ms = ?, lat_total_ms = ? WHERE id = ?",
                       (ms(), -sol, got, fee, int((time.time() - t_built) * 1000), ms() - entry_end, tid))
            self.say(mode, f"<b>bought</b> {notify.esc(symbol or mint[:8])} for {-sol:.4f} SOL ({notify.esc(strategy)}), "
                           f"{(ms() - entry_end) / 1000:.1f} s after the signal\n<a href=\"https://solscan.io/tx/{sig}\">transaction</a>")
        except Exception as e:
            if sent:    # sent: the chain decides, not this error
                db.execute("UPDATE trades SET status = 'check', check_kind = 'buy', error = ? WHERE id = ?",
                           (f"checked by signature after: {e!r}"[:500], tid))
            else:
                db.execute("UPDATE trades SET status = 'failed', error = ? WHERE id = ?", (repr(e)[:500], tid))
                self.say(mode, f"⚠️ buy of {notify.esc(symbol or mint[:8])} failed: {notify.esc(repr(e)[:200])}")
        finally:
            db.close()
            with self.lock:
                self.busy.discard(tid)
            if reserved:
                with self.buy_lock:
                    self.reserved -= cfg["sol_per_trade"]
                    self.reserved_n -= 1

    def sell(self, tid, mode, cfg, mint, symbol, sol_in, tokens, reason, trigger_ts, quoted):
        db, sent, name = self.db(), False, symbol or mint[:8]
        try:
            slip = cfg["stop_slippage_bps"] if reason in ("stop loss", "sell all", "sold by hand", "time stop") else cfg["sell_slippage_bps"]
            if mode == "dry":
                out = quoted
                db.execute("UPDATE trades SET status = 'closed', sol_out = ?, closed_ts = ?, reason = ?, "
                           "sell_trigger_ts = ?, sell_quote_sol = ?, lat_sell_ms = ?, error = NULL WHERE id = ?",
                           (out, ms(), reason, trigger_ts, quoted, ms() - trigger_ts, tid))
            else:
                # only this position's tokens, and never more than the wallet really holds
                held = sum(a[2] for a in token_accounts(cfg, self.wallet.pubkey, mint))
                amount = min(tokens, held)
                if held > 0:
                    db.execute("UPDATE trades SET empty_reads = 0 WHERE id = ?", (tid,))
                if amount <= 0:
                    # a node that is behind can answer "no tokens": only three empty reads in a row,
                    # 30 s apart and within 3 minutes, close it
                    n, last = db.execute("SELECT empty_reads, last_empty_ts FROM trades WHERE id = ?", (tid,)).fetchone()
                    n = (n or 0) + 1 if last and ms() - last < 180000 else 1
                    if n >= 3:
                        db.execute("UPDATE trades SET status = 'closed', sol_out = 0, closed_ts = ?, reason = ?, empty_reads = ?, error = ? "
                                   "WHERE id = ?", (ms(), reason, n, "the wallet holds none of this token (sold elsewhere?)", tid))
                        self.alert_once(f"gone{tid}", f"{notify.esc(name)}: the wallet no longer holds this token, so the position "
                                                      "was closed without a sale. Check the wallet.")
                    else:
                        db.execute("UPDATE trades SET empty_reads = ?, last_empty_ts = ? WHERE id = ?", (n, ms(), tid))
                        self.sale_retry(db, tid, "the wallet showed no tokens: read again shortly", 30000)
                    return
                q = quote(mint, SOL, amount, slip)
                signed, sig, last_valid = build_swap(cfg, self.wallet, q)
                db.execute("UPDATE trades SET status = 'sell_sent', retry_since = NULL, sell_sig = ?, sell_last_valid = ?, sell_trigger_ts = ?, "
                           "sell_quote_sol = ?, reason = ? WHERE id = ?", (sig, last_valid, trigger_ts, quoted, reason, tid))
                sent = True
                try:
                    send(cfg, signed)
                except Exception:
                    pass
                fate = wait_landed(cfg, sig, last_valid, signed)
                if fate == "unknown":
                    db.execute("UPDATE trades SET status = 'check', check_kind = 'sell', error = 'no answer yet: checked by signature' "
                               "WHERE id = ?", (tid,))
                    return
                if fate != "landed":
                    self.sale_failed(db, tid, name, "the sale failed on chain" if fate == "failed" else "the sale expired without landing")
                    return
                _, sol, fee = tx_effect_patiently(cfg, sig, self.wallet.pubkey, mint)
                rent = 0.0
                try:
                    if sum(a[2] for a in token_accounts(cfg, self.wallet.pubkey, mint)) == 0:
                        rent = close_empty_accounts(cfg, self.wallet, mint)
                except Exception:
                    pass
                out = sol + rent
                db.execute("UPDATE trades SET status = 'closed', sol_out = ?, closed_ts = ?, reason = ?, rent_back_sol = ?, "
                           "fee_sol = COALESCE(fee_sol, 0) + ?, lat_sell_ms = ?, error = NULL WHERE id = ?",
                           (out, ms(), reason, rent, fee, ms() - trigger_ts, tid))
            r = out / sol_in - 1 if sol_in else 0
            self.say(mode, f"{'✅' if r > 0 else '🔴'} <b>sold</b> {notify.esc(name)}: {r * 100:+.1f}% "
                           f"({out - sol_in:+.4f} SOL) by {reason}, {(ms() - trigger_ts) / 1000:.1f} s after the trigger")
        except Exception as e:
            if sent:
                db.execute("UPDATE trades SET status = 'check', check_kind = 'sell', error = ? WHERE id = ?",
                           (f"checked by signature after: {e!r}"[:500], tid))
            elif isinstance(e, SimError):   # the swap itself would fail: counts towards giving up
                self.sale_failed(db, tid, name, str(e)[:300])
            else:                           # nothing sent, a passing problem: tried again shortly
                self.sale_retry(db, tid, f"sale not sent, tried again: {e!r}")
        finally:
            db.close()
            with self.lock:
                self.busy.discard(tid)

    def start(self, tid, target, *args):
        with self.lock:
            if tid in self.busy:
                return
            self.busy.add(tid)
        threading.Thread(target=target, args=(tid, *args), daemon=True).start()

    # ---- one round of the main loop ----
    def consider(self, db, cfg, mode, lost, s, mint, symbol, sid, decided, entry_end):
        """Buy on one fresh signal of strategy s, unless a limit says no."""
        seen = db.execute("SELECT id, status, mode, error FROM trades WHERE mint = ? AND strategy = ?", (mint, sid)).fetchone()
        if seen:
            # only a dry buy that never got a quote is tried again; anything else is final
            if not (seen[1] == "failed" and seen[2] == "dry" and ("HTTPError" in (seen[3] or "") or "URLError" in (seen[3] or ""))):
                return
            db.execute("DELETE FROM trades WHERE id = ?", (seen[0],))
        held = f"({','.join('?' * len(HELD_STATES))})"
        if mode == "live" and db.execute(f"SELECT 1 FROM trades WHERE mint = ? AND mode = 'live' AND status IN {held}",
                                         (mint, *HELD_STATES)).fetchone():
            return    # one live position per token: a sale must never touch another position's tokens
        if db.execute(f"SELECT COUNT(*) FROM trades WHERE mode = ? AND status IN {held}", (mode, *HELD_STATES)).fetchone()[0] >= cfg["max_open"]:
            return
        if lost <= -abs(cfg["daily_loss_limit_sol"]):
            return
        # a "sell all" asked while this buy is on its way applies to it too
        cur = db.execute("INSERT INTO trades (mint, symbol, strategy, mode, signal_ts, entry_ts, status, tp, sl, hold_until, "
                         "lat_seen_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (mint, symbol, sid, mode, decided, entry_end, "buying", s["tp"], s["sl"],
                          int(entry_end + s["hold"] * MIN), ms() - entry_end))
        self.start(cur.lastrowid, self.buy, mode, cfg, mint, symbol, s["name"], entry_end)

    def sync_wallet(self, cfg):
        """Add the wallet's new transactions to wallet_tx, oldest first, stopping at the first that
        cannot be read yet (so none is ever skipped). Runs in its own thread."""
        db = None
        try:
            db = self.db()
            newest = db.execute("SELECT sig FROM wallet_tx ORDER BY slot DESC, ts DESC LIMIT 1").fetchone()
            sigs, before = [], None
            for _ in range(20):   # pages of 100, newest first
                opts = {"limit": 100, "commitment": "confirmed", **({"until": newest[0]} if newest else {}),
                        **({"before": before} if before else {})}
                page = rpc(cfg, "getSignaturesForAddress", [self.wallet.pubkey, opts]) or []
                sigs += page
                if len(page) < 100:
                    break
                before = page[-1]["signature"]
            for row in reversed(sigs):
                if self.busy:
                    break       # an order is in flight: the free RPC's requests are left to it
                time.sleep(0.5)  # spread out: the free public RPC refuses bursts
                tx = rpc(cfg, "getTransaction", [row["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                    "maxSupportedTransactionVersion": 0}])
                if not tx:
                    break
                keys = tx["transaction"]["message"]["accountKeys"]
                i = next((k for k, a in enumerate(keys) if a.get("pubkey") == self.wallet.pubkey), None)
                if i is None:
                    continue
                db.execute("INSERT OR IGNORE INTO wallet_tx VALUES (?,?,?,?,?,?)",
                           (row["signature"], tx.get("slot"), (tx.get("blockTime") or 0) * 1000,
                            tx["meta"]["preBalances"][i] / LAMPORTS, tx["meta"]["postBalances"][i] / LAMPORTS,
                            int(bool(keys[i].get("signer")))))
        except Exception as e:
            print(f"wallet history: {e!r}", flush=True)
        finally:
            if db is not None:
                db.close()
            self.wallet_syncing = False

    def wallet_history(self, db):
        """[[unix ms, SOL]] from just before the wallet's first trade (its first signed transaction)
        to its latest transaction; [] before any trade."""
        rows = db.execute("SELECT ts, pre, post, signer FROM wallet_tx ORDER BY slot, ts").fetchall()
        first = next((k for k, r in enumerate(rows) if r[3]), None)
        if first is None:
            return []
        return [[rows[first][0] - 1, rows[first][1]]] + [[r[0], r[2]] for r in rows[first:]]

    def round(self):
        cfg, now = load_cfg(self.data), time.time()
        db = self.db()
        live_rows = db.execute(f"SELECT COUNT(*) FROM trades WHERE mode = 'live' AND status IN "
                               f"({','.join('?' * len(HELD_STATES))})", HELD_STATES).fetchone()[0]
        # the wallet is loaded whenever its file is there, so the Trading tab shows its address and
        # balance in every mode; it signs only in live mode, or to sell positions bought live
        key = os.path.join(self.settings, "wallet.json")
        if self.wallet is None and (cfg["mode"] == "live" or live_rows or os.path.exists(key)):
            try:
                self.wallet, self.wallet_error = Wallet(key), ""
            except Exception as e:      # no key file, a readable one, a malformed one, or solders missing
                self.wallet_error = f"the wallet could not be loaded: {e!r}"
                if live_rows:
                    self.alert_once("wallet", f"Live positions are held but the wallet cannot be loaded: {notify.esc(repr(e)[:200])}")
        mode = cfg["mode"] if cfg["mode"] != "live" or self.wallet else "off"
        public_rpc = PUBLIC_RPC in cfg["rpc_url"]

        # 1. orders whose outcome is not known yet, settled by signature
        if self.wallet:
            for tid, kind, mint, symbol, bsig, ssig, blv, slv in db.execute(
                    "SELECT id, check_kind, mint, symbol, buy_sig, sell_sig, buy_last_valid, sell_last_valid FROM trades "
                    "WHERE status = 'check' AND mode = 'live'").fetchall():
                if tid in self.busy or self.next_check.get(tid, 0) > now:
                    continue
                self.next_check[tid] = now + 5
                try:
                    self.settle(cfg, db, tid, kind or "buy", mint, symbol, bsig if kind != "sell" else ssig,
                                blv if kind != "sell" else slv)
                except Exception as e:
                    db.execute("UPDATE trades SET error = ? WHERE id = ?", (f"checking by signature: {e!r}"[:500], tid))
                if db.execute("SELECT status FROM trades WHERE id = ?", (tid,)).fetchone()[0] == "check" and \
                        now * 1000 - (db.execute("SELECT COALESCE(sell_trigger_ts, opened_ts, signal_ts) FROM trades WHERE id = ?",
                                                 (tid,)).fetchone()[0] or now * 1000) > 300000:
                    self.alert_once(f"check{tid}", f"The outcome of an order for {notify.esc(symbol or mint[:8])} is still "
                                                   "not known after 5 minutes. Check the wallet on Solscan.")

        # 2. open positions: sell at the target, the stop, the time limit, or on "sell all"
        if cfg["sell_all"] > self.handled_sell_all:
            # every position, including one still being bought or checked, is sold as soon as it can be
            db.execute(f"UPDATE trades SET sell_asked = 1 WHERE status IN ({','.join('?' * len(HELD_STATES))})", HELD_STATES)
            self.handled_sell_all = cfg["sell_all"]
            db.execute("INSERT OR REPLACE INTO meta VALUES ('sell_all', ?)", (str(self.handled_sell_all),))
        # one position sold by hand from the Trading tab: at once, even during a pause after failed
        # sales, and a position set aside as stuck is tried again
        for tid, asked_ts in cfg["sell_ids"]:
            # remembered one by one; up to 10 minutes older than the newest handled still counts (a clock
            # set back), and re-asking a sale after a restart is harmless
            if asked_ts < self.handled_sell_one - 600000 or (tid, asked_ts) in self.done_sell_one:
                continue
            self.done_sell_one.add((tid, asked_ts))
            try:   # a bad request is dropped: it must never keep the stops below from running
                db.execute(f"UPDATE trades SET sell_asked = 2, next_sell_ts = NULL, status = CASE WHEN status = 'stuck' AND "
                           f"sol_in IS NOT NULL AND tokens > 0 THEN 'open' ELSE status END WHERE id = ? AND status IN "
                           f"({','.join('?' * len(HELD_STATES))}) AND signal_ts < ?", (tid, *HELD_STATES, asked_ts))
            except Exception as e:
                print(f"sell request {tid} dropped: {e!r}", flush=True)
            self.handled_sell_one = max(self.handled_sell_one, asked_ts)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('sell_one', ?)", (str(self.handled_sell_one),))
        db.execute("UPDATE trades SET status = 'open', sell_fails = ? WHERE status = 'stuck' AND sol_in IS NOT NULL AND tokens > 0 "
                   "AND COALESCE(next_sell_ts, 0) <= ?", (MAX_SELL_FAILS - 1, ms()))
        positions = db.execute(
            "SELECT id, mint, symbol, mode, sol_in, tokens, tp, sl, hold_until, last_checked, sell_asked, next_sell_ts, "
            "opened_ts FROM trades WHERE status = 'open'").fetchall()
        open_ids = {r[0] for r in positions}
        for d in (self.price_try, self.price_logged):
            for k in [k for k in d if k not in open_ids]:
                d.pop(k)
        # the position that has waited longest for a price goes first, so none is ever left out
        positions.sort(key=lambda r: self.price_try.get(r[0], 0))
        gap = max(CHECK_EVERY, JUP_WINDOW / JUP_FOR_CHECKS * len(positions))
        for tid, mint, symbol, pmode, sol_in, tokens, tp, sl, until, checked, asked, next_sell, opened in positions:
            if tid in self.busy or sol_in is None or not tokens:
                continue
            if pmode == "live" and self.wallet is None:
                continue
            # a sale asked by hand or by "sell all", or a time stop that has come, needs no price:
            # the sale gets its own quote (a price check that keeps failing must never hold these up)
            if (asked or now * 1000 >= until) and not (next_sell and now * 1000 < next_sell):
                reason = "sold by hand" if asked == 2 else "sell all" if asked else "time stop"
                db.execute("UPDATE trades SET status = 'selling' WHERE id = ?", (tid,))
                self.start(tid, self.sell, pmode, cfg, mint, symbol, sol_in, tokens, reason, ms(), last_value_of(db, tid))
                continue
            # no price for 2 minutes, whatever the reason (failing, or no room to ask): its stop loss is not working
            if now * 1000 - (checked or opened or now * 1000) > 120000:
                self.alert_once(f"price{tid}", f"{notify.esc(symbol or mint[:8])} has had no price for 2 minutes, "
                                               "so its stop loss cannot work. Check it by hand.")
            # price checks share Jupiter's small allowance: each position every `gap` s at most,
            # spread out when several are open, and only while real orders have room
            if now - self.price_try.get(tid, 0) < gap or jup_recent() >= JUP_FOR_CHECKS:
                continue
            self.price_try[tid] = now
            try:
                value = int(quote(mint, SOL, tokens, cfg["sell_slippage_bps"], tries=1)["outAmount"]) / LAMPORTS
            except Exception as e:
                db.execute("UPDATE trades SET error = ? WHERE id = ?", (f"price check: {e!r}"[:500], tid))
                if now - self.price_logged.get(tid, 0) > 60:
                    self.price_logged[tid] = now
                    print(f"price check {symbol or mint[:8]}: {e!r}", flush=True)
                continue
            db.execute("UPDATE trades SET last_value = ?, last_checked = ?, error = NULL WHERE id = ?", (value, ms(), tid))
            if next_sell and now * 1000 < next_sell:
                continue        # pausing after failed sales
            # the cost the levels are measured against: what was paid, less the token account's
            # rent, which comes back when the account is closed after the sale
            cost = sol_in - (RENT if pmode == "live" else 0)
            eps = 1e-9 * cost
            reason = ("take profit" if value >= cost * (1 + tp) - eps else "stop loss" if value <= cost * (1 - sl) + eps else None)
            if reason:
                db.execute("UPDATE trades SET status = 'selling' WHERE id = ?", (tid,))
                self.start(tid, self.sell, pmode, cfg, mint, symbol, sol_in, tokens, reason, ms(), value)

        # 3. then buy on fresh live signals
        strategies, available, note = trade_strategies(self.data, cfg["strategies"])
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        closed = db.execute("SELECT COALESCE(SUM(sol_out - sol_in), 0) FROM trades WHERE status = 'closed' AND mode = ? "
                            "AND date(closed_ts / 1000, 'unixepoch') = ?", (mode, today)).fetchone()[0]
        unrealised = db.execute("SELECT COALESCE(SUM(MIN(last_value - sol_in, 0)), 0) FROM trades WHERE status IN "
                                "('open', 'selling', 'sell_sent', 'check', 'stuck') AND mode = ? AND last_value IS NOT NULL",
                                (mode,)).fetchone()[0]
        lost = closed + min(unrealised, 0)
        blocked = ""   # the user chose the free public RPC: orders also go out through a second free RPC
        for mint, symbol, sid, decided, entry_end in new_signals(self.data, int((now - SIGNAL_FOR) * 1000)):
            if mode == "off" or blocked or sid not in strategies:
                continue
            try:
                self.consider(db, cfg, mode, lost, strategies[sid], mint, symbol, sid, decided, entry_end)
            except Exception as e:
                print(f"signal {mint} {sid} skipped: {e!r}", flush=True)

        # 4. what the Trading tab shows
        cols = ("id", "mint", "symbol", "strategy", "mode", "signal_ts", "entry_ts", "opened_ts", "buy_sig", "sol_in", "status",
                "last_value", "sell_sig", "sol_out", "closed_ts", "reason", "error", "lat_seen_ms", "lat_quote_ms",
                "lat_build_ms", "lat_land_ms", "lat_total_ms", "lat_sell_ms", "buy_quote_tokens", "tokens", "sell_quote_sol",
                "rent_back_sol", "fee_sol", "sell_fails", "sell_asked")
        rows = [dict(zip(cols, r)) for r in db.execute(f"SELECT {', '.join(cols)} FROM trades ORDER BY id DESC LIMIT 200")]
        state = {"generated_at": ms(), "mode": mode, "wanted_mode": cfg["mode"],
                 "error": "; ".join(x for x in (self.wallet_error, blocked, note) if x),
                 "strategies_available": available, "strategies_traded": [{"id": k, "name": s["name"]} for k, s in strategies.items()],
                 "wallet": self.wallet.pubkey if self.wallet else None, "live_ready": self.wallet is not None,
                 "settings": {k: cfg[k] for k in DEFAULTS if k not in ("rpc_url", "sell_all", "sell_ids")},
                 "rpc_is_public": public_rpc, "hard_max_sol": HARD_MAX_SOL, "today_result_sol": lost, "trades": rows}
        if self.wallet and (int(now) % 20 < 2 or getattr(self, "balance", None) is None):
            try:
                self.balance = sol_balance(cfg, self.wallet.pubkey)
            except Exception:
                pass
        if self.wallet:
            state["balance_sol"] = getattr(self, "balance", None)
            state["wallet_history"] = self.wallet_history(db)
            # what the open live positions would bring if sold now, for the chart's last point
            state["open_value_sol"] = db.execute("SELECT COALESCE(SUM(last_value), 0) FROM trades WHERE mode = 'live' "
                                                 f"AND status IN ({','.join('?' * len(HELD_STATES))})", HELD_STATES).fetchone()[0]
            if not self.wallet_syncing and now >= self.next_wallet_sync and not self.busy:
                self.wallet_syncing, self.next_wallet_sync = True, now + 60
                threading.Thread(target=self.sync_wallet, args=(cfg,), daemon=True).start()
        tmp = os.path.join(self.data, "trading_state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, os.path.join(self.data, "trading_state.json"))
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default="data")
    args = ap.parse_args()
    t = Trader(args.data)
    t.recover()
    print("trader started", flush=True)
    rounds, slowest, next_beat = 0, 0.0, time.time() + 600
    while True:
        # watchdog: a round that has not finished within WATCHDOG seconds means something is stuck,
        # and open positions would go unwatched. Every thread's stack goes to the log and the
        # process stops; systemd starts it again and recover() takes the positions back.
        faulthandler.dump_traceback_later(WATCHDOG, exit=True)
        t0 = time.time()
        try:
            t.round()
        except Exception as e:      # a bad round must not stop the positions being managed
            print(f"  ! round: {e!r}", flush=True)
        rounds, slowest = rounds + 1, max(slowest, time.time() - t0)
        if time.time() >= next_beat:
            print(f"alive: {rounds} rounds in 10 min, slowest {slowest:.1f} s", flush=True)
            rounds, slowest, next_beat = 0, 0.0, time.time() + 600
        time.sleep(0.5)


if __name__ == "__main__":
    main()

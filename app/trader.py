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
import http.client
import json
import math
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

import notify

SOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
MANUAL, MANUAL_NAME = "crossing-manual", "Crossing (manual buy)"     # the strategy id and name of a buy asked by hand on the Crossing page
MAX_BUY_IMPACT = 0.07      # the default of the "max_buy_impact_pct" setting: a live buy is skipped when Jupiter's quote says it moves the price more than this (the backtest assumes at most 5%)
JUP = "https://lite-api.jup.ag/swap/v1"
MIN = 60000
SIGNAL_FOR = 60          # a buy signal older than this is not acted on
CHECK_EVERY = 1.5        # seconds between price checks of an open position
# Open positions are priced from their PumpSwap pool, read on chain every POOL_EVERY s (one batched call for all of them),
# so a stop or a target is seen within about a second; Jupiter, whose free tier allows only a few calls, is kept for the
# sales themselves and asked every CALIBRATE_EVERY s per position to calibrate the pool estimate (its fee).
POOL_EVERY, POOL_FRESH, CALIBRATE_EVERY = 1.0, 3.0, 30.0
POOL_FEE_GUESS = 0.0125  # before the first calibration: PumpSwap's highest fee tier, so a stop is seen early, never late
ROUND_SLEEP, STATE_EVERY = 0.15, 0.5   # the main loop's pause; the Trading tab's state file at most this often
LAND_FOR = 90            # longest wait for a transaction, whatever its blockhash says
WATCHDOG = 180           # seconds a round may take before the trader is restarted (normal: under 1 s)
HARD_MAX_SOL = 1.0       # no single order is ever larger than this, whatever the settings say
# a position exists (or may exist) in these states: they count for "one per token", "most open" and "sell all"
HELD_STATES = ("buying", "buy_sent", "open", "selling", "sell_sent", "check", "stuck")
MAX_SELL_FAILS = 5       # failed sales in a row before a position is left for a person
RENT = 0.00203928        # SOL held by a token account, returned when it is closed
SUPPLY_RAW = 1e15         # every pump.fun token: 1 billion tokens of 6 decimals, in raw units
RESERVE_PER_POSITION = 0.01   # SOL kept back per live position for the fees and temporary accounts of its sale
RETRY_ALERT_MS = 300000  # a sale still not out after this long is alerted
STUCK_RETRY_MS = 300000  # a position that could not be sold is tried again this often
DEFAULTS = {"mode": "off", "sol_per_trade": 0.1, "max_open": 2, "daily_loss_limit_sol": 1.0, "max_buy_impact_pct": round(MAX_BUY_IMPACT * 100, 4),
            "take_profit_pct": 50.0, "stop_loss_pct": 50.0,      # the exit of a buy made by hand on the Crossing page, in % (checked every round, prices about once a second)
            "slippage_bps": 500, "sell_slippage_bps": 1000, "stop_slippage_bps": 2500,
            "priority_fee_lamports": 300000, "rpc_url": "https://api.mainnet-beta.solana.com", "sell_all": 0,
            "sell_ids": (),    # positions asked to be sold by hand: (id, unix ms of the request)
            "strategies": ()}  # ids of the strategies to trade (switched ON on the Trading tab); none: nothing is bought
LIMITS = {"sol_per_trade": (0.01, HARD_MAX_SOL), "max_open": (1, 5), "daily_loss_limit_sol": (0.01, 10.0), "max_buy_impact_pct": (1.0, 20.0),
          "take_profit_pct": (5.0, 1000.0), "stop_loss_pct": (5.0, 95.0),
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
           "last_empty_ts": "INTEGER",     # when the wallet last showed none of the token
           "peak_value": "REAL",           # the highest last_value while held: the best the position was worth
           "exit_value": "REAL",
           "be": "REAL",                   # breakeven trigger (0.5 = +50%): once the position was worth that much more than its cost, the stop moves to the cost
           "be_stop": "REAL",              # where that stop moves to, as a gain on the cost (0.1 = +10%)
           "wallet": "TEXT",               # the wallet a live trade was made with (dry runs: none)
           "pool": "TEXT"}                 # the coin's PumpSwap pool, priced on chain while the position is open           # SOL the sale's swap brought (before the network fee and the rent back)


class WalletChanged(RuntimeError):
    """The wallet was deleted or switched on the Settings page while a buy was being prepared: nothing is sent."""


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


_side = ThreadPoolExecutor(max_workers=16)  # reads done alongside another call (balance with a quote, tokens with a quote)
_fallback_logged = [0.0]
_primary_down_until = [0.0]    # after the chosen RPC fails, the public one is used first for a while, so a hung primary costs one wait, not one per call


def rpc_primary(cfg, method, params):
    """One call to the chosen RPC only, no fallback: for frequent background reads (pool prices), so a busy primary
    never sends orders to the public RPC because of them."""
    r = http_json(cfg["rpc_url"], {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=5)
    if r.get("error"):
        raise RuntimeError(f"{method}: {r['error'].get('message')}")
    return r.get("result")


def rpc(cfg, method, params):
    """One JSON-RPC call to the chosen RPC; when that one cannot be reached (429, 5xx, timeout, dropped
    connection) the free public RPC is asked instead. An answer that is an RPC error (a failed
    transaction, an unsupported version) is real and is never retried elsewhere."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    urls = [cfg["rpc_url"]] + (["https://" + PUBLIC_RPC] if PUBLIC_RPC not in cfg["rpc_url"] else [])
    if len(urls) == 2 and time.time() < _primary_down_until[0]:
        urls.reverse()
    for n, url in enumerate(urls):
        try:
            r = retrying(lambda: http_json(url, body, timeout=20), 2 if n + 1 < len(urls) else 4)
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, http.client.HTTPException) as e:     # HTTPError is a URLError
            if url == cfg["rpc_url"]:
                _primary_down_until[0] = time.time() + 30
            if n + 1 == len(urls):
                raise
            if time.time() - _fallback_logged[0] > 60:
                _fallback_logged[0] = time.time()
                print(f"rpc {method}: the chosen RPC failed ({type(e).__name__}), using the public RPC", flush=True)
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
    if signed_b64 in _tipped:    # Sender's answer is not waited for: the RPC send and the status checks decide
        def to_sender():
            try:
                http_json(SENDER_URL, {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                       "params": [signed_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]}, timeout=5)
            except Exception:
                pass
        threading.Thread(target=to_sender, daemon=True).start()
    if PUBLIC_RPC in cfg["rpc_url"]:
        for url in SEND_ALSO:
            def also(url=url):
                try:
                    http_json(url, {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": params}, timeout=10)
                except Exception:
                    pass
            threading.Thread(target=also, daemon=True).start()
    return rpc(cfg, "sendTransaction", params)


# what Jupiter's swap transaction may contain at its top level: compute budget, token accounts, wrapping SOL, the
# token programs and Jupiter's own router. A transaction with anything else is refused unsigned.
SWAP_PROGRAMS = {"ComputeBudget111111111111111111111111111111", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
                 "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
                 "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB"}
SWAP_EXTRA_LAMPORTS = 20_000_000    # SOL a swap may move beyond the order itself: account rent and fees


def inspect_swap(tx_b64, wallet_pubkey, max_transfer_lamports):
    """Refuse a Jupiter swap transaction that is not a plain swap for this wallet: the wallet must pay the fee, only known
    programs may be called at the top level, and the SOL transferred by System instructions may not exceed the order."""
    from solders.transaction import VersionedTransaction
    msg = VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).message
    keys = [str(k) for k in msg.account_keys]
    if not keys or keys[0] != wallet_pubkey:
        raise SimError("the swap transaction is not paid by this wallet: not signed")
    moved = 0
    for ins in msg.instructions:
        program = keys[ins.program_id_index]
        if program not in SWAP_PROGRAMS:
            raise SimError(f"the swap transaction calls an unexpected program ({program[:8]}…): not signed")
        data = bytes(ins.data)
        if program == "11111111111111111111111111111111" and len(data) >= 12:
            tag = int.from_bytes(data[:4], "little")
            if tag in (0, 2, 11):       # CreateAccount, Transfer, TransferWithSeed: the lamports follow the 4-byte tag
                moved += int.from_bytes(data[4:12], "little")
    if moved > max_transfer_lamports:
        raise SimError(f"the swap transaction moves {moved / LAMPORTS:.4f} SOL, more than the order allows: not signed")


# Helius Sender: sends over staked connections (and Jito), so a transaction lands in fewer slots; it needs one small tip
# in the transaction to one of its tip accounts. Its staked-connections-only mode takes tips from 0.000005 SOL.
SENDER_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"
SENDER_TIP_LAMPORTS = 100_000          # 0.0001 SOL per transaction
SENDER_TIP_ACCOUNTS = ("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE", "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
                       "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta", "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
                       "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD", "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
                       "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF", "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
                       "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey", "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or")
MIN_PRIORITY_LAMPORTS = 200_000        # a swap pays at least this much priority (capped by the priority setting)
_tipped, _alts = set(), {}             # signed transactions carrying a Sender tip; lookup tables: address -> (read at, keys)


class JupSimError(SimError):
    """Jupiter's own simulation says the swap would fail: no other way of building it is tried."""


def lookup_tables(cfg, addresses):
    """The address lookup tables a Jupiter swap uses, read from the chain and kept for 5 minutes."""
    from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
    from solders.pubkey import Pubkey
    now = time.time()
    missing = [a for a in addresses if now - _alts.get(a, (0, None))[0] > 300]
    if missing:
        res = rpc(cfg, "getMultipleAccounts", [missing, {"encoding": "base64", "commitment": "confirmed"}])
        for a, acc in zip(missing, res["value"]):
            if not acc:
                raise RuntimeError(f"lookup table {a} not found")
            _alts[a] = (now, list(AddressLookupTable.deserialize(base64.b64decode(acc["data"][0])).addresses))
    return [AddressLookupTableAccount(Pubkey.from_string(a), _alts[a][1]) for a in addresses]


def compile_tipped(cfg, payer, r):
    """(MessageV0, lastValidBlockHeight) from Jupiter's swap instructions: its compute budget with the priority fee
    raised to MIN_PRIORITY_LAMPORTS when lower (never above the priority setting), and a Sender tip at the end."""
    import random
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey

    def ix(j):
        return Instruction(Pubkey.from_string(j["programId"]), base64.b64decode(j["data"]),
                           [AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"], a["isWritable"]) for a in j["accounts"]])
    budget = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
    compute = [ix(j) for j in r.get("computeBudgetInstructions") or []]
    # the limit the transaction is priced on: its own SetComputeUnitLimit instruction (tag 2, u32)
    limit = next((int.from_bytes(bytes(i.data)[1:5], "little") for i in compute if bytes(i.data)[:1] == b"\x02"), 0)
    price = next((int.from_bytes(bytes(i.data)[1:9], "little") for i in compute if bytes(i.data)[:1] == b"\x03"), 0)
    if limit > 0:
        # micro-lamports per compute unit: never below Jupiter's own estimate, raised to the floor, and the raise
        # never past the priority setting
        floor = -(-min(MIN_PRIORITY_LAMPORTS, int(cfg["priority_fee_lamports"])) * 1_000_000 // limit)
        cap = int(cfg["priority_fee_lamports"]) * 1_000_000 // limit
        want = max(price, min(floor, cap))
        compute = [i for i in compute if bytes(i.data)[:1] != b"\x03"] + [Instruction(budget, b"\x03" + want.to_bytes(8, "little"), [])]
    payer_key = Pubkey.from_string(payer)
    tip = Instruction(Pubkey.from_string("11111111111111111111111111111111"),
                      (2).to_bytes(4, "little") + SENDER_TIP_LAMPORTS.to_bytes(8, "little"),
                      [AccountMeta(payer_key, True, True), AccountMeta(Pubkey.from_string(random.choice(SENDER_TIP_ACCOUNTS)), False, True)])
    body = ([ix(j) for j in r.get("setupInstructions") or []] + ([ix(r["tokenLedgerInstruction"])] if r.get("tokenLedgerInstruction") else [])
            + [ix(r["swapInstruction"])] + ([ix(r["cleanupInstruction"])] if r.get("cleanupInstruction") else [])
            + [ix(j) for j in r.get("otherInstructions") or []])
    bh = r["blockhashWithMetadata"]
    msg = MessageV0.try_compile(payer_key, compute + body + [tip], lookup_tables(cfg, r.get("addressLookupTableAddresses") or []),
                                Hash(bytes(bh["blockhash"])))
    return msg, int(bh["lastValidBlockHeight"])


def build_swap_tipped(cfg, wallet, q, max_transfer_lamports):
    """Like build_swap, built from Jupiter's swap instructions with a Sender tip and the priority floor."""
    from solders.transaction import VersionedTransaction
    body = {"quoteResponse": q, "userPublicKey": wallet.pubkey, "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": int(cfg["priority_fee_lamports"]), "priorityLevel": "veryHigh"}}}
    r = retrying(lambda: jup_call(lambda: http_json(f"{JUP}/swap-instructions", body)))
    if r.get("simulationError"):
        raise JupSimError(f"Jupiter's simulation says this swap would fail: {r['simulationError']}")
    msg, last_valid = compile_tipped(cfg, wallet.pubkey, r)
    from solders.signature import Signature
    unsigned = bytes(VersionedTransaction.populate(msg, [Signature.default()]))
    if len(unsigned) > 1232:     # Solana's packet limit: the tip pushed this route over it
        raise RuntimeError(f"the tipped transaction is {len(unsigned)} bytes, over the 1232 limit")
    inspect_swap(base64.b64encode(unsigned).decode(), wallet.pubkey, max_transfer_lamports)   # checked before it is signed
    tx = VersionedTransaction(msg, [wallet.kp])
    signed = base64.b64encode(bytes(tx)).decode()
    if len(_tipped) > 200:
        _tipped.clear()
    _tipped.add(signed)
    return signed, str(tx.signatures[0]), last_valid


def build_swap(cfg, wallet, q, max_transfer_lamports):
    """The signed swap: through Helius Sender when it can be built (faster landing), else Jupiter's own transaction."""
    try:
        return build_swap_tipped(cfg, wallet, q, max_transfer_lamports)
    except (JupSimError, urllib.error.URLError, TimeoutError, ConnectionError):
        raise                   # Jupiter's verdict, or Jupiter/the network failing: asking Jupiter again would not help
    except Exception as e:      # a local problem (lookup tables, size, check): nothing was sent, Jupiter's own transaction it is
        print(f"Sender build failed, using Jupiter's transaction: {e!r}", flush=True)
        return build_swap_plain(cfg, wallet, q, max_transfer_lamports)


def build_swap_plain(cfg, wallet, q, max_transfer_lamports):
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
    inspect_swap(r["swapTransaction"], wallet.pubkey, max_transfer_lamports)
    signed, sig = wallet.sign(r["swapTransaction"])
    if not r.get("lastValidBlockHeight"):
        raise SimError("Jupiter did not say until when the transaction can land: not sent")
    return signed, sig, int(r["lastValidBlockHeight"])


def tx_status(cfg, sig, last_valid, check_expiry=True):
    """'landed', 'failed' (landed, did nothing, fee spent), 'expired' (can no longer land) or
    'pending'. Raises when the RPC cannot be asked: the caller keeps the order as it is."""
    st = (rpc(cfg, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]) or {}).get("value", [None])[0]
    if st and st.get("err"):
        return "failed"
    if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
        return "landed"
    if check_expiry and last_valid and rpc(cfg, "getBlockHeight", [CONFIRMED]) > last_valid + 5:
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
    start, resent, height_at = time.time(), time.time(), 0.0
    while time.time() - start < LAND_FOR:
        try:
            # the status every poll; whether its blockhash has expired (a second call) only every 2.5 s
            expiry = time.time() - height_at > 2.5
            if expiry:
                height_at = time.time()
            fate = tx_status(cfg, sig, last_valid, expiry)
            if fate != "pending":
                return fate
        except Exception:
            pass        # a busy RPC: ask again
        if signed_b64 and time.time() - resent > (1.5 if time.time() - start < 4 else 3):   # soon at first, then less often
            resent = time.time()
            try:
                send(cfg, signed_b64)
            except Exception:
                pass
        time.sleep(0.3 if time.time() - start < 8 else 0.7)   # most land within seconds: look often at first
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
    usable). The strategies come from the watch's own file; only the chosen ones are traded, and when none
    is chosen or none of them is still recorded, nothing is."""
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
                  "sl": s["sl"], "be": s.get("be") if num(s.get("be")) and s["be"] > 0 else 0.0,
                  "be_stop": s.get("be_stop") if num(s.get("be_stop")) and s["be_stop"] >= 0 else 0.0, "hold": s["hold"], "backtest": s.get("backtest") if isinstance(s.get("backtest"), dict) else None}
                 for s in every]
    picked = {s["id"]: s for s in every if s["id"] in chosen}
    if chosen and not picked:    # fail closed: never trade strategies the user did not choose
        return {}, available, "none of the chosen strategies is recorded any more, so nothing is bought until it is back or another is chosen"
    return picked, available, ""      # only what the user switched ON on the Trading tab


_sol_usd = [0.0, None]   # (read at, USD per SOL)


def watch_sol_usd(data):
    """USD per SOL as the live watch works it out (watch.json), read at most every 60 s; None when not known."""
    if time.time() - _sol_usd[0] > 60:
        _sol_usd[0] = time.time()
        try:
            with open(os.path.join(data, "watch.json"), encoding="utf-8") as f:
                v = json.load(f).get("sol_usd")
            _sol_usd[1] = float(v) if isinstance(v, (int, float)) and v > 0 else None
        except (OSError, ValueError, AttributeError):
            pass
    return _sol_usd[1]


def last_value_of(db, tid):
    """What the position was last worth by a price check (0 when never priced)."""
    v = db.execute("SELECT last_value FROM trades WHERE id = ?", (tid,)).fetchone()
    return v[0] if v and v[0] is not None else 0.0


def new_signals(data, since_ms):
    """(mint, symbol, strategy, decided_ts, entry minute end, PumpSwap pool) of live buys made since since_ms."""
    try:
        with closing(sqlite3.connect(os.path.join(data, "watch.db"), timeout=30)) as w:
            return w.execute("SELECT d.mint, json_extract(t.info, '$.symbol'), d.strategy, d.decided_ts, "
                             "t.graduated_ts / 60000 * 60000 + d.entry * 60000, json_extract(t.info, '$.pool') "
                             "FROM decisions d JOIN tokens t USING (mint) "
                             "WHERE d.pick = 1 AND d.late = 0 AND d.decided_ts > ?", (since_ms,)).fetchall()
    except sqlite3.Error:
        return []


# ---- Telegram messages, laid out like Rug Radar's automatic strategy's ----
def k_usd(v):
    return "—" if not v else f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K"


def coin_name(symbol, mint):
    return "$" + (symbol or mint[:6])


def tx_links(mint, sig=None):
    parts = ([f'<a href="https://solscan.io/tx/{notify.esc(sig)}">Transaction</a>'] if sig else []) + \
            [f'<a href="https://pump.fun/coin/{notify.esc(mint)}">pump.fun</a>']
    return " · ".join(parts)


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
        try:    # live trades from before trades recorded their wallet: all were made with the wallet in use now
            import wallets
            with open(os.path.join(self.settings, "wallet.json"), encoding="utf-8") as f:
                raw = bytes(json.load(f))
            if len(raw) == 64:
                db.execute("UPDATE trades SET wallet = ? WHERE mode = 'live' AND wallet IS NULL", (wallets.b58encode(raw[32:]),))
        except (OSError, ValueError, TypeError, ImportError):
            pass
        db.close()
        self.wallet, self.wallet_error, self.busy = None, "", set()   # busy: trade ids with an order in progress
        self.lock, self.alerted, self.next_check = threading.Lock(), set(), {}
        self.price_try, self.price_logged = {}, {}   # trade id -> last price check attempt / last logged failure
        self.buy_lock, self.reserved, self.reserved_n = threading.Lock(), 0.0, 0   # SOL taken by live buys still in flight
        # pool prices: trade id -> (estimated value in SOL, read at, pool's gross output in SOL); the calibration factor
        # (Jupiter's quote / the pool's gross output) per trade; a pool's vaults (they never change)
        self.pool_px, self.pool_cal, self.pool_vaults, self.px_written = {}, {}, {}, {}
        self.state_at, self.pricer_logged, self.signals_at, self.buy_gen = 0.0, 0.0, 0.0, 0
        self.pool_bad = set()   # trades whose pool reading disagreed with Jupiter's quote

    def db(self):
        """A connection that commits every statement at once: no write lock is ever held while
        waiting on the network."""
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def say(self, mode, text):
        if mode == "live":      # Telegram is for real trades only: a dry run sends nothing
            tell(self.settings, f"<b>🤖 Auto trading</b>\n{text}")

    def mcap_usd(self, swap_sol, tokens):
        """The market cap in USD at the price a swap of `swap_sol` for `tokens` raw units got (None when not known)."""
        usd = watch_sol_usd(self.data)
        return swap_sol / tokens * SUPPLY_RAW * usd if usd and tokens and swap_sol > 0 else None

    @staticmethod
    def is_live(db, tid):
        r = db.execute("SELECT mode FROM trades WHERE id = ?", (tid,)).fetchone()
        return bool(r) and r[0] == "live"

    def alert_once(self, key, text):
        if key not in self.alerted:
            self.alerted.add(key)
            tell(self.settings, "<b>🤖 Auto trading</b>\n⚠️ " + text)

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
        self.buy_gen += 1       # a buy that lands here changes what a balance read just before it means
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
                    try:    # a message must never change how the trade is handled
                        self.say("live", f"🟢 Bought <b>{notify.esc(coin_name(symbol, mint))}</b> for {-sol:.4f} SOL: confirmed late, "
                                         f"now managed\n{tx_links(mint, sig)}")
                    except Exception as e:
                        print(f"message: {e!r}", flush=True)
                    return
            if fate != "failed" and self.adopt_held(db, cfg, tid, mint, symbol, None, None):
                return      # an RPC that lags can call a landed buy expired: the wallet has the last word
            db.execute("UPDATE trades SET status = 'failed', error = ? WHERE id = ?", (f"buy {fate}: nothing bought", tid))
            return
        if fate == "landed":
            _, sol, fee = tx_effect(cfg, sig, self.wallet.pubkey, mint)
            db.execute("UPDATE trades SET status = 'closed', sol_out = ?, exit_value = ?, closed_ts = ?, "
                       "fee_sol = COALESCE(fee_sol, 0) + ?, error = 'sale confirmed by its signature after a delay' WHERE id = ?",
                       (sol, sol + fee, ms(), fee, tid))
            # the emptied token account gives its rent back, as after a sale confirmed at once; in its
            # own thread, since waiting for that to land must not hold up the stops of other positions
            threading.Thread(target=self.close_late, args=(cfg, tid, mint), daemon=True).start()
        else:   # the sale did nothing: the tokens are still held and sold by the rules again
            self.sale_failed(db, tid, symbol or mint[:8], f"sale {fate}")

    def close_late(self, cfg, tid, mint, wallet=None):
        wallet = wallet or self.wallet
        for attempt in range(2):          # a node that is behind may still show the tokens: once more a little later
            try:
                if wallet and sum(a[2] for a in token_accounts(cfg, wallet.pubkey, mint)) == 0:
                    rent = close_empty_accounts(cfg, wallet, mint)
                    if rent:
                        db = self.db()
                        try:
                            db.execute("UPDATE trades SET rent_back_sol = ?, sol_out = sol_out + ? WHERE id = ?", (rent, rent, tid))
                        finally:
                            db.close()
                        return
            except Exception:
                pass
            time.sleep(10)

    def sale_retry(self, db, tid, why, pause_ms=10000):
        """A sale that did not happen for a reason outside the chain (RPC or Jupiter busy, no
        route for a moment): back to open, tried again shortly, never counted as a failure.
        If it still has not gone out 5 minutes after it was first wanted, Telegram is told."""
        now = ms()   # attempts more than a minute apart are not one run (the sale was not wanted in between)
        db.execute("UPDATE trades SET status = 'open', next_sell_ts = ?, error = ?, retry_since = CASE WHEN retry_since IS NULL "
                   "OR next_sell_ts IS NULL OR next_sell_ts < ? THEN ? ELSE retry_since END "
                   "WHERE id = ? AND status IN ('selling', 'sell_sent', 'check')", (now + pause_ms, why[:500], now - 60000, now, tid))
        r = db.execute("SELECT retry_since, symbol, mint FROM trades WHERE id = ?", (tid,)).fetchone()
        if r and r[0] and ms() - r[0] > RETRY_ALERT_MS and self.is_live(db, tid):
            self.alert_once(f"retry{tid}", f"{notify.esc(r[1] or r[2][:8])}: the sale has not gone out for 5 minutes and is "
                                           f"still being tried. Last error: {notify.esc(why[:200])}. Check it, or sell it by hand.")

    def sale_failed(self, db, tid, name, why):
        """Back to open with a growing pause; after MAX_SELL_FAILS the position is set aside as
        'stuck', alerted once, and still tried again every 5 minutes."""
        n = (db.execute("SELECT sell_fails FROM trades WHERE id = ?", (tid,)).fetchone()[0] or 0) + 1
        if n >= MAX_SELL_FAILS:
            db.execute("UPDATE trades SET status = 'stuck', sell_fails = ?, next_sell_ts = ?, error = ? WHERE id = ?",
                       (n, ms() + STUCK_RETRY_MS, f"{why}; {n} failed sales in a row: tried again every 5 minutes, or sell it by hand", tid))
            if self.is_live(db, tid):
                self.alert_once(f"stuck{tid}", f"{notify.esc(name)} could not be sold {n} times in a row ({notify.esc(why)}). "
                                               "It is left in the wallet: sell it by hand.")
        else:
            db.execute("UPDATE trades SET status = 'open', sell_fails = ?, next_sell_ts = ?, error = ? WHERE id = ?",
                       (n, ms() + min(5000 * 2 ** n, 120000), why, tid))

    def adopt_held(self, db, cfg, tid, mint, symbol, strategy, entry_end):
        """Before a live buy is written off as not bought, look at the wallet: a slow RPC can call a transaction expired
        that did land. Tokens found are managed as an open position (its cost taken as the order's size); when the wallet
        cannot be read, the order stays 'check' so it is looked at again. True when the order was dealt with here."""
        try:
            held = sum(a[2] for a in token_accounts(cfg, self.wallet.pubkey, mint))
        except Exception as e:
            db.execute("UPDATE trades SET status = 'check', check_kind = 'buy', error = ? WHERE id = ?",
                       (f"the wallet could not be read to confirm the buy failed: {e!r}"[:500], tid))
            return True
        if held <= 0:
            return False
        own = db.execute("SELECT buy_quote_tokens FROM trades WHERE id = ?", (tid,)).fetchone()
        if own and own[0]:
            held = min(held, int(own[0]))   # never more than this order's own tokens: the rest may be someone else's
        db.execute("UPDATE trades SET status = 'open', opened_ts = ?, sol_in = ?, tokens = ?, "
                   "error = 'tokens found in the wallet after the buy looked failed: managed as open' WHERE id = ?",
                   (ms(), cfg["sol_per_trade"], held, tid))
        self.alert_once(f"adopt{tid}", f"{notify.esc(symbol or mint[:8])}: the buy looked failed but its tokens are in the wallet. "
                                       "It is managed as an open position.")
        return True

    # ---- orders, each in its own thread ----
    def buy(self, tid, mode, cfg, mint, symbol, strategy, entry_end):
        db, t0, sent, reserved = self.db(), time.time(), False, False
        try:
            lamports = int(cfg["sol_per_trade"] * LAMPORTS)
            # the wallet's balance is read while the quote is asked (used only if no other buy is in flight meanwhile)
            gen = self.buy_gen
            bal = _side.submit(sol_balance, cfg, self.wallet.pubkey) if mode == "live" and self.wallet else None
            q = quote(SOL, mint, lamports, cfg["slippage_bps"])
            t_quote = time.time()
            db.execute("UPDATE trades SET lat_quote_ms = ?, buy_quote_tokens = ? WHERE id = ?",
                       (int((t_quote - t0) * 1000), int(q["outAmount"]), tid))
            if mode == "dry":
                db.execute("UPDATE trades SET status = 'open', opened_ts = ?, sol_in = ?, tokens = ?, lat_total_ms = ? "
                           "WHERE id = ?", (ms(), cfg["sol_per_trade"], int(q["outAmount"]), ms() - entry_end, tid))
                return
            if q.get("inputMint") != SOL or q.get("outputMint") != mint or int(q.get("inAmount", -1)) != lamports:
                raise RuntimeError("Jupiter's quote is not for the order that was asked: not bought")
            limit = cfg["max_buy_impact_pct"] / 100       # the "Highest price impact of a buy" setting (the backtest assumes at most 5%)
            if float(q.get("priceImpactPct") or 0) > limit:
                raise RuntimeError(f"the buy would move the price {float(q['priceImpactPct']) * 100:.1f}%, more than the "
                                   f"{limit * 100:g}% limit in Limits and settings (the backtest allows 5%): not bought")
            with self.buy_lock:
                # positions that hold or may hold tokens, plus buys past this check and not yet sent
                held_live = db.execute(f"SELECT COUNT(*) FROM trades WHERE mode = 'live' AND id != ? AND status IN "
                                       f"({','.join('?' * (len(HELD_STATES) - 1))})",
                                       (tid, *[x for x in HELD_STATES if x != "buying"])).fetchone()[0] + self.reserved_n
                checking = db.execute("SELECT COALESCE(SUM(?), 0) FROM trades WHERE mode = 'live' AND id != ? AND "
                                      "status = 'check' AND check_kind = 'buy'", (cfg["sol_per_trade"], tid)).fetchone()[0]
                per = max(RESERVE_PER_POSITION, 0.006 + 2 * (cfg["priority_fee_lamports"] + SENDER_TIP_LAMPORTS) / 1e9)
                need = cfg["sol_per_trade"] + self.reserved + checking + per * (held_live + 1)
                try:
                    early = bal.result(timeout=3) if bal is not None and self.reserved_n == 0 and self.buy_gen == gen else None
                except Exception:     # the side read failed: read it again below
                    early = None
                if (early if early is not None else sol_balance(cfg, self.wallet.pubkey)) < need:
                    raise RuntimeError(f"not enough SOL in the wallet (needs {need:.3f} with fees for every open position's sale)")
                self.reserved += cfg["sol_per_trade"]
                self.reserved_n += 1
                self.buy_gen += 1
                reserved = True
            if not self.wallet_still_ok():   # deleted or switched on the Settings page since this buy started
                raise WalletChanged("the wallet was deleted or changed: not bought")
            signed, sig, last_valid = build_swap(cfg, self.wallet, q, lamports + SWAP_EXTRA_LAMPORTS)
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
            if fate == "expired" and self.adopt_held(db, cfg, tid, mint, symbol, strategy, entry_end):
                return
            if fate != "landed":
                db.execute("UPDATE trades SET status = 'failed', error = ? WHERE id = ?",
                           ("the buy failed on chain: nothing bought" if fate == "failed" else
                            "the buy did not land before its blockhash expired: nothing bought", tid))
                return
            got, sol, fee = tx_effect_patiently(cfg, sig, self.wallet.pubkey, mint)
            if got <= 0:
                if self.adopt_held(db, cfg, tid, mint, symbol, strategy, entry_end):
                    return
                db.execute("UPDATE trades SET status = 'failed', error = 'the buy landed but no tokens arrived' WHERE id = ?", (tid,))
                return
            db.execute("UPDATE trades SET status = 'open', opened_ts = ?, sol_in = ?, tokens = ?, fee_sol = ?, "
                       "lat_land_ms = ?, lat_total_ms = ? WHERE id = ?",
                       (ms(), -sol, got, fee, int((time.time() - t_built) * 1000), ms() - entry_end, tid))
            try:    # a message must never change how the trade is handled
                tp, sl, until = db.execute("SELECT tp, sl, hold_until FROM trades WHERE id = ?", (tid,)).fetchone()
                exits = (f"Take profit +{tp * 100:.0f}% · stop loss −{sl * 100:.0f}% · sold after "
                         f"{(until - entry_end) / MIN:.0f} min at the latest\n") if tp is not None and sl is not None and until else ""
                self.say(mode, f"🟢 Bought <b>{notify.esc(coin_name(symbol, mint))}</b> by {notify.esc(strategy)} for {-sol:.4f} SOL "
                               f"at {k_usd(self.mcap_usd(-sol - RENT - fee, got))}\n{exits}"
                               f"Confirmed {(ms() - entry_end) / 1000:.1f} s after the signal\n<code>{notify.esc(mint)}</code>\n{tx_links(mint, sig)}")
            except Exception as e:
                print(f"message: {e!r}", flush=True)
        except Exception as e:
            if sent:    # sent: the chain decides, not this error (a row already settled stays as it is)
                db.execute("UPDATE trades SET status = 'check', check_kind = 'buy', error = ? WHERE id = ? AND status IN ('buying', 'buy_sent')",
                           (f"checked by signature after: {e!r}"[:500], tid))
            else:
                db.execute("UPDATE trades SET status = 'failed', error = ? WHERE id = ?", (repr(e)[:500], tid))
                if isinstance(e, WalletChanged):   # one note for the switch, not one message for every signal
                    self.alert_once("wallet-changed", "The trading wallet was deleted or switched: buys wait until the new one is loaded.")
                else:
                    self.say(mode, f"⚠️ Couldn't buy <b>{notify.esc(coin_name(symbol, mint))}</b> ({notify.esc(strategy)}): "
                                   f"{notify.esc(str(e)[:200] or repr(e)[:200])}. Nothing was bought.")
        finally:
            db.close()
            with self.lock:
                self.busy.discard(tid)
            if reserved:
                with self.buy_lock:
                    self.reserved -= cfg["sol_per_trade"]
                    self.reserved_n -= 1
                    self.buy_gen += 1

    def sell(self, tid, mode, cfg, mint, symbol, sol_in, tokens, reason, trigger_ts, quoted):
        db, sent, name = self.db(), False, symbol or mint[:8]
        try:
            slip = cfg["stop_slippage_bps"] if reason in ("stop loss", "breakeven stop", "sell all", "sold by hand", "time stop") else cfg["sell_slippage_bps"]
            if mode == "dry":
                out = quoted
                db.execute("UPDATE trades SET status = 'closed', sol_out = ?, exit_value = ?, closed_ts = ?, reason = ?, "
                           "sell_trigger_ts = ?, sell_quote_sol = ?, lat_sell_ms = ?, error = NULL WHERE id = ?",
                           (out, out, ms(), reason, trigger_ts, quoted, ms() - trigger_ts, tid))
            else:
                # only this position's tokens, and never more than the wallet really holds; the quote for the position's
                # tokens is asked while the wallet is read, and asked again only if the wallet holds fewer
                q_full = _side.submit(quote, mint, SOL, tokens, slip, 1)   # once: the fallback below retries
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
                try:
                    q = q_full.result(timeout=8) if amount == tokens else None
                except Exception:
                    q = None
                if q is None:
                    q = quote(mint, SOL, amount, slip)
                signed, sig, last_valid = build_swap(cfg, self.wallet, q, SWAP_EXTRA_LAMPORTS)
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
                landed_ms = ms()          # the sale's latency is to here: reading its result and the rent come after
                _, sol, fee = tx_effect_patiently(cfg, sig, self.wallet.pubkey, mint)
                out = sol
                db.execute("UPDATE trades SET status = 'closed', sol_out = ?, exit_value = ?, closed_ts = ?, reason = ?, rent_back_sol = 0, "
                           "fee_sol = COALESCE(fee_sol, 0) + ?, lat_sell_ms = ?, error = NULL WHERE id = ?",
                           (out, sol + fee, ms(), reason, fee, landed_ms - trigger_ts, tid))
                # the emptied token account is closed in the background: its rent is added to sol_out when it lands
                threading.Thread(target=self.close_late, args=(cfg, tid, mint, self.wallet), daemon=True).start()
            if mode == "live":
                try:    # a message must never change how the trade is handled
                    got = out + (RENT if out and mode == "live" else 0)   # the token account's rent comes back moments later
                    r = got / sol_in - 1 if sol_in else 0
                    opened = db.execute("SELECT COALESCE(opened_ts, entry_ts) FROM trades WHERE id = ?", (tid,)).fetchone()[0]
                    held_min = f"Held {(ms() - opened) / MIN:.0f} min · " if opened else ""
                    self.say(mode, f"{'💰' if got >= (sol_in or 0) else '🔻'} Sold <b>{notify.esc(coin_name(symbol, mint))}</b> ({notify.esc(reason)}) "
                                   f"at {k_usd(self.mcap_usd(sol + fee, amount))}\n"
                                   f"{got - (sol_in or 0):+.4f} SOL ({r * 100:+.1f}%) · spent {(sol_in or 0):.4f}, got {got:.4f}\n"
                                   f"{held_min}confirmed {(ms() - trigger_ts) / 1000:.1f} s after the trigger\n{tx_links(mint, sig)}")
                except Exception as e:
                    print(f"message: {e!r}", flush=True)
        except Exception as e:
            if sent:    # (a row already settled stays as it is)
                db.execute("UPDATE trades SET status = 'check', check_kind = 'sell', error = ? WHERE id = ? AND status IN ('selling', 'sell_sent')",
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
    def consider(self, db, cfg, mode, lost, s, mint, symbol, sid, decided, entry_end, pool=None):
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
                         "lat_seen_ms, wallet, pool, be, be_stop) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (mint, symbol, sid, mode, decided, entry_end, "buying", s["tp"], s["sl"],
                          int(entry_end + s["hold"] * MIN), ms() - entry_end,
                          self.wallet.pubkey if mode == "live" and self.wallet else None, pool, s.get("be") or 0.0, s.get("be_stop") or 0.0))
        self.start(cur.lastrowid, self.buy, mode, cfg, mint, symbol, s["name"], entry_end)

    # ---- buys asked by hand on the Crossing page ----
    def manual_buys(self, db, cfg, mode, lost):
        """Each file in settings/manual_buys/ (written by the web server, one per token) is one buy asked by hand: the token, its exit
        levels (take profit, stop loss, time stop in minutes) and when it was asked. The same limits as an automatic buy apply
        (mode, a loaded wallet, most open positions, the daily loss limit, the price impact limit at the quote); a request that
        cannot be carried out is recorded as a failed buy with the reason, which the page shows. One manual buy per token."""
        folder = os.path.join(self.settings, "manual_buys")
        try:
            names = [n for n in os.listdir(folder) if n.endswith(".json")][:5]
        except OSError:
            return
        for n in names:
            path = os.path.join(folder, n)
            try:
                with open(path, encoding="utf-8") as f:
                    req = json.load(f)
            except (OSError, ValueError):
                req = None
            try:
                os.remove(path)
            except OSError:
                pass
            try:
                mint, symbol = req["mint"], str(req.get("symbol") or "")[:40]
                tp, sl, hold, asked = float(req["tp"]), float(req["sl"]), int(req["hold"]), int(req["asked"])
                if not (re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint) and 0.05 <= tp <= 10 and 0.05 <= sl <= 0.95 and 1 <= hold <= 240):
                    raise ValueError("bad request")
            except (TypeError, KeyError, ValueError):
                print(f"manual buy request {n} ignored: unreadable", flush=True)
                continue
            if ms() - asked > 120_000:
                print(f"manual buy {mint} ignored: asked {(ms() - asked) // 1000} s ago", flush=True)
                continue       # (the trader was down when it was asked: a buy must not happen long after the click)
            held = f"({','.join('?' * len(HELD_STATES))})"
            seen = db.execute("SELECT id, status FROM trades WHERE mint = ? AND strategy = ?", (mint, MANUAL)).fetchone()
            if seen and seen[1] != "failed":
                continue       # bought before (or on its way): one manual buy per token
            why = None
            if mode == "off":
                why = "Trading is off: switch it on first (Crossing trading on the Crossing page, or the Trading tab)"
            elif mode == "live" and not self.wallet:
                why = "no trading wallet is loaded"
            elif mode == "live" and db.execute(f"SELECT 1 FROM trades WHERE mint = ? AND mode = 'live' AND status IN {held}", (mint, *HELD_STATES)).fetchone():
                why = "this token already has a live position"
            elif db.execute(f"SELECT COUNT(*) FROM trades WHERE mode = ? AND status IN {held}", (mode, *HELD_STATES)).fetchone()[0] >= cfg["max_open"]:
                why = f"the limit of {cfg['max_open']} open positions is reached"
            elif lost <= -abs(cfg["daily_loss_limit_sol"]):
                why = "the daily loss limit is reached"
            if seen:
                db.execute("DELETE FROM trades WHERE id = ?", (seen[0],))
            now = ms()
            if why:
                db.execute("INSERT INTO trades (mint, symbol, strategy, mode, signal_ts, entry_ts, status, error, wallet) VALUES (?,?,?,?,?,?,?,?,?)",
                           (mint, symbol, MANUAL, mode, now, now, "failed", "not bought: " + why, self.wallet.pubkey if mode == "live" and self.wallet else None))
                print(f"manual buy {symbol or mint[:6]} refused: {why}", flush=True)
                continue
            s = {"name": MANUAL_NAME, "tp": tp, "sl": sl, "hold": hold}
            print(f"manual buy {symbol or mint[:6]}: {cfg['sol_per_trade']} SOL, +{tp * 100:.0f}% / -{sl * 100:.0f}% / {hold} min", flush=True)
            self.consider(db, cfg, mode, lost, s, mint, symbol, MANUAL, now, now, None)

    # ---- open positions priced from their pool, in their own thread ----
    def pricer(self):
        while True:
            t0 = time.time()
            try:
                self.price_pools()
            except Exception as e:      # never stops: the round falls back to Jupiter while pool prices are stale
                if time.time() - self.pricer_logged > 60:
                    self.pricer_logged = time.time()
                    print(f"pool prices: {e!r}", flush=True)
            time.sleep(max(0.1, POOL_EVERY - (time.time() - t0)))

    def price_pools(self):
        db = self.db()
        try:
            rows = db.execute("SELECT id, mint, tokens, pool FROM trades WHERE status = 'open' AND tokens > 0").fetchall()
            missing = [(tid, mint) for tid, mint, _, pool in rows if not pool]
            if missing:     # positions from before trades recorded their pool: the watch knows it
                try:
                    with closing(sqlite3.connect(f"file:{os.path.join(self.data, 'watch.db')}?mode=ro", uri=True, timeout=5)) as w:
                        for tid, mint in missing:
                            r = w.execute("SELECT json_extract(info, '$.pool') FROM tokens WHERE mint = ?", (mint,)).fetchone()
                            if r and r[0]:
                                db.execute("UPDATE trades SET pool = ? WHERE id = ?", (r[0], tid))
                except sqlite3.Error:
                    pass
                rows = db.execute("SELECT id, mint, tokens, pool FROM trades WHERE status = 'open' AND tokens > 0").fetchall()
        finally:
            db.close()
        rows = [r for r in rows if r[3]]
        for tid in [t for t in self.pool_px if t not in {r[0] for r in rows}]:
            self.pool_px.pop(tid, None)
        if not rows:
            return
        cfg = load_cfg(self.data)
        new = sorted({r[3] for r in rows if r[3] not in self.pool_vaults})
        if new:             # a pool's mints (43, 75) and vaults (base 139, SOL 171) in the PumpSwap pool account
            from solders.pubkey import Pubkey
            res = rpc_primary(cfg, "getMultipleAccounts", [new, {"encoding": "base64", "commitment": "confirmed"}])["value"]
            mint_of = {r[3]: r[1] for r in rows}
            for pool, acc in zip(new, res):
                raw = base64.b64decode(acc["data"][0]) if acc else b""
                if len(raw) < 107:
                    continue          # not there (a node behind): read again next time
                key = lambda a: str(Pubkey.from_bytes(raw[a:a + 32]))   # noqa: E731
                # only the coin's own pool, paired with SOL; anything else is remembered as unusable (Jupiter prices it)
                ok = len(raw) >= 261 and key(43) == mint_of[pool] and key(75) == SOL
                self.pool_vaults[pool] = (key(139), key(171)) if ok else None
        rows = [r for r in rows if self.pool_vaults.get(r[3])]
        keys = []
        for _, _, _, pool in rows:
            keys += [pool, *self.pool_vaults[pool]]
        res = rpc_primary(cfg, "getMultipleAccounts", [keys[:99], {"encoding": "base64", "commitment": "confirmed"}])["value"]
        now = time.time()
        for i, (tid, mint, tokens, pool) in enumerate(rows[:33]):
            p_acc, b_acc, q_acc = res[3 * i: 3 * i + 3]
            if not (p_acc and b_acc and q_acc):
                continue
            praw, braw, qraw = (base64.b64decode(a["data"][0]) for a in (p_acc, b_acc, q_acc))
            if len(praw) < 261 or len(braw) < 72 or len(qraw) < 72:
                continue
            virtual = int.from_bytes(praw[245:261], "little", signed=True)     # PumpSwap's virtual SOL reserve
            base = int.from_bytes(braw[64:72], "little")                        # a token account's amount
            quote_lamports = int.from_bytes(qraw[64:72], "little") + virtual   # signed: some pools carry a negative one
            if base <= 0 or quote_lamports <= 0:
                continue
            gross = quote_lamports * tokens / (base + tokens) / LAMPORTS        # constant product with the virtual reserve
            # never more than the SOL really in the vault (the virtual part can not be paid out)
            gross = min(gross, int.from_bytes(qraw[64:72], "little") / LAMPORTS)
            self.pool_px[tid] = (gross * self.pool_cal.get(tid, 1 - POOL_FEE_GUESS), now, gross)

    def purge_wallets(self, db):
        """A wallet deleted on the Settings page: its trades go too, once none of them is held. Today's result of those
        trades is kept in meta, so deleting a wallet never resets the daily loss limit."""
        folder = os.path.join(self.settings, "wallet_purge")
        try:
            queued = os.listdir(folder)
        except OSError:
            return
        held = ",".join("?" * len(HELD_STATES))
        for address in queued:
            if address.endswith(".tmp"):     # a request still being written
                continue
            if db.execute(f"SELECT 1 FROM trades WHERE wallet = ? AND status IN ({held}) LIMIT 1", (address, *HELD_STATES)).fetchone():
                continue                 # still held: purged once it is closed
            today = time.strftime("%Y-%m-%d", time.gmtime())
            lost = db.execute("SELECT COALESCE(SUM(sol_out - sol_in), 0) FROM trades WHERE wallet = ? AND mode = 'live' AND "
                              "status = 'closed' AND date(closed_ts / 1000, 'unixepoch') = ?", (address, today)).fetchone()[0]
            lost = min(lost, 0.0)        # only a deleted wallet's loss counts against the limit, wallet by wallet
            if lost:
                key = "purged:" + today
                row = db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str((float(row[0]) if row else 0.0) + lost)))
            n = db.execute(f"DELETE FROM trades WHERE wallet = ? AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.wallet = ? "
                           f"AND t.status IN ({held}))", (address, address, *HELD_STATES)).rowcount
            print(f"deleted wallet {address[:4]}…{address[-4:]}: its {n} trades removed", flush=True)
            try:
                os.remove(os.path.join(folder, address))
            except OSError:
                pass

    def wallet_still_ok(self):
        """The loaded key is still the one on disk and has not been deleted: checked right before a buy is signed."""
        key = os.path.join(self.settings, "wallet.json")
        try:
            same = os.stat(key).st_mtime_ns == getattr(self, "wallet_stamp", None)
        except OSError:
            return False
        return same and not os.path.exists(os.path.join(self.settings, "wallet_purge", self.wallet.pubkey))

    def round(self):
        cfg, now = load_cfg(self.data), time.time()
        db = self.db()
        live_rows = db.execute(f"SELECT COUNT(*) FROM trades WHERE mode = 'live' AND status IN "
                               f"({','.join('?' * len(HELD_STATES))})", HELD_STATES).fetchone()[0]
        # the wallet is loaded whenever its file is there, so the Trading tab shows its address and
        # balance in every mode; it signs only in live mode, or to sell positions bought live
        key = os.path.join(self.settings, "wallet.json")
        try:        # the Settings page switched the wallet: load the new key once nothing is held with the old one
            stamp = os.stat(key).st_mtime_ns
        except OSError:
            stamp = None
        if self.wallet is not None and stamp != getattr(self, "wallet_stamp", stamp) and not live_rows and not self.busy:
            print("wallet file changed: loading it", flush=True)
            self.wallet = None
        if self.wallet is None and (cfg["mode"] == "live" or live_rows or os.path.exists(key)):
            try:
                self.wallet, self.wallet_error, self.wallet_stamp = Wallet(key), "", stamp
            except Exception as e:      # no key file, a readable one, a malformed one, or solders missing
                self.wallet_error = f"the wallet could not be loaded: {e!r}"
                if live_rows:
                    self.alert_once("wallet", f"Live positions are held but the wallet cannot be loaded: {notify.esc(repr(e)[:200])}")
        mode = cfg["mode"] if cfg["mode"] != "live" or self.wallet else "off"
        public_rpc = PUBLIC_RPC in cfg["rpc_url"]

        # 0. a row whose order thread is gone (a database error in its own handler) would never be looked at again:
        # rounds run one after another, and a thread is on `busy` before its row is touched, so this is safe here
        with self.lock:
            idle = set(self.busy)
        for tid, status, kind in db.execute("SELECT id, status, check_kind FROM trades WHERE status IN "
                                            "('buying', 'selling', 'buy_sent', 'sell_sent')").fetchall():
            if tid in idle:
                continue
            new = {"buying": ("failed", "the order thread ended before the order was sent"), "selling": ("open", None),
                   "buy_sent": ("check", "the order thread ended after the order was sent: checked by signature"),
                   "sell_sent": ("check", "the order thread ended after the order was sent: checked by signature")}[status]
            db.execute("UPDATE trades SET status = ?, check_kind = CASE WHEN ? IN ('buy_sent', 'sell_sent') THEN ? ELSE check_kind END, "
                       "error = COALESCE(?, error) WHERE id = ?",
                       (new[0], status, "buy" if status == "buy_sent" else "sell", new[1], tid))

        # a wallet deleted on the Settings page: its trade history goes too (once none of its positions is held)
        self.purge_wallets(db)

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
            "opened_ts, be, peak_value, be_stop FROM trades WHERE status = 'open'").fetchall()
        open_ids = {r[0] for r in positions}
        for d in (self.price_try, self.price_logged):
            for k in [k for k in d if k not in open_ids]:
                d.pop(k)
        # the position that has waited longest for a price goes first, so none is ever left out
        positions.sort(key=lambda r: self.price_try.get(r[0], 0))
        gap = max(CHECK_EVERY, JUP_WINDOW / JUP_FOR_CHECKS * len(positions))
        for tid, mint, symbol, pmode, sol_in, tokens, tp, sl, until, checked, asked, next_sell, opened, be, peak, be_stop in positions:
            try:
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
                if pmode == "live" and now * 1000 - (checked or opened or now * 1000) > 120000:
                    self.alert_once(f"price{tid}", f"{notify.esc(symbol or mint[:8])} has had no price for 2 minutes, "
                                                   "so its stop loss cannot work. Check it by hand.")
                # the pool price (read every second in its own thread) when it is fresh; Jupiter, whose small
                # allowance is shared with real orders, every CALIBRATE_EVERY s to calibrate it, or as the fallback
                px = self.pool_px.get(tid)
                fresh = px is not None and now - px[1] < POOL_FRESH and tid not in self.pool_bad
                value = None
                if now - self.price_try.get(tid, 0) >= (max(CALIBRATE_EVERY, gap) if fresh else gap) and jup_recent() < JUP_FOR_CHECKS:
                    self.price_try[tid] = now
                    try:
                        value = int(quote(mint, SOL, tokens, cfg["sell_slippage_bps"], tries=1)["outAmount"]) / LAMPORTS
                        px = self.pool_px.get(tid)
                        if px and px[2] > 0 and time.time() - px[1] < 1.0:
                            # Jupiter's quote / the pool's gross output (the fee and route), only against a reading taken
                            # within the last second, smoothed, and only ever lowering the estimate a little
                            r = value / px[2]
                            if not 0.95 <= r <= 1.02:      # the pool reading is wrong for this coin: Jupiter prices it
                                self.pool_bad.add(tid)
                                print(f"pool price {symbol or mint[:8]}: {r:.3f} of Jupiter's, not used", flush=True)
                            r = min(0.9975, max(0.97, r))
                            old = self.pool_cal.get(tid)
                            self.pool_cal[tid] = r if old is None else (old + r) / 2
                    except Exception as e:
                        if not fresh:
                            db.execute("UPDATE trades SET error = ? WHERE id = ?", (f"price check: {e!r}"[:500], tid))
                        if now - self.price_logged.get(tid, 0) > 60:
                            self.price_logged[tid] = now
                            print(f"price check {symbol or mint[:8]}: {e!r}", flush=True)
                if value is None and fresh:
                    if self.px_written.get(tid) == px[1]:
                        continue          # no new reading since the last look
                    value = px[0]
                    self.px_written[tid] = px[1]
                if value is None:
                    continue
                db.execute("UPDATE trades SET last_value = ?, peak_value = MAX(COALESCE(peak_value, 0), ?), last_checked = ?, "
                           "error = NULL WHERE id = ?", (value, value, ms(), tid))
                if next_sell and now * 1000 < next_sell:
                    continue        # pausing after failed sales
                # the cost the levels are measured against: what was paid, less the token account's
                # rent, which comes back when the account is closed after the sale
                cost = sol_in - (RENT if pmode == "live" else 0)
                eps = 1e-9 * cost
                # breakeven: once the position has been worth `be` more than its cost (the peak includes this reading), the
                # stop is the cost plus `be_stop`
                moved = bool(be) and max(peak or 0, value) >= cost * (1 + be) - eps
                reason = ("take profit" if value >= cost * (1 + tp) - eps
                          else "breakeven stop" if moved and value <= cost * (1 + (be_stop or 0.0)) + eps
                          else "stop loss" if value <= cost * (1 - sl) + eps else None)
                if reason:
                    db.execute("UPDATE trades SET status = 'selling' WHERE id = ?", (tid,))
                    self.start(tid, self.sell, pmode, cfg, mint, symbol, sol_in, tokens, reason, ms(), value)
            except Exception as e:     # one position's trouble must never stop the others' stops
                print(f"position {tid} skipped this round: {e!r}", flush=True)

        # 3. then buy on fresh live signals
        strategies, available, note = trade_strategies(self.data, cfg["strategies"])
        if getattr(self, "manual_only", False):
            strategies = {}
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        closed = db.execute("SELECT COALESCE(SUM(sol_out - sol_in), 0) FROM trades WHERE status = 'closed' AND mode = ? "
                            "AND date(closed_ts / 1000, 'unixepoch') = ?", (mode, today)).fetchone()[0]
        unrealised = db.execute("SELECT COALESCE(SUM(MIN(last_value - sol_in, 0)), 0) FROM trades WHERE status IN "
                                "('open', 'selling', 'sell_sent', 'check', 'stuck') AND mode = ? AND last_value IS NOT NULL",
                                (mode,)).fetchone()[0]
        purged = db.execute("SELECT v FROM meta WHERE k = ?", ("purged:" + today,)).fetchone()
        # what the page shows is the wallet in use; the daily loss limit also counts what deleted wallets lost today
        # (never their profit), so deleting a wallet can not reset it
        lost = closed + min(unrealised, 0) + (min(float(purged[0]), 0.0) if purged and mode == "live" else 0.0)
        if mode == "live" and self.wallet:   # the wallet in use only (a wallet switched away from still counts in `lost`)
            mine = db.execute("SELECT COALESCE(SUM(sol_out - sol_in), 0) FROM trades WHERE status = 'closed' AND mode = 'live' "
                              "AND wallet = ? AND date(closed_ts / 1000, 'unixepoch') = ?", (self.wallet.pubkey, today)).fetchone()[0]
            open_mine = db.execute("SELECT COALESCE(SUM(MIN(last_value - sol_in, 0)), 0) FROM trades WHERE status IN "
                                   "('open', 'selling', 'sell_sent', 'check', 'stuck') AND mode = 'live' AND wallet = ? "
                                   "AND last_value IS NOT NULL", (self.wallet.pubkey,)).fetchone()[0]
            today_result = mine + min(open_mine, 0)
        else:
            today_result = closed + min(unrealised, 0)
        blocked = ""   # the user chose the free public RPC: orders also go out through a second free RPC
        signals = []
        if now - self.signals_at >= 0.3:   # (the round runs every ROUND_SLEEP s; the watch's database is read ~3 times a second)
            self.signals_at = now
            signals = new_signals(self.data, int((now - SIGNAL_FOR) * 1000))
        for mint, symbol, sid, decided, entry_end, pool in signals:
            if mode == "off" or blocked or sid not in strategies:
                continue
            try:
                self.consider(db, cfg, mode, lost, strategies[sid], mint, symbol, sid, decided, entry_end, pool)
            except Exception as e:
                print(f"signal {mint} {sid} skipped: {e!r}", flush=True)
        try:
            self.manual_buys(db, cfg, mode, lost)
        except Exception as e:
            print(f"manual buys skipped: {e!r}", flush=True)

        # 4. what the Trading tab shows (at most every STATE_EVERY s: the round itself runs every ROUND_SLEEP s)
        if time.time() - self.state_at < STATE_EVERY:
            db.close()
            return
        self.state_at = time.time()
        cols = ("id", "mint", "symbol", "strategy", "mode", "signal_ts", "entry_ts", "opened_ts", "buy_sig", "sol_in", "status",
                "last_value", "sell_sig", "sol_out", "closed_ts", "reason", "error", "lat_seen_ms", "lat_quote_ms",
                "lat_build_ms", "lat_land_ms", "lat_total_ms", "lat_sell_ms", "buy_quote_tokens", "tokens", "sell_quote_sol",
                "rent_back_sol", "fee_sol", "sell_fails", "sell_asked", "tp", "sl", "hold_until", "peak_value", "exit_value", "wallet")
        rows = [dict(zip(cols, r)) for r in db.execute(f"SELECT {', '.join(cols)} FROM trades ORDER BY id DESC LIMIT 200")]
        for r in rows:
            # what the buy paid per token: the SOL spent on the swap itself (not the token account's rent, which comes
            # back, nor the network fee) over the tokens received; times pump.fun's fixed supply, the market cap at entry
            if r["sol_in"] and r["tokens"]:
                # (a measured live buy only: an adopted one's cost is the order size, with no rent or fee in it)
                spent = r["sol_in"] - (RENT + r["fee_sol"] if r["mode"] == "live" and r["fee_sol"] is not None else 0)
                r["entry_mcap_sol"] = spent / r["tokens"] * SUPPLY_RAW if spent > 0 else None
                r["swap_cost"] = spent if spent > 0 else None     # what the buy's swap itself cost: "Now / exit" is measured on it
        state = {"generated_at": ms(), "mode": mode, "wanted_mode": cfg["mode"], "sol_usd": watch_sol_usd(self.data),
                 "error": "; ".join(x for x in (self.wallet_error, blocked, note) if x),
                 "strategies_available": available, "strategies_traded": [{"id": k, "name": s["name"]} for k, s in strategies.items()],
                 "wallet": self.wallet.pubkey if self.wallet else None, "live_ready": self.wallet is not None,
                 "settings": {k: cfg[k] for k in DEFAULTS if k not in ("rpc_url", "sell_all", "sell_ids")},
                 "rpc_is_public": public_rpc, "hard_max_sol": HARD_MAX_SOL, "today_result_sol": today_result, "today_limit_sol": lost, "trades": rows}
        if self.wallet and (now - getattr(self, "balance_at", 0) >= 10) and not getattr(self, "balance_reading", False):
            self.balance_at, self.balance_reading = now, True     # read in the background: a slow RPC never holds up a round
            w = self.wallet

            def read_balance():
                try:
                    self.balance = sol_balance(cfg, w.pubkey)
                except Exception:
                    pass
                finally:
                    self.balance_reading = False
            _side.submit(read_balance)
        if self.wallet:
            state["balance_sol"] = getattr(self, "balance", None)
        tmp = os.path.join(self.data, "trading_state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, os.path.join(self.data, "trading_state.json"))
        db.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--manual-only", action="store_true", help="never buy on a strategy's signal: only what a Buy button asked for (the Crossing trader)")
    args = ap.parse_args()
    t = Trader(args.data)
    t.manual_only = args.manual_only
    t.recover()
    threading.Thread(target=t.pricer, daemon=True).start()
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
        time.sleep(ROUND_SLEEP)


if __name__ == "__main__":
    main()

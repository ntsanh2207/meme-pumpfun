"""Wallet monitor service: watches chosen wallets and reports every trade (and optionally transfers) to Telegram.

A port of Rug Radar's src/wallet/watch.js. Every few seconds (slower while the RPC rate-limits) each wallet's new
transaction signatures are read (getSignaturesForAddress with `until` the last one seen), the transactions are fetched and
classified by walletparse.parse_tx, and trades are kept as events. Two files:
  settings/watch.json      the wallets and options, written by the web server: {"wallets": [{address, label, muted, paused}],
                           "settings": {telegram, transfers, min_sol}}
  wallet-watch.json        this service's state (last signature, pending signatures, events), read by the web server
Telegram uses its own bot: settings/watch_telegram.json {token, chat_id}, separate from the trade alerts bot.
    python3 monitor.py --data data
"""
import argparse
import copy
import html
import json
import os
import queue
import re
import tempfile
import threading
import time

import notify
import solana
import sources
import walletparse as P

BASE_POLL, MAX_POLL = 5.0, 30.0
MAX_EVENTS, MAX_BACKLOG, MAX_MESSAGES_PER_CHECK = 100, 300, 8
PARALLEL, PENDING_TRIES = 4, 24
TRADES = ("buy", "sell", "swap")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://pumpgrad.0xportal.app").rstrip("/")
DEFAULTS = {"telegram": True, "transfers": False, "min_sol": 0.0}
BASE58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
MAX_WALLETS = 25


# ---- the files --------------------------------------------------------------------------------------------------------

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, doc, mode=0o644):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".w", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_watch(data):
    """The wallets and options the web server saved."""
    doc = read_json(os.path.join(data, "settings", "watch.json"), {})
    wallets = [w for w in doc.get("wallets", []) if isinstance(w, dict) and BASE58.fullmatch(str(w.get("address", "")))]
    return {"wallets": wallets, "settings": {**DEFAULTS, **(doc.get("settings") or {})}}


def save_watch(data, doc):
    os.makedirs(os.path.join(data, "settings"), exist_ok=True)
    write_json(os.path.join(data, "settings", "watch.json"), doc, 0o600)


TG_FILE = "watch_telegram.json"


def tg_load(data):
    cfg = read_json(os.path.join(data, "settings", TG_FILE), {})
    return cfg if isinstance(cfg, dict) else {}


def tg_public(cfg):
    token, chat = cfg.get("token") or "", str(cfg.get("chat_id") or "")
    return {"configured": bool(token and chat), "token_hint": "…" + token[-4:] if token else "", "chat_id": chat}


def tg_save(data, token, chat_id):
    """Validate and save the monitor's bot. An empty token keeps the saved one. Raises ValueError saying what is wrong."""
    cfg = tg_load(data)
    token = (token or "").strip() or cfg.get("token") or ""
    chat = str(chat_id or "").strip()
    if token and not notify.TOKEN.fullmatch(token):
        raise ValueError("That does not look like a bot token. BotFather gives one like 123456789:AA…")
    if chat and not notify.CHAT.fullmatch(chat):
        raise ValueError("The chat ID must be a number (for example 123456789), or @channelname.")
    os.makedirs(os.path.join(data, "settings"), exist_ok=True)
    write_json(os.path.join(data, "settings", TG_FILE), {"token": token, "chat_id": chat}, 0o600)
    return tg_load(data)


def tg_clear(data):
    try:
        os.remove(os.path.join(data, "settings", TG_FILE))
    except OSError:
        pass


# ---- Telegram messages ------------------------------------------------------------------------------------------------

e = notify.esc


def ea(x):
    return html.escape(str(x), quote=True)      # inside an attribute


def money(n):
    return "—" if not n else f"${n / 1e6:.2f}M" if n >= 1e6 else f"${n / 1e3:.1f}K" if n >= 1e3 else f"${round(n)}"


def amt(n):
    a = abs(n)
    return f"{a / 1e9:.2f}B" if a >= 1e9 else f"{a / 1e6:.2f}M" if a >= 1e6 else f"{a / 1e3:.1f}K" if a >= 1e3 else f"{a:.2f}" if a >= 1 else f"{a:.3g}"


def sol_txt(n):
    return f"{abs(n):.3f}" if abs(n) >= 1 else f"{abs(n):.4f}"


def usd_txt(n):
    return "" if n is None else money(n) if n >= 1 else f"${n:.2f}"


def price_txt(n):
    return "" if not n else f"${n:.2f}" if n >= 1 else f"${n:.3g}"


def coin_txt(leg):
    m = leg["mint"]
    return "$" + e(leg.get("symbol") or m[:4] + "…" + m[-4:])


def who(w):
    return e(w.get("label") or w["address"][:4] + "…" + w["address"][-4:])


def short(a):
    return e(a[:4] + "…" + a[-4:])


def stable_txt(n):
    return f"${abs(n):.2f} in USDC/USDT"


def paid(ev):
    return f"{sol_txt(ev['sol'])} SOL" if abs(ev["sol"]) > 0.0005 else stable_txt(ev["stable"]) if abs(ev["stable"]) > 0.005 else f"${(ev.get('usd') or 0):.2f}"


def activity_line(ev):
    k, legs = ev["kind"], ev["legs"]
    l = legs[0] if legs else None
    if k == "buy":
        return f"Bought {amt(l['amount'])} {coin_txt(l)} for {paid(ev)}" + (f" ({usd_txt(ev['usd'])})" if (ev.get("usd") or 0) >= 1 and abs(ev["sol"]) > 0.0005 else "")
    if k == "sell":
        if ev.get("dust"):
            return f"Sold {amt(l['amount'])} {coin_txt(l)} for dust"
        return f"Sold {amt(l['amount'])} {coin_txt(l)} for {paid(ev)}" + (f" ({usd_txt(ev['usd'])})" if (ev.get("usd") or 0) >= 1 and abs(ev["sol"]) > 0.0005 else "")
    if k == "swap":
        if not legs:
            return "Swapped " + (f"{sol_txt(ev['sol'])} SOL for {stable_txt(ev['stable'])}" if ev["sol"] < 0 else f"{stable_txt(ev['stable'])} for {sol_txt(ev['sol'])} SOL")
        out = " + ".join(f"{amt(x['amount'])} {coin_txt(x)}" for x in legs if x["amount"] < 0)
        got = " + ".join(f"{amt(x['amount'])} {coin_txt(x)}" for x in legs if x["amount"] > 0)
        return f"Swapped {out} for {got}" + (f" ({usd_txt(ev['usd'])})" if ev.get("usd") else "")
    if k == "transfer_in":
        return f"Received {amt(l['amount'])} {coin_txt(l)}" + (f" from {short(ev['from'][0])}" if ev.get("from") else "")
    if k == "transfer_out":
        return f"Sent {amt(l['amount'])} {coin_txt(l)}" + (f" to {short(ev['to'][0])}" if ev.get("to") else "")
    if k == "deposit":
        return "Received " + (f"{sol_txt(ev['sol'])} SOL" if abs(ev["sol"]) > 0.0005 else stable_txt(ev["stable"])) + (f" from {short(ev['from'][0])}" if ev.get("from") else "")
    if k == "withdraw":
        return "Sent " + (f"{sol_txt(ev['sol'])} SOL" if abs(ev["sol"]) > 0.0005 else stable_txt(ev["stable"])) + (f" to {short(ev['to'][0])}" if ev.get("to") else "")
    return k


TITLE = {"buy": "🟢 Buy", "sell": "🔴 Sell", "swap": "🔄 Swap", "transfer_in": "📥 Transfer in", "transfer_out": "📤 Transfer out",
         "deposit": "📥 Deposit", "withdraw": "📤 Withdrawal"}


def http_url(u):
    return u if isinstance(u, str) and re.match(r"https?://", u) else None


def activity_message(w, ev):
    legs = ev["legs"]
    l = next((x for x in legs if x["amount"] > 0), legs[0] if legs else None)
    m = ev.get("market")
    lines = [f"<b>{TITLE.get(ev['kind'], '🔔 Activity')} · {who(w)}</b>", activity_line(ev)]
    at = " (found later)" if m and m.get("late") else ""
    facts = [f"{'Sold at' if ev['kind'] == 'sell' else 'Paid'} {price_txt(ev['priceUsd'])} per token (fees included)" if ev.get("priceUsd") else "",
             f"MC {money(m['marketCap'])}{at}" if m and m.get("marketCap") else "", f"Liq {money(m['liquidity'])}{at}" if m and m.get("liquidity") else ""]
    facts = [x for x in facts if x]
    if facts:
        lines.append(" · ".join(facts))
    if l:
        lines.append(f"<code>{e(l['mint'])}</code>")
    links = [f'<a href="https://solscan.io/tx/{ea(ev["sig"])}">Tx</a>']
    if l:
        url = http_url(m.get("url") if m else None) or "https://www.geckoterminal.com/solana/tokens/" + l["mint"]
        links.append(f'<a href="{ea(url)}">{"DexScreener" if "dexscreener.com" in url else "GeckoTerminal"}</a>')
        if BASE58.fullmatch(l["mint"]) and (((ev.get("pumpfun") and ev["kind"] != "swap") or (m and m.get("pumpfun"))) or l["mint"].endswith("pump")):
            links.append(f'<a href="https://pump.fun/coin/{ea(l["mint"])}">pump.fun</a>')
        links.append(f'<a href="{ea(PUBLIC_URL)}/#coins/{ea(l["mint"])}">Coin check</a>')
    links.append(f'<a href="{ea(PUBLIC_URL)}/#wallets/{ea(w["address"])}">Wallet</a>')
    lines.append(" · ".join(links))
    return "\n".join(lines)


def summary_message(w, rest, skipped):
    def count(k):
        return sum(1 for x in rest if x["kind"] == k)
    parts = [f"{count(k)} {n}" for k, n in (("buy", "buys"), ("sell", "sells"), ("swap", "swaps")) if count(k)]
    other = len(rest) - sum(1 for x in rest if x["kind"] in TRADES)
    if other:
        parts.append(f"{other} transfers")
    text = ". ".join(x for x in (f"{len(rest)} more: {', '.join(parts)}" if rest else "",
                                 f"{skipped}{'+' if skipped >= 300 else ''} older transactions weren't checked" if skipped else "") if x) + "."
    return "\n".join([f"<b>⚡ Busy wallet · {who(w)}</b>", text,
                      f'<a href="{ea(PUBLIC_URL)}/#monitor">Wallet monitor</a> · <a href="https://solscan.io/account/{ea(w["address"])}">Solscan</a>'])


class Sender:
    """Telegram sends in order, one a 1.1 s (the per-chat limit), off the checking threads."""

    def __init__(self, data):
        self.data, self.q = data, queue.Queue(maxsize=2000)
        threading.Thread(target=self.run, daemon=True).start()

    def send(self, text):
        try:
            self.q.put_nowait(text)
        except queue.Full:
            pass

    def run(self):
        while True:
            text = self.q.get()
            cfg = tg_load(self.data)
            ok, why = notify.send(cfg, text)
            if not ok:
                print("telegram:", why, flush=True)
                if "429" in why or "Too Many" in why:
                    try:
                        self.q.put_nowait(text)
                    except queue.Full:
                        pass
                    time.sleep(5)
            time.sleep(1.1)


# ---- market data per coin ---------------------------------------------------------------------------------------------

_tokens = {}      # mint -> (time, info or None)
_looking = {}     # mint -> Event set when a lookup in progress has finished
_tok_lock = threading.Lock()


def token_info(mints):
    """Market data per coin. A check that needs a coin another thread is already looking up waits for that lookup."""
    now = time.time()
    waits, want = [], []
    with _tok_lock:
        if len(_tokens) > 2000:
            for m in [m for m, c in _tokens.items() if now - c[0] > 300]:
                del _tokens[m]
        for m in mints:
            if m in _looking:
                waits.append(_looking[m])
                continue
            c = _tokens.get(m)
            if c is None or now - c[0] > (300 if c[1] else 60):
                want.append(m)
        done = threading.Event()
        for m in want:
            _looking[m] = done
    if want:
        try:
            got, failed = sources.lookup_tokens(want)
        except Exception:
            got, failed = {}, set(want)
        with _tok_lock:
            for m in want:
                old = _tokens.get(m, (0, None))[1]
                _tokens[m] = (0, old) if (m in failed and m not in got) else (time.time(), got.get(m) or old)
                _looking.pop(m, None)
        done.set()
    for ev in waits:
        ev.wait(20)
    with _tok_lock:
        return {m: _tokens.get(m, (0, None))[1] for m in mints}


# ---- events -----------------------------------------------------------------------------------------------------------

def build_event(row, info, usd):
    legs = [{"mint": m, "amount": a, "symbol": (info.get(m) or {}).get("symbol")} for m, a in row["memes"].items()]
    base = {"t": int((row["t"] or 0) * 1000), "sig": row["sig"], "sol": row["sol"], "stable": row["stable"], "usd": abs(row["quote"]), "legs": legs}
    k, coin = row["kind"], (legs[0] if legs else None)
    if k in ("buy", "sell"):
        return {**base, "kind": k, "priceUsd": abs(row["quote"]) / abs(coin["amount"]) if coin else None, "market": info.get(coin["mint"]) if coin else None}
    if k == "dispose":
        return None if row["basicOnly"] else {**base, "kind": "sell", "dust": True, "market": info.get(coin["mint"]) if coin else None}
    if k == "multi":
        if any(l["amount"] < 0 for l in legs) and any(l["amount"] > 0 for l in legs):
            return {**base, "kind": "swap", "usd": sum(row["swapGiven"].values()) if row["swapGiven"] else None}
        buying = all(l["amount"] > 0 for l in legs) and row["quote"] < -P.DUST_USD
        selling = all(l["amount"] < 0 for l in legs) and row["quote"] > P.DUST_USD and not row["basicOnly"]
        if not buying and not selling:
            return None
        priced = all(((info.get(l["mint"]) or {}).get("priceUsd") or 0) > 0 for l in legs)

        def size(l):
            return abs(l["amount"]) * (info[l["mint"]]["priceUsd"] if priced else 1)
        main = max(legs, key=size)
        if any(l is not main and size(l) >= size(main) * (0.01 if priced else 1e-6) for l in legs):
            return None
        return {**base, "legs": [main] + [l for l in legs if l is not main], "kind": "buy" if buying else "sell",
                "priceUsd": abs(row["quote"]) / abs(main["amount"]), "market": info.get(main["mint"])}
    if k == "convert":
        return {**base, "kind": "swap", "usd": abs(row["sol"]) * usd}
    if k == "tok_in":
        return {**base, "kind": "transfer_in", "from": row["cps"]}
    if k == "tok_out":
        return {**base, "kind": "transfer_out", "to": row["cps"]}
    if k == "deposit":
        return {**base, "kind": "deposit", "from": row["cps"]}
    if k == "withdraw":
        return {**base, "kind": "withdraw", "to": row["cps"]}
    return None


def to_event(row, info, usd):
    ev = build_event(row, info, usd)
    if ev and ev["kind"] in ("buy", "sell") and ev["legs"] and ev["legs"][0]["mint"] in row.get("pumpMints", []):
        ev["pumpfun"] = True
    return ev


# ---- the service ------------------------------------------------------------------------------------------------------

class Monitor:
    def __init__(self, data):
        self.data = data
        self.state_path = os.path.join(data, "wallet-watch.json")
        st = read_json(self.state_path, {})
        self.wallets = st.get("wallets", {}) if isinstance(st.get("wallets"), dict) else {}
        self.poll, self.slowed_by, self.clean = BASE_POLL, None, 0
        self.sender = Sender(data)
        self.busy = set()
        self.lock = threading.Lock()
        self.pruned = 0.0

    # -- signatures
    def new_signatures(self, w, address, since_ms=None):
        """New signatures since w["lastSig"], oldest first: (signatures, skipped, started). With no starting point yet, it is the newest
        transaction from before the wallet was added or resumed (`since_ms`), so a trade in the seconds between the click and the first
        check is still reported; a wallet with no `since_ms` starts at its newest transaction and reports nothing from before."""
        if not w.get("lastSig"):
            page = solana.signatures(self.data, address, limit=100)
            w["baselineAt"] = int(time.time() * 1000)
            if not page:
                return [], 0, True                  # an empty wallet: anything that arrives later is newer than the cut
            cut = since_ms / 1000 if since_ms else None
            if cut is None:
                w["lastSig"] = page[0]["signature"]
                return [], 0, True
            idx = next((i for i, x in enumerate(page) if x.get("blockTime") and x["blockTime"] <= cut), None)
            if idx is None:
                idx = len(page) if len(page) < 100 else len(page) - 1      # all newer than the cut (or a very busy wallet)
            w["lastSig"] = page[0]["signature"]
            return [x["signature"] for x in reversed(page[:idx]) if not x.get("err")], 0, False
        out, before = [], None
        while True:
            page = solana.signatures(self.data, address, before=before, until=w["lastSig"], limit=100)
            if not page:
                break
            out += page
            if len(page) < 100 or len(out) >= MAX_BACKLOG + 100:
                break
            before = page[-1]["signature"]
        if not out:
            return [], 0, False
        w["lastSig"] = out[0]["signature"]
        ok = [x for x in out if not x.get("err")]
        kept = ok[:MAX_BACKLOG]
        return [x["signature"] for x in reversed(kept)], len(ok) - len(kept), False

    def check_wallet(self, cfg, budget):
        address = cfg["address"]
        with self.lock:
            if address in self.busy:
                return
            self.busy.add(address)
            w = self.wallets.setdefault(address, {"events": [], "pending": [], "trades": 0})
        try:
            sigs, skipped, _ = self.new_signatures(w, address, cfg.get("added"))
            pending = w.setdefault("pending", [])
            for s in sigs:
                if not any(p["sig"] == s for p in pending):
                    pending.append({"sig": s, "tries": 0})
            if skipped:
                w["skipped"] = w.get("skipped", 0) + skipped
            usd = solana.sol_usd_now() if pending else 0
            if pending and usd > 0:
                want = [p["sig"] for p in pending]
                txs = solana.get_transactions(self.data, want, workers=2)
                for s in want:
                    if s not in txs:
                        try:
                            tx = solana.get_transaction(self.data, s, commitment="confirmed", use_cache=False)
                        except solana.RPCError:
                            tx = None
                        if tx:
                            txs[s] = tx
                ready, still = [], []
                for p in pending:
                    if p["sig"] in txs:
                        ready.append(txs[p["sig"]])
                    elif p["tries"] + 1 < PENDING_TRIES:
                        still.append({**p, "tries": p["tries"] + 1})
                    else:
                        w["lost"] = w.get("lost", 0) + 1
                rows = []
                for tx in ready:
                    if (tx.get("meta") or {}).get("err"):
                        continue
                    try:
                        rows.append(P.parse_tx(tx, address, lambda t: usd))
                    except Exception:
                        w["lost"] = w.get("lost", 0) + 1
                info = token_info(sorted({m for r in rows for m in r["memes"]}))
                events = [x for x in (to_event(r, info, usd) for r in rows) if x]
                w["pending"] = still
                trades = [x for x in events if x["kind"] in TRADES]
                for ev in trades:
                    w["events"].insert(0, ev)
                w["trades"] = w.get("trades", 0) + len(trades)
                w["events"] = w["events"][:MAX_EVENTS]
                if trades:
                    w["lastActivity"] = trades[-1]["t"]
                self.report(cfg, w, events, skipped, budget, usd)
            elif skipped:
                self.report(cfg, w, [], skipped, budget, usd)
            w["lastCheck"], w["error"] = int(time.time() * 1000), None
        except Exception as ex:      # one wallet's trouble never stops the others
            w["error"], w["lastCheck"] = str(ex)[:200], int(time.time() * 1000)
        finally:
            with self.lock:
                self.busy.discard(address)

    def report(self, cfg, w, events, skipped, budget, usd):
        s = self.settings
        if not s["telegram"] or cfg.get("muted"):
            return
        tg = tg_load(self.data)
        if not (tg.get("token") and tg.get("chat_id")):
            return

        def wanted(ev):
            if ev["kind"] in TRADES:
                return not (s["min_sol"] > 0 and ev.get("usd") is not None and usd > 0 and ev["usd"] / usd < s["min_sol"])
            return bool(s["transfers"])
        send = list(w.get("backlog") or []) + [ev for ev in events if wanted(ev)]
        skipped += w.get("carriedSkipped", 0)
        w["backlog"], w["carriedSkipped"] = [], 0
        if not send and not skipped:
            return
        with budget["lock"]:
            left = budget["left"]
        if left <= 0:
            w["backlog"], w["carriedSkipped"] = send[-500:], skipped
            return
        cap = min(MAX_MESSAGES_PER_CHECK, left)
        n = len(send) if len(send) <= cap and not skipped else max(0, cap - 1)
        for ev in send[:n]:
            self.sender.send(activity_message({**cfg}, ev))
        rest = send[n:]
        summary = bool(rest) or skipped > 0
        if summary:
            self.sender.send(summary_message(cfg, rest, skipped))
        with budget["lock"]:
            budget["left"] -= n + (1 if summary else 0)
        w["sent"] = w.get("sent", 0) + n + (1 if summary else 0)

    def round(self):
        doc = load_watch(self.data)
        self.settings = doc["settings"]
        wallets = doc["wallets"]
        known = {c["address"] for c in wallets}
        with self.lock:
            for a in [a for a in self.wallets if a not in known]:
                del self.wallets[a]
        todo = []
        for cfg in wallets:
            w = self.wallets.get(cfg["address"])
            if cfg.get("paused"):
                if w is not None:
                    w["pausedSeen"] = True
                continue
            if w is not None and w.pop("pausedSeen", False):       # resumed: start from now, activity while paused is not reported
                w["lastSig"], w["pending"] = None, []
            todo.append(cfg)
        budget = {"left": max(3, int(self.poll / 1.5)), "lock": threading.Lock()}
        q = list(todo)
        qlock = threading.Lock()

        def worker():
            while True:
                with qlock:
                    if not q:
                        return
                    cfg = q.pop(0)
                self.check_wallet(cfg, budget)
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(PARALLEL)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(150)
        self.backfill()
        if time.time() - self.pruned > 3600:
            self.pruned = time.time()
            solana.prune_cache(self.data)
        self.write()

    def backfill(self):
        """Trades of coins DexScreener had not indexed yet are saved without a symbol or market cap; for 30 minutes they are
        looked up again (15 coins a round). Market data found this way is marked `late`."""
        since = (time.time() - 1800) * 1000
        todo, mints = [], set()
        for w in self.wallets.values():
            for ev in w.get("events", []):
                if ev["t"] < since or not ev.get("legs"):
                    continue
                miss = [l["mint"] for i, l in enumerate(ev["legs"]) if not l.get("symbol") or (i == 0 and ev["kind"] in ("buy", "sell") and not ev.get("market"))]
                if miss:
                    todo.append(ev)
                    mints.update(miss)
        if not todo:
            return
        now = time.time()
        with _tok_lock:
            ask = [m for m in mints if (m not in _tokens) or now - _tokens[m][0] > (300 if _tokens[m][1] else 60)][:15]
        if not ask:
            return
        try:
            info = token_info(sorted(set(ask) | {m for m in mints if m not in ask}))
        except Exception:
            return
        for ev in todo:
            for l in ev["legs"]:
                if not l.get("symbol") and (info.get(l["mint"]) or {}).get("symbol"):
                    l["symbol"] = info[l["mint"]]["symbol"]
            m = info.get(ev["legs"][0]["mint"])
            if ev["kind"] in ("buy", "sell") and not ev.get("market") and m:
                ev["market"] = {**m, "late": True}

    def adjust_pace(self, since):
        limited = solana.rate_limited_since(since)
        if limited:
            self.clean = 0
            self.poll = min(MAX_POLL, self.poll * 2)
            self.slowed_by = "the RPC key was rate-limited"
        elif self.poll > BASE_POLL:
            self.clean += 1
            if self.clean >= 3:
                self.clean = 0
                self.poll = max(BASE_POLL, self.poll / 2)
                self.slowed_by = None if self.poll == BASE_POLL else "recovering after rate limits"

    def write(self):
        with self.lock:
            snap = copy.deepcopy(self.wallets)
        write_json(self.state_path, {"version": 1, "updated": int(time.time() * 1000), "pollSeconds": self.poll, "basePollSeconds": BASE_POLL,
                                     "slowedBy": self.slowed_by, "wallets": snap})

    def run(self):
        print(f"wallet monitor started; RPC {str(len(solana.active_pool(self.data)[0])) + ' key(s)' if solana.rpc_url(self.data)[1] else 'public (slow)'}", flush=True)
        while True:
            t0 = time.time()
            try:
                self.round()
                self.adjust_pace(t0)
            except Exception as ex:     # never let one bad round stop the monitor
                print("round failed:", repr(ex)[:200], flush=True)
            time.sleep(max(0.5, self.poll - (time.time() - t0)))


def view(data, wallet=None, kind=None):
    """What the Monitor page shows: the saved wallets and options joined with the service's state."""
    doc, st = load_watch(data), read_json(os.path.join(data, "wallet-watch.json"), {})
    states = st.get("wallets", {}) if isinstance(st.get("wallets"), dict) else {}
    rows, feed = [], []
    for c in doc["wallets"]:
        w = states.get(c["address"], {})
        trades = [x for x in w.get("events", []) if x.get("kind") in TRADES]
        rows.append({**c, "pending": len(w.get("pending") or []), "events": w.get("trades", len(trades)), "last": trades[0] if trades else None,
                     "lastCheck": w.get("lastCheck"), "error": w.get("error"), "sent": w.get("sent", 0), "lost": w.get("lost", 0),
                     "skipped": w.get("skipped", 0), "started": bool(w.get("lastSig"))})
        if not wallet or c["address"] == wallet:
            feed += [{**x, "wallet": c["address"], "label": c.get("label", "")} for x in trades if kind != "buy" or x["kind"] == "buy"]
    feed.sort(key=lambda x: -x["t"])
    use, custom = solana.active_pool(data)
    return {"settings": doc["settings"], "pollSeconds": st.get("pollSeconds", BASE_POLL), "basePollSeconds": BASE_POLL, "slowedBy": st.get("slowedBy"),
            "updated": st.get("updated"), "maxWallets": MAX_WALLETS, "telegram": tg_public(tg_load(data)), "wallets": rows, "wallet": wallet, "kind": kind,
            "feed": feed[:100], "rpc": {"custom": custom, "keys": len(use) if custom else 0, "mode": solana.load_pool(data)[0], "keys_list": [{"id": e["id"], "label": e.get("label", ""), "key": solana.mask(e["url"]), "enabled": bool(e.get("enabled", True))} for e in solana.load_pool(data)[1]]}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    Monitor(a.data).run()


if __name__ == "__main__":
    main()

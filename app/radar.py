"""The web API of the Coins, Wallets and Monitor pages (everything under /api/radar/). server.py hands every such request to
handle(); each call returns (status, object). The account only: a guest session never reaches these (server.require_login).
"""
import json
import os
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

import coins
import jobs
import monitor
import notify
import solana
import wallets

ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def fail(message, status=400):
    return status, {"error": message}


def check_address(a):
    s = str(a or "").strip()
    try:
        ok = bool(ADDRESS.fullmatch(s)) and len(wallets.b58decode(s)) == 32
    except Exception:
        ok = False
    if not ok:
        raise ValueError("That isn't a Solana wallet address.")
    return s


def clean_label(x):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(x or "")).strip()[:40]


def job_answer(data, kind, key, params, peek, refresh):
    """The state of a heavy job (jobs.py runs it): none / running / done / error. A first request starts it; polls pass peek=1."""
    folder = jobs.jobs_dir(data)
    state_path = os.path.join(folder, key + ".json")
    st = jobs.read(state_path)
    now = int(time.time() * 1000)
    queued = os.path.exists(os.path.join(folder, key + ".req.json"))
    if st and st.get("status") == "running" and now - (st.get("heartbeat") or 0) > (1_800_000 if queued else 180_000):
        st = {**st, "status": "error", "error": "The analysis stopped (the worker is not running). Start it again."}
    if st and st.get("status") == "running":
        return {"status": "running", "progress": st.get("progress") or {}, "startedAt": st.get("startedAt")}
    if st and st.get("status") == "done" and not refresh:
        return {"status": "done", "result": st["result"]}
    if peek and not refresh:
        if st and st.get("status") == "error":
            return {"status": "error", "error": st.get("error"), "cancelled": bool(st.get("cancelled"))}
        return {"status": "none"}
    os.makedirs(folder, exist_ok=True)
    jobs.write(state_path, {"status": "running", "startedAt": now, "heartbeat": now, "progress": {"stage": "Queued"}, "kind": kind, "params": params})
    jobs.write(os.path.join(folder, key + ".req.json"), {"kind": kind, "params": params})
    return {"status": "running", "progress": {"stage": "Queued"}, "startedAt": now}


def job_cancel(data, key):
    folder = jobs.jobs_dir(data)
    st = jobs.read(os.path.join(folder, key + ".json"))
    if st and st.get("status") == "running":
        os.makedirs(folder, exist_ok=True)
        try:
            os.remove(os.path.join(folder, key + ".req.json"))          # still queued: it never starts
            jobs.write(os.path.join(folder, key + ".json"), {**st, "status": "error", "error": "Cancelled.", "cancelled": True})
        except OSError:
            open(os.path.join(folder, key + ".cancel"), "w").close()   # running: the worker stops it
    return {"ok": True}


# What a guest session may do on the Coins Review and Wallets Review pages (server.require_login checks the same sets): everything
# those two pages need and nothing of the Monitor or the RPC keys. A guest's saved wallets are kept apart from the owner's.
GUEST_GET = {"coins/review", "traders", "wallet", "bookmarks"}
GUEST_POST = {"traders/cancel", "wallet/cancel", "bookmarks", "bookmarks/remove"}


def bookmarks_path(data, guest=False):
    return os.path.join(data, "settings", "bookmarks_guest.json" if guest else "bookmarks.json")


def load_bookmarks(data, guest=False):
    doc = monitor.read_json(bookmarks_path(data, guest), [])
    return [b for b in doc if isinstance(b, dict) and ADDRESS.fullmatch(str(b.get("address", "")))] if isinstance(doc, list) else []


def rpc_view(data):
    """The saved RPC keys for the page: a masked key and the provider's host, never the address itself."""
    mode, eps = solana.load_pool(data)
    return {"mode": mode, "custom": any(e.get("enabled", True) for e in eps), "keys": [
        {"id": e["id"], "label": e.get("label", ""), "key": solana.mask(e["url"]), "enabled": bool(e.get("enabled", True))} for e in eps]}


def handle(method, path, query, body, data, lock, guest=False):
    """path is below /api/radar/. body is the parsed JSON (POST) or None. `lock` serialises the saves of settings/watch.json."""
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    if guest and path not in (GUEST_GET if method == "GET" else GUEST_POST):
        return fail("The guest account can use the Lab, Trading, Coins Review and Wallets Review pages only.", 403)
    try:
        if method == "GET":
            if path == "monitor":
                return 200, monitor.view(data, q.get("wallet") or None, q.get("kind"))
            if path == "rpc":
                return 200, rpc_view(data)
            if path == "coins/review":
                try:
                    return 200, coins.review(q.get("q", ""))
                except coins.LookupFailed as ex:
                    return fail(str(ex), ex.status if ex.status in (400, 404, 429) else 502)
            if path == "traders":
                mint = q.get("mint", "")
                if not ADDRESS.fullmatch(mint):
                    return fail("That isn't a valid token mint address.")
                return 200, job_answer(data, "traders", "traders-" + mint, {"mint": mint}, q.get("peek") == "1", q.get("refresh") == "1")
            if path == "wallet":
                address = check_address(q.get("address"))
                try:
                    days = max(1, min(30, int(q.get("days", "30"))))
                except ValueError:
                    days = 90
                all_tr = q.get("allTransfers") == "1"
                return 200, job_answer(data, "wallet", f"wallet-{address}-{days}{'-all' if all_tr else ''}", {"address": address, "days": days, "allTransfers": all_tr},
                                       q.get("peek") == "1", q.get("refresh") == "1")
            if path == "bookmarks":
                return 200, {"wallets": load_bookmarks(data, guest)}
            return fail("not found", 404)
        if not isinstance(body, dict):
            return fail("Could not read the request.")
        if path == "traders/cancel":
            mint = str(body.get("mint") or "")
            return (200, job_cancel(data, "traders-" + mint)) if ADDRESS.fullmatch(mint) else fail("That isn't a valid token mint address.")
        if path == "wallet/cancel":
            address = check_address(body.get("address"))
            try:
                days = max(1, min(30, int(body.get("days", 30))))
            except (TypeError, ValueError):
                days = 90
            return 200, job_cancel(data, f"wallet-{address}-{days}{'-all' if body.get('allTransfers') else ''}")
        if path == "bookmarks":
            with lock:
                a, label = check_address(body.get("address")), clean_label(body.get("label"))
                lst = load_bookmarks(data, guest)
                ex = next((b for b in lst if b["address"] == a), None)
                if ex:
                    ex["label"] = label or ex.get("label", "")
                elif len(lst) >= 100:
                    return fail("You can save up to 100 wallets.")
                else:
                    lst.append({"address": a, "label": label})
                os.makedirs(os.path.join(data, "settings"), exist_ok=True)
                monitor.write_json(bookmarks_path(data, guest), lst, 0o600)
            return 200, {"wallets": lst}
        if path == "bookmarks/remove":
            with lock:
                lst = [b for b in load_bookmarks(data, guest) if b["address"] != body.get("address")]
                os.makedirs(os.path.join(data, "settings"), exist_ok=True)
                monitor.write_json(bookmarks_path(data, guest), lst, 0o600)
            return 200, {"wallets": lst}
        if path == "monitor/wallets":
            with lock:
                doc = monitor.load_watch(data)
                a = check_address(body.get("address"))
                label = clean_label(body.get("label"))
                existing = next((w for w in doc["wallets"] if w["address"] == a), None)
                if existing:
                    if label:
                        existing["label"] = label
                elif len(doc["wallets"]) >= monitor.MAX_WALLETS:
                    return fail(f"You can monitor up to {monitor.MAX_WALLETS} wallets. Remove one first.")
                else:
                    doc["wallets"].append({"address": a, "label": label, "muted": False, "paused": False, "added": int(time.time() * 1000)})
                monitor.save_watch(data, doc)
            return 200, monitor.view(data)
        if path == "monitor/wallets/update":
            with lock:
                doc = monitor.load_watch(data)
                w = next((x for x in doc["wallets"] if x["address"] == body.get("address")), None)
                if not w:
                    return fail("That wallet isn't being monitored.", 404)
                if "label" in body:
                    w["label"] = clean_label(body["label"])
                if "muted" in body:
                    w["muted"] = bool(body["muted"])
                if "paused" in body:
                    if w.get("paused") and not body["paused"]:
                        w["added"] = int(time.time() * 1000)      # resumed: report from this moment on
                    w["paused"] = bool(body["paused"])
                monitor.save_watch(data, doc)
            return 200, monitor.view(data)
        if path == "monitor/wallets/remove":
            with lock:
                doc = monitor.load_watch(data)
                doc["wallets"] = [w for w in doc["wallets"] if w["address"] != body.get("address")]
                monitor.save_watch(data, doc)
            return 200, monitor.view(data)
        if path == "monitor/settings":
            with lock:
                doc = monitor.load_watch(data)
                s = doc["settings"]
                if "telegram" in body:
                    s["telegram"] = bool(body["telegram"])
                if "transfers" in body:
                    s["transfers"] = bool(body["transfers"])
                if "min_sol" in body:
                    try:
                        v = float(body["min_sol"])
                    except (TypeError, ValueError):
                        v = -1
                    if not 0 <= v <= 10000:
                        return fail("The minimum trade size must be between 0 and 10,000 SOL.")
                    s["min_sol"] = v
                monitor.save_watch(data, doc)
            return 200, monitor.view(data)
        if path == "monitor/telegram":
            try:
                monitor.tg_save(data, body.get("token"), body.get("chat_id"))
            except ValueError as ex:
                return fail(str(ex))
            return 200, monitor.view(data)
        if path == "monitor/telegram/clear":
            monitor.tg_clear(data)
            return 200, monitor.view(data)
        if path == "monitor/telegram/test":
            cfg = monitor.tg_load(data)
            token = str(body.get("token") or "").strip() or cfg.get("token") or ""
            chat = str(body.get("chat_id") or "").strip() or cfg.get("chat_id") or ""
            ok, why = notify.send({"token": token, "chat_id": chat},
                                  "<b>✅ Wallet monitor is connected</b>\nYou will get a message here for each trade of the wallets you monitor.")
            return (200, {"ok": True}) if ok else fail(why)
        if path == "monitor/telegram/chats":
            token = str(body.get("token") or "").strip() or monitor.tg_load(data).get("token") or ""
            if not notify.TOKEN.fullmatch(token):
                return fail("Paste the bot token first.")
            try:
                req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getUpdates?limit=50&timeout=0")
                with urllib.request.urlopen(req, timeout=15) as r:
                    updates = json.loads(r.read()).get("result") or []
            except (urllib.error.URLError, OSError, ValueError):
                return fail("Telegram could not be reached, or the token is wrong.")
            chats = {}
            for u in updates:
                m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or u.get("edited_message") or {}
                c = m.get("chat")
                if c:
                    name = (c.get("title") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
                            or ("@" + c["username"] if c.get("username") else str(c["id"])))
                    chats[str(c["id"])] = {"id": str(c["id"]), "type": c.get("type"), "name": name}
            return 200, {"chats": list(chats.values())}
        if path == "rpc/add":
            url = solana.normalize_key(body.get("key"))
            if any(e["url"] == url for e in solana.load_pool(data)[1]):
                return fail("That key is already in the list.")
            result, err = solana.test(url)              # (a network call: not under the lock that serialises every settings save)
            if err:
                return fail(err)
            with lock:
                mode, eps = solana.load_pool(data)
                if any(e["url"] == url for e in eps):
                    return fail("That key is already in the list.")
                if len(eps) >= solana.MAX_KEYS:
                    return fail(f"You can save up to {solana.MAX_KEYS} keys.")
                label = clean_label(body.get("label")) or f"Key {len(eps) + 1}"
                eps.append({"id": uuid.uuid4().hex[:12], "label": label, "url": url, "enabled": True, "added": int(time.time() * 1000)})
                solana.save_pool(data, mode, eps)
            return 200, {**rpc_view(data), "tested": result}
        if path == "rpc/test":
            ep = next((e for e in solana.load_pool(data)[1] if e["id"] == body.get("id")), None)
            if not ep:
                return fail("That key is not in the list.", 404)
            result, err = solana.test(ep["url"])
            return fail(err) if err else (200, result)
        if path in ("rpc/update", "rpc/remove"):
            with lock:
                mode, eps = solana.load_pool(data)
                ep = next((e for e in eps if e["id"] == body.get("id")), None)
                if not ep:
                    return fail("That key is not in the list.", 404)
                if path == "rpc/remove":
                    eps = [e for e in eps if e is not ep]
                    if ep["id"] == "single":                 # the older single address (analysis_rpc.json) goes with it
                        solana.clear_rpc_url(data)
                else:
                    if "enabled" in body:
                        ep["enabled"] = bool(body["enabled"])
                    if "label" in body:
                        ep["label"] = clean_label(body["label"]) or ep["label"]
                    if ep["id"] == "single":                 # a change to it is saved into the pool, which then replaces the old file
                        solana.clear_rpc_url(data)
                solana.save_pool(data, mode, eps)
            return 200, rpc_view(data)
        if path == "rpc/mode":
            with lock:
                mode, eps = solana.load_pool(data)
                if body.get("mode") not in solana.MODES:
                    return fail("Unknown mode.")
                solana.save_pool(data, body["mode"], eps)
            return 200, rpc_view(data)
        return fail("not found", 404)
    except ValueError as ex:
        return fail(str(ex))
    except Exception as ex:      # odd data from a third party must not drop the connection
        return fail("That failed (%s). Try again." % type(ex).__name__, 502)

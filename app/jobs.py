"""Worker for the heavy jobs of the Coins and Wallets pages (Top traders of a coin, a wallet's P&L review). The web service has a small
memory limit, so it only writes a request file (data/jobs/<key>.req.json) and reads the answer (data/jobs/<key>.json); this
service runs the job and keeps its progress there. data/jobs/<key>.cancel stops it.
    python3 jobs.py --data data
"""
import argparse
import json
import os
import tempfile
import threading
import time
import traceback

import solana
import traders
import wallet_review

KEEP_DAYS = 3
MAX_PARALLEL = 2


def jobs_dir(data):
    return os.path.join(data, "jobs")


def write(path, doc):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".w", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Cancel(threading.Event):
    """An Event the worker also sets when the cancel file appears."""

    def __init__(self, path):
        super().__init__()
        self.path = path

    def is_set(self):
        return super().is_set() or os.path.exists(self.path)


def run_job(data, key, req):
    folder = jobs_dir(data)
    state_path = os.path.join(folder, key + ".json")
    cancel_path = os.path.join(folder, key + ".cancel")
    try:
        os.remove(cancel_path)
    except OSError:
        pass
    cancel = Cancel(cancel_path)
    started = int(time.time() * 1000)
    state = {"status": "running", "startedAt": started, "progress": {"stage": "Starting"}, "heartbeat": started, "kind": req["kind"], "params": req["params"]}
    write(state_path, state)
    last = [0.0]
    alive = threading.Event()

    def beat():                  # long stages report no progress: keep the heartbeat moving so the page does not call the job dead
        while not alive.wait(20):
            state["heartbeat"] = int(time.time() * 1000)
            try:
                write(state_path, state)
            except OSError:
                pass
    threading.Thread(target=beat, daemon=True).start()

    def progress(**k):
        now = time.time()
        if now - last[0] < 0.8 and k.get("done") not in (None, k.get("total")):
            return
        last[0] = now
        state["progress"] = {"stage": k.get("stage", ""), **({"done": k["done"], "total": k["total"]} if k.get("total") else {})}
        state["heartbeat"] = int(now * 1000)
        write(state_path, state)
    try:
        if req["kind"] == "traders":
            result = traders.review_traders(data, req["params"]["mint"], progress, cancel)
        elif req["kind"] == "wallet":
            p = req["params"]
            result = wallet_review.review_wallet(data, p["address"], days=p.get("days", 90), progress=progress, cancel=cancel, all_transfers=bool(p.get("allTransfers")))
        else:
            raise RuntimeError("unknown job")
        state.update({"status": "done", "result": result, "finishedAt": int(time.time() * 1000)})
        state.pop("progress", None)
    except (traders.Cancelled, wallet_review.Cancelled) as e:
        state.update({"status": "error", "error": str(e), "cancelled": True, "finishedAt": int(time.time() * 1000)})
    except (RuntimeError, solana.RPCError) as e:
        state.update({"status": "error", "error": str(e)[:400], "finishedAt": int(time.time() * 1000)})
    except Exception as e:
        traceback.print_exc()
        state.update({"status": "error", "error": "The analysis failed (%s). Try again." % type(e).__name__, "finishedAt": int(time.time() * 1000)})
    alive.set()
    try:
        write(state_path, state)
    except OSError:
        pass
    try:
        os.remove(cancel_path)
    except OSError:
        pass
    solana.prune_cache(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    folder = jobs_dir(a.data)
    os.makedirs(folder, exist_ok=True)
    running = {}
    print("radar jobs worker started", flush=True)
    # a job that was running when this service stopped cannot continue
    for n in os.listdir(folder):
        if n.endswith(".json") and not n.endswith(".req.json"):
            st = read(os.path.join(folder, n))
            if st and st.get("status") == "running":
                st.update({"status": "error", "error": "The analysis stopped because the server restarted. Start it again."})
                write(os.path.join(folder, n), st)
    last_clean = 0.0
    while True:
        for n in sorted(os.listdir(folder)):
            if not n.endswith(".req.json"):
                continue
            key = n[:-9]
            if key in running and running[key].is_alive():
                continue
            if sum(1 for t in running.values() if t.is_alive()) >= MAX_PARALLEL:
                break
            path = os.path.join(folder, n)
            req = read(path)
            try:
                os.remove(path)
            except OSError:
                pass
            if not req or req.get("kind") not in ("traders", "wallet"):
                continue
            t = threading.Thread(target=run_job, args=(a.data, key, req), daemon=True)
            running[key] = t
            t.start()
        if time.time() - last_clean > 3600:
            last_clean = time.time()
            for n in os.listdir(folder):
                p = os.path.join(folder, n)
                try:
                    if time.time() - os.path.getmtime(p) > KEEP_DAYS * 86400:
                        os.remove(p)
                except OSError:
                    pass
        time.sleep(1)


if __name__ == "__main__":
    main()

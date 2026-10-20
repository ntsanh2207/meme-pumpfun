"""The trading wallets kept on the server: private keys the user adds on the Settings page.

Layout in data/settings/: wallets/<address>.json (the key as a Solana key file, a JSON list of 64 numbers,
mode 600), wallets.json (labels and which one is active) and wallet.json, a copy of the active key, which is
the file trader.py loads. A key is never returned by any function that feeds a web response.
Pure Python (no solders), so the web service can check a key and work out its address itself."""
import hashlib
import json
import os
import re
import time

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MAX_WALLETS = 10

# ---- Ed25519 public key from a seed (RFC 8032), to check that a key's two halves belong together ----
_P = 2 ** 255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_G = (15112221349535400772501151409588531511454012693041857206046113283949847762202,
      46316835694926478169428394003475163141307993866256225615783033603165251855960, 1,
      15112221349535400772501151409588531511454012693041857206046113283949847762202 *
      46316835694926478169428394003475163141307993866256225615783033603165251855960 % _P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % _P, (a[1] + a[0]) * (b[1] + b[0]) % _P
    C, D = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _public_from_seed(seed):
    h = bytearray(hashlib.sha512(seed).digest()[:32])
    h[0] &= 248
    h[31] = (h[31] & 127) | 64
    k, q, p = int.from_bytes(h, "little"), (0, 1, 1, 0), _G
    while k:
        if k & 1:
            q = _add(q, p)
        p = _add(p, p)
        k >>= 1
    zi = pow(q[2], _P - 2, _P)
    x, y = q[0] * zi % _P, q[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def b58encode(raw):
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def b58decode(text):
    n = 0
    for ch in text:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + raw


def parse_key(text):
    """(64 key bytes, address) from what a wallet app exports: a base58 string or a JSON list of numbers.
    Raises ValueError with a message that does not repeat the key."""
    text = (text or "").strip()
    try:
        if text.startswith("["):
            nums = json.loads(text)
            if not isinstance(nums, list) or not all(isinstance(n, int) and 0 <= n < 256 for n in nums):
                raise ValueError
            raw = bytes(nums)
        else:
            raw = b58decode(text)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("That is not a private key. Paste the base58 key a wallet app exports, or a key file's list of 64 numbers.") from None
    if len(raw) == 32:
        raw = raw + _public_from_seed(raw)
    if len(raw) != 64:
        raise ValueError(f"The key has {len(raw)} bytes; a Solana private key has 64 (or 32 for the seed alone). "
                         "A seed phrase of words cannot be used: export the private key.")
    if _public_from_seed(raw[:32]) != raw[32:]:
        raise ValueError("The two halves of this key do not belong together, so it is damaged or not a Solana key.")
    return raw, b58encode(raw[32:])


def _dir(settings):
    return os.path.join(settings, "wallets")


def _read_index(settings):
    try:
        with open(os.path.join(settings, "wallets.json"), encoding="utf-8") as f:
            d = json.load(f)
        return {"active": d.get("active") or "", "wallets": [w for w in d.get("wallets", []) if isinstance(w, dict) and w.get("address")]}
    except (OSError, ValueError, AttributeError):
        return {"active": "", "wallets": []}


def _write_private(path, data):
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)      # a leftover file could have looser permissions
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)      # never leave key material in a stray file
        except OSError:
            pass
        raise


def _write_index(settings, idx):
    _write_private(os.path.join(settings, "wallets.json"), json.dumps(idx))


def _loaded_address(settings):
    """The address of wallet.json (put there by hand or by this module), or '' if there is none."""
    try:
        with open(os.path.join(settings, "wallet.json"), encoding="utf-8") as f:
            raw = bytes(json.load(f))
        return b58encode(raw[32:]) if len(raw) == 64 else ""
    except (OSError, ValueError, TypeError):
        return ""


def listing(settings):
    """{"active": address, "wallets": [{address, label, added}]}; a wallet.json put there by hand is listed too."""
    idx, active = _read_index(settings), _loaded_address(settings)
    if active and not any(w["address"] == active for w in idx["wallets"]):
        idx["wallets"].insert(0, {"address": active, "label": "Wallet file", "added": None})
    return {"active": active, "wallets": idx["wallets"]}


def add(settings, key_text, label, activate):
    raw, address = parse_key(key_text)
    idx = _read_index(settings)
    if any(w["address"] == address for w in idx["wallets"]) or address == _loaded_address(settings):
        raise ValueError("That wallet is already added.")
    if len(idx["wallets"]) >= MAX_WALLETS:
        raise ValueError(f"At most {MAX_WALLETS} wallets can be kept: remove one first.")
    cancel_purge(settings, address)   # deleted earlier and added back: its new history must not be wiped
    os.makedirs(_dir(settings), mode=0o700, exist_ok=True)
    _write_private(os.path.join(_dir(settings), address + ".json"), json.dumps(list(raw)))
    idx["wallets"].append({"address": address, "label": (label or "").strip()[:40] or "Wallet " + address[:4], "added": int(time.time() * 1000)})
    _write_index(settings, idx)
    if activate:
        activate_wallet(settings, address)
    return address


def _key_of(settings, address):
    _check_address(address)
    path = os.path.join(_dir(settings), address + ".json")
    if not os.path.exists(path):
        raise ValueError("Unknown wallet.")
    return path


def activate_wallet(settings, address):
    path = _key_of(settings, address)
    with open(path, encoding="utf-8") as f:
        data = f.read()
    _write_private(os.path.join(settings, "wallet.json"), data)
    idx = _read_index(settings)
    idx["active"] = address
    _write_index(settings, idx)


def remove(settings, address):
    """Deletes a wallet's key from the server: its saved key file, and wallet.json when it is the one in use (the
    trader then has no wallet). Returns True when it was the wallet in use. Its funds stay on the chain."""
    _check_address(address)
    in_use = address == _loaded_address(settings)
    if not in_use:
        os.remove(_key_of(settings, address))
    else:
        try:        # a wallet added by hand has no saved copy, only wallet.json
            os.remove(_key_of(settings, address))
        except ValueError:
            pass
        os.remove(os.path.join(settings, "wallet.json"))
    idx = _read_index(settings)
    idx["wallets"] = [w for w in idx["wallets"] if w["address"] != address]
    if idx.get("active") == address:
        idx["active"] = ""
    _write_index(settings, idx)
    return in_use


PURGE_DIR = "wallet_purge"   # one file per deleted wallet, removed by the trader once its trades are gone


def queue_purge(settings, address):
    """Asks the trader to delete every trade this wallet made (trades.db is the trader's, so it does it). One file per
    address, so a new request can never be lost while the trader handles another."""
    _check_address(address)
    folder = os.path.join(settings, PURGE_DIR)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    _write_private(os.path.join(folder, address), str(int(time.time() * 1000)))


def cancel_purge(settings, address):
    try:
        os.remove(os.path.join(settings, PURGE_DIR, address))
    except OSError:
        pass


def _check_address(address):
    if not isinstance(address, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
        raise ValueError("Unknown wallet.")

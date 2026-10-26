"""Telegram alerts for the live watch: the saved bot settings and sending a message.

The settings are saved from the web app (Rule settings tab) into the settings directory as
telegram.json: {"token": bot token, "chat_id": who to write to, "enabled": bool}. Shared by
the web server (which saves them and sends the test message) and watch.py (which sends alerts).
"""
import html
import json
import os
import re
import tempfile
import urllib.error
import urllib.request

FILE = "telegram.json"
TOKEN = re.compile(r"\d{5,12}:[A-Za-z0-9_-]{30,60}")
CHAT = re.compile(r"-?\d{3,20}|@[A-Za-z0-9_]{4,40}")


def load(settings_dir):
    """The saved settings, or {} when nothing usable is saved."""
    try:
        with open(os.path.join(settings_dir, FILE), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def ready(cfg):
    return bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))


def public(cfg):
    """What the web app may show: never the token itself."""
    token, chat = cfg.get("token") or "", str(cfg.get("chat_id") or "")
    out = {"configured": bool(token and chat), "enabled": bool(cfg.get("enabled")),
           "token_hint": "…" + token[-4:] if token else "", "chat_id": chat}
    return out


def update(settings_dir, new):
    """Validate and save. An empty token keeps the saved one. Raises ValueError saying what is wrong."""
    if not isinstance(new, dict):
        raise ValueError("expected an object")
    cfg = load(settings_dir)
    token = str(new.get("token") or "").strip() or cfg.get("token") or ""
    chat = str(new.get("chat_id") or "").strip()
    if token and not TOKEN.fullmatch(token):
        raise ValueError("That does not look like a bot token. BotFather gives one like 123456789:AA…")
    if chat and not CHAT.fullmatch(chat):
        raise ValueError("The user ID must be a number (for example 123456789), or @channelname.")
    cfg2 = {"token": token, "chat_id": chat, "enabled": bool(new.get("enabled")) and bool(token and chat)}
    cfg = cfg2
    fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix="telegram.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.chmod(tmp, 0o600)   # the token lets anyone send as the bot
    os.replace(tmp, os.path.join(settings_dir, FILE))
    return cfg


def esc(x):
    return html.escape(str(x), quote=False)


def send_file(cfg, name, data, caption=""):
    """Send one file (bytes) as a document. Returns (sent, what went wrong)."""
    if not (cfg.get("token") and cfg.get("chat_id")):
        return False, "The bot token and the user ID are not both saved yet."
    bound = "pumpwatch" + os.urandom(8).hex()
    parts = []
    for k, v in (("chat_id", str(cfg["chat_id"])), ("caption", caption[:1000]), ("parse_mode", "HTML")):
        parts.append(f'--{bound}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{bound}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                 f'Content-Type: text/csv\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{bound}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{cfg['token']}/sendDocument", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={bound}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return bool(json.load(r).get("ok")), ""
    except urllib.error.HTTPError as e:
        return False, f"Telegram answered {e.code} to the file."
    except Exception as e:
        return False, f"Telegram could not be reached ({type(e).__name__})."


def send(cfg, text):
    """Send one HTML message. Returns (sent, what went wrong)."""
    if not (cfg.get("token") and cfg.get("chat_id")):
        return False, "The bot token and the user ID are not both saved yet."
    body = json.dumps({"chat_id": cfg["chat_id"], "text": text[:4000], "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{cfg['token']}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(json.load(r).get("ok")), ""
    except urllib.error.HTTPError as e:
        try:
            why = json.load(e).get("description") or str(e.code)
        except ValueError:
            why = str(e.code)
        hint = {401: " The bot token is wrong.", 403: " Open the bot in Telegram and press Start first.",
                400: " Check the user ID, and that you pressed Start on the bot."}.get(e.code, "")
        return False, f"Telegram answered: {why}.{hint}"
    except Exception as e:  # no network, timeout
        return False, f"Telegram could not be reached ({type(e).__name__})."

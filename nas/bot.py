"""gk-bot: a Telegram bot that files shared posts into the Library's "Share here" note.

Share a reel, a TikTok or a YouTube video to the bot: it cleans the link with link.py (the
plugin's own), skips a post that is already in the Library, and appends the link on a new
line of the Obsidian Library's "Share here" note, the format the Worker already reads.
It answers only the Telegram users in TG_ALLOWED. Standard library only.

Environment: TG_TOKEN, TG_ALLOWED (user ids, comma-separated), VAULT (the Library folder),
SHARE_HERE / ALREADY_READ / ENTRIES (names, default Italian), STATE (a writable folder),
TG_API (default https://api.telegram.org). The token and the text of a message are never logged.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from link import parse

TOKEN = os.environ.get("TG_TOKEN", "")
API = os.environ.get("TG_API", "https://api.telegram.org")
ALLOWED = {int(i) for i in re.findall(r"\d+", os.environ.get("TG_ALLOWED", ""))}
VAULT = Path(os.environ.get("VAULT", "/vault"))
SHARE_HERE = os.environ.get("SHARE_HERE", "Condividi qui")
ALREADY_READ = os.environ.get("ALREADY_READ", "Già letti")
ENTRIES = os.environ.get("ENTRIES", "Schede")
STATE = Path(os.environ.get("STATE", "/state"))
HELP = ("Mandami un reel, un TikTok o un video YouTube (dal tasto Condividi) e lo salvo nella Libreria. "
        "Se è già dentro, te lo dico.")


def call(method: str, **params: Any) -> Any:
    data = urlencode(params).encode()
    with urlopen(f"{API}/bot{TOKEN}/{method}", data=data, timeout=70) as response:
        answer = json.load(response)
    return answer.get("result") if answer.get("ok") else None


def known(needle: str) -> bool:
    """Is the post already in the Library: its note, its entries, or what was already read."""
    files = [VAULT / f"{SHARE_HERE}.md", VAULT / f"{ALREADY_READ}.md", *sorted((VAULT / ENTRIES).glob("*.md"))]
    for path in files:
        try:
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
    return False


def save(link: str) -> None:
    note = VAULT / f"{SHARE_HERE}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    with note.open("a", encoding="utf-8") as out:  # one line at the end, as the phone does
        out.write(f"\n{link}\n")


def answer_for(user: int, text: str) -> str | None:
    """What to say to a message, after saving what it shares. None: say nothing."""
    command = text.strip().split()[0].split("@")[0].lower() if text.strip().startswith("/") else ""
    if command == "/id":
        return f"Il tuo ID Telegram è {user}."
    if user not in ALLOWED:
        return "Questo bot è privato."
    if command == "/start":
        return HELP
    found = parse(text)
    if "error" in found:
        return HELP
    link = found["link"]
    if known(str(found.get("id") or link)):
        return "Già in Libreria."
    save(link)
    return f"Salvato ({found['platform']})."


def handle(update: dict[str, Any]) -> None:
    message = update.get("message") or {}
    sender = (message.get("from") or {}).get("id")
    text = message.get("text") or message.get("caption") or ""
    chat = (message.get("chat") or {}).get("id")
    if not (isinstance(sender, int) and isinstance(chat, int) and text):
        return
    reply = answer_for(sender, text)
    if reply:
        call("sendMessage", chat_id=chat, text=reply)


def main() -> None:
    if not TOKEN:
        sys.exit("TG_TOKEN is not set")
    STATE.mkdir(parents=True, exist_ok=True)
    offset_file = STATE / "offset"
    offset = int(offset_file.read_text()) if offset_file.is_file() else 0
    once = os.environ.get("GK_BOT_ONCE") == "1"
    while True:
        try:
            updates = call("getUpdates", offset=offset, timeout=0 if once else 50, allowed_updates='["message"]') or []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle(update)
                except (OSError, URLError, ValueError, KeyError):
                    pass  # one bad message must not stop the bot; the next poll goes on
                offset_file.write_text(str(offset))
        except (OSError, URLError, ValueError):
            time.sleep(5)
        if once:
            return


if __name__ == "__main__":
    main()

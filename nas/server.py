"""gk-fetcher: runs post.py read on the NAS and serves the frames it made.

    POST /read            {"link": "...", "language": null}  ->  post.py read's JSON; the paths
                          of keyframes and thumbnail are relative to the read's folder, "work" is
                          that folder's name, and "read_id" says which read to fetch files from
    GET  /files/<id>/<path>   one file of that read, nothing outside its folder
    POST /clean/<id>      delete that read
    GET  /health          {"ok": true}, no token

Every route but /health needs "Authorization: Bearer $GK_TOKEN". One read at a time. A read nobody
cleans is deleted after GK_TTL seconds. It never logs a header, a link's query or a body.
Standard library only: the same post.py that runs on the computer runs here.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

POST = Path(os.environ.get("GK_POST") or Path(__file__).with_name("post.py"))
WORK = Path(os.environ.get("GK_WORK", "/work"))
TOKEN = os.environ.get("GK_TOKEN", "")
TTL = float(os.environ.get("GK_TTL", "3600"))
READ_LIMIT = float(os.environ.get("GK_READ_LIMIT", str(3 * 3600)))
MIN_GAP = float(os.environ.get("GK_MIN_GAP", "6"))  # seconds between two Instagram reads
HOSTS = ("instagram.com", "instagr.am", "tiktok.com", "youtube.com", "youtu.be")
READ_ID = re.compile(r"graveyard-killer-[A-Za-z0-9_]+")
LANGUAGE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
MARKER = ".graveyard-killer-work"
BODY_LIMIT = 16 * 1024

lock = threading.Lock()  # one read at a time: it keeps the pauses between posts
last_instagram = 0.0


def allowed(link: str) -> bool:
    parts = urlsplit(link)
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in HOSTS)


def folder_of(read_id: str) -> Path | None:
    if not READ_ID.fullmatch(read_id):
        return None
    folder = WORK / read_id
    return folder if (folder / MARKER).is_file() else None


def relative(path: Any, folder: Path) -> str | None:
    try:
        return str(Path(path).resolve().relative_to(folder))
    except (TypeError, ValueError):
        return None


def read(link: str, language: str | None) -> dict[str, Any]:
    global last_instagram
    with lock:
        instagram = "instagram" in (urlsplit(link).hostname or "") or "instagr.am" in link
        if instagram:
            time.sleep(max(0.0, last_instagram + MIN_GAP - time.monotonic()))
        command = [sys.executable, str(POST), "read", *(["--language", language] if language else [])]
        try:  # the link goes in on stdin, never in a shell
            done = subprocess.run(command, input=link, capture_output=True, text=True, timeout=READ_LIMIT,
                                  env={**os.environ, "TMPDIR": str(WORK), "GK_REMOTE_URL": "", "GK_CONFIG": "/nonexistent"})
            result = json.loads(done.stdout)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return {"status": "error", "reason": "the read on the NAS failed or timed out", "link": link, "work": None}
        finally:
            if instagram:
                last_instagram = time.monotonic()
    work = Path(result["work"]).resolve() if result.get("work") else None
    if work is None or work.parent != WORK.resolve() or not READ_ID.fullmatch(work.name):
        return {**result, "work": None}
    if result.get("status") != "ok":  # nothing to fetch: the computer makes its own empty folder
        shutil.rmtree(work, ignore_errors=True)
        return {**result, "work": str(work.name), "read_id": ""}
    result["keyframes"] = [r for r in (relative(f, work) for f in result.get("keyframes", [])) if r]
    result["thumbnail"] = relative(result.get("thumbnail"), work)
    return {**result, "work": work.name, "read_id": work.name}


def sweep() -> None:
    """Delete reads older than TTL: a computer that never called clean must not fill the disk."""
    while True:
        time.sleep(min(300.0, max(1.0, TTL / 4)))
        for folder in WORK.glob("graveyard-killer-*"):
            try:
                if (folder / MARKER).is_file() and time.time() - folder.stat().st_mtime > TTL:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - no line may hold a link or a header
        pass

    def send(self, code: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json(self, code: int, value: Any) -> None:
        self.send(code, json.dumps(value).encode())

    def authorized(self) -> bool:
        given = self.headers.get("Authorization", "")
        return bool(TOKEN) and hmac.compare_digest(given.encode(), f"Bearer {TOKEN}".encode())

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            return self.json(200, {"ok": True})
        if not self.authorized():
            return self.json(401, {"error": "unauthorized"})
        parts = [unquote(p) for p in path.split("/")[1:]]
        if len(parts) < 3 or parts[0] != "files":
            return self.json(404, {"error": "not found"})
        folder = folder_of(parts[1])
        target = (folder / "/".join(parts[2:])).resolve() if folder else None
        if not (folder and target and folder.resolve() in target.parents and target.is_file()
                and target.name != MARKER):
            return self.json(404, {"error": "not found"})
        kind = "image/jpeg" if target.suffix.lower() in (".jpg", ".jpeg") else "application/octet-stream"
        self.send(200, target.read_bytes(), kind)

    def do_POST(self) -> None:  # noqa: N802
        if not self.authorized():
            return self.json(401, {"error": "unauthorized"})
        path = urlsplit(self.path).path
        if path.startswith("/clean/"):
            folder = folder_of(unquote(path[len("/clean/"):]))
            if folder:
                shutil.rmtree(folder, ignore_errors=True)
            return self.json(200, {"cleaned": bool(folder)})
        if path != "/read":
            return self.json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if 0 < length <= BODY_LIMIT else None
            link, language = body["link"], body.get("language")
        except (ValueError, TypeError, KeyError, AttributeError):
            return self.json(400, {"error": "expected {\"link\": ..., \"language\": ...}"})
        if not isinstance(link, str) or not allowed(link):
            return self.json(200, {"status": "error", "reason": "not an Instagram, TikTok or YouTube link",
                                   "link": link if isinstance(link, str) else "", "work": None})
        if language is not None and not (isinstance(language, str) and LANGUAGE.fullmatch(language)):
            language = None
        result = read(link.strip(), language)
        if "is not installed" in str(result.get("reason", "")):  # a broken NAS, not a post's answer:
            return self.json(503, {"error": "a tool is missing on the NAS"})  # the computer reads it itself
        self.json(200, result)


def main() -> None:
    if not TOKEN:
        sys.exit("GK_TOKEN is not set")
    WORK.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=sweep, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("GK_PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()

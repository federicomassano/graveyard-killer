"""Optional NAS helpers for post.py: Scriberr for speech, and a remote gk-fetcher for whole reads.

Both are off unless configured. Settings come from the environment, or from a JSON file for
programs that start without one (Claude Desktop): GK_CONFIG, else ~/.graveyard-killer/nas.json.
The environment wins. Names:

    GK_SCRIBERR_URL, GK_SCRIBERR_KEY, GK_SCRIBERR_PROFILE   transcribe with a Scriberr server
    GK_REMOTE_URL, GK_REMOTE_TOKEN                          read posts through a gk-fetcher server
    GK_REMOTE_CF_ID, GK_REMOTE_CF_SECRET                    Cloudflare Access service token, optional

Secrets are only ever sent in request headers: never printed, logged or put in the JSON result.
Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_CONFIG = Path.home() / ".graveyard-killer" / "nas.json"
REMOTE_TIMEOUT = 3600  # seconds a remote read may take: one at a time, and a long video
FILE_TIMEOUT = 120
DONE = {"completed", "complete", "done", "success", "succeeded", "finished"}
FAILED = {"failed", "error", "cancelled", "canceled", "expired", "timeout", "timed_out"}


class NasUnavailable(Exception):
    """The NAS did not answer usefully. The caller carries on without it."""


def setting(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    try:
        data = json.loads(Path(os.environ.get("GK_CONFIG") or DEFAULT_CONFIG).read_text(encoding="utf-8"))
        value = data.get(name) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) and value else None


def base_url(name: str) -> str | None:
    value = setting(name)
    return value.rstrip("/") if value and value.lower().startswith(("http://", "https://")) else None


def scriberr_configured() -> bool:
    return bool(base_url("GK_SCRIBERR_URL") and setting("GK_SCRIBERR_KEY"))


def remote_configured() -> bool:
    return bool(base_url("GK_REMOTE_URL") and setting("GK_REMOTE_TOKEN"))


def multipart(fields: dict[str, str], name: str, filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
             for key, value in fields.items()]
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
              f"Content-Type: {content_type}\r\n\r\n".encode(), data, f"\r\n--{boundary}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def call(url: str, headers: dict[str, str], data: bytes | None = None, method: str = "GET",
         timeout: float = 60) -> bytes:
    """One HTTP request. Any failure, a status of 400 or more included, becomes NasUnavailable."""
    try:
        # Cloudflare refuses urllib's default User-Agent (403, error 1010), so name the client.
        headers = {"User-Agent": "graveyard-killer-nas/1.0", **headers}
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=timeout) as response:
            body: bytes = response.read()
            return body
    except HTTPError as error:
        raise NasUnavailable(f"HTTP {error.code} from {method} {url.split('?')[0]}") from None
    except (OSError, ValueError) as error:
        raise NasUnavailable(f"{type(error).__name__} from {method} {url.split('?')[0]}") from None


# ---- Scriberr --------------------------------------------------------------------------------

def transcript_of(value: Any, language: str | None = None) -> tuple[str, str | None]:
    """The text, and the language when it says one, from Scriberr's transcript: plain text, or
    WhisperX JSON (a "text", or "segments" with a "text" each), alone or inside "transcript"."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                return transcript_of(json.loads(stripped), language)
            except ValueError:
                pass
        return stripped, language
    if isinstance(value, dict):
        language = value.get("language") if isinstance(value.get("language"), str) else language
        if isinstance(value.get("text"), str):
            return value["text"].strip(), language
        for key in ("transcript", "result", "results"):
            if isinstance(value.get(key), (str, dict)):
                return transcript_of(value[key], language)
        segments = value.get("segments")
        if isinstance(segments, list):
            texts = [s["text"].strip() for s in segments if isinstance(s, dict) and isinstance(s.get("text"), str)]
            return " ".join(t for t in texts if t), language
    raise NasUnavailable("Scriberr gave a transcript in a shape post.py does not know")


def scriberr_transcribe(audio: Path, limit: float) -> tuple[str, str | None]:
    """Upload audio to Scriberr's quick transcription and wait for the text and its language.

    Raises NasUnavailable on a network error, a refusal, a failed job, an answer in an unknown
    shape, or when limit seconds pass.
    """
    url, key, profile = base_url("GK_SCRIBERR_URL"), setting("GK_SCRIBERR_KEY"), setting("GK_SCRIBERR_PROFILE")
    if not (url and key):
        raise NasUnavailable("Scriberr is not configured")
    deadline = time.monotonic() + limit
    body, content_type = multipart({"profile_name": profile} if profile else {}, "audio", audio.name,
                                   audio.read_bytes(), "audio/mpeg")
    headers = {"X-API-Key": key}
    started = json.loads(call(f"{url}/api/v1/transcription/quick", {**headers, "Content-Type": content_type},
                              body, "POST", timeout=min(limit, 300)) or b"{}")
    job = started.get("id") or started.get("job_id") if isinstance(started, dict) else None
    if not job:
        raise NasUnavailable("Scriberr gave no job id")
    wait = 0.5
    while True:
        answer = json.loads(call(f"{url}/api/v1/transcription/quick/{quote(str(job), safe='')}", headers,
                                 timeout=60) or b"{}")
        status = str(answer.get("status", "")).lower() if isinstance(answer, dict) else ""
        if status in FAILED:
            raise NasUnavailable(f"Scriberr job {status}")
        if status in DONE:
            text, language = transcript_of(answer.get("transcript"), answer.get("language"))
            return text, language
        if time.monotonic() + wait > deadline:
            raise NasUnavailable("Scriberr took too long")
        time.sleep(wait)
        wait = min(wait * 1.5, 15)


# ---- gk-fetcher ------------------------------------------------------------------------------

def remote_headers() -> dict[str, str]:
    headers = {"Authorization": f"Bearer {setting('GK_REMOTE_TOKEN')}"}
    if setting("GK_REMOTE_CF_ID") and setting("GK_REMOTE_CF_SECRET"):
        headers["CF-Access-Client-Id"] = str(setting("GK_REMOTE_CF_ID"))
        headers["CF-Access-Client-Secret"] = str(setting("GK_REMOTE_CF_SECRET"))
    return headers


REMOTE_ID_FILE = ".gk-remote-id"


def remote_read(link: str, language: str | None, make_folder: Callable[[], Path]) -> dict[str, Any]:
    """Read a post on the NAS and bring its frames here, into a fresh marked work folder.

    The result has post.py read's own shape. A post the NAS could not read (hidden, gone) is an
    answer and comes back as such. Raises NasUnavailable when the NAS itself failed.
    """
    url = base_url("GK_REMOTE_URL")
    if not url:
        raise NasUnavailable("no NAS configured")
    headers = remote_headers()
    payload = json.dumps({"link": link, "language": language}).encode()
    result = json.loads(call(f"{url}/read", {**headers, "Content-Type": "application/json"}, payload, "POST",
                             timeout=REMOTE_TIMEOUT))
    if not isinstance(result, dict) or "status" not in result:
        raise NasUnavailable("the NAS gave an answer that is not a read result")
    read_id = result.pop("read_id", None)
    if result.get("work") is None:  # refused before any tool ran: no folder, as locally
        return result
    folder = make_folder()
    try:
        if read_id:
            (folder / REMOTE_ID_FILE).write_text(str(read_id))
            relative = [*result.get("keyframes", []), result.get("thumbnail")]
            for name in dict.fromkeys(n for n in relative if isinstance(n, str)):
                target = (folder / name).resolve()
                if folder not in target.parents:
                    raise NasUnavailable("the NAS named a file outside the work folder")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(call(f"{url}/files/{quote(str(read_id), safe='')}/{quote(name)}", headers,
                                        timeout=FILE_TIMEOUT))
            result["keyframes"] = [str(folder / n) for n in result.get("keyframes", [])]
            if isinstance(result.get("thumbnail"), str):
                result["thumbnail"] = str(folder / result["thumbnail"])
    except Exception:
        remote_clean(folder)
        shutil.rmtree(folder, ignore_errors=True)
        raise
    result["work"] = str(folder)
    return result


def remote_clean(folder: Path) -> None:
    """Ask the NAS to drop its copy of a read, when this folder came from one. Best effort."""
    marker, url = folder / REMOTE_ID_FILE, base_url("GK_REMOTE_URL")
    if not (marker.is_file() and url and remote_configured()):
        return
    try:
        call(f"{url}/clean/{quote(marker.read_text().strip(), safe='')}", remote_headers(), b"", "POST", timeout=30)
    except NasUnavailable:
        pass  # the NAS drops it by itself after an hour

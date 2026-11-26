"""Scriberr as the speech engine, and a gk-fetcher on the NAS as the reader, both with fakes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest
from conftest import FAKE_NAMES, SCRIPT, Run, install_fakes
from test_post import REEL, reel, speech_calls

SERVER = SCRIPT.parents[4] / "nas" / "server.py"
KEY = "scriberr-secret-key-123"
TOKEN = "fetcher-secret-token-456"


class Fake:
    """A local HTTP server that plays one canned behaviour and remembers what it was asked."""

    def __init__(self, handler: Callable[[BaseHTTPRequestHandler, "Fake"], None]) -> None:
        fake = self
        self.requests: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def handle_any(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                      "body": self.rfile.read(length)})
                handler(self, fake)

            do_GET = do_POST = handle_any  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def reply(h: BaseHTTPRequestHandler, code: int, body: Any, kind: str = "application/json") -> None:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    h.send_response(code)
    h.send_header("Content-Type", kind)
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def scriberr(transcript: Any = "Cinque cose da rifare.", *, status: str = "completed", code: int = 200,
             language: str | None = None) -> Callable[[BaseHTTPRequestHandler, Fake], None]:
    def handler(h: BaseHTTPRequestHandler, fake: Fake) -> None:
        if h.headers.get("X-API-Key") != KEY:
            return reply(h, 401, {"error": "bad key"})
        if code != 200:
            return reply(h, code, {"error": "boom"})
        if h.command == "POST":
            return reply(h, 202, {"id": "job1", "status": "pending"})
        answer: dict[str, Any] = {"id": "job1", "status": status}
        if status == "completed":
            answer["transcript"] = transcript
        if language:
            answer["language"] = language
        reply(h, 200, answer)
    return handler


@pytest.fixture
def serve() -> Iterator[Callable[[Callable[[BaseHTTPRequestHandler, Fake], None]], Fake]]:
    started: list[Fake] = []

    def start(handler: Callable[[BaseHTTPRequestHandler, Fake], None]) -> Fake:
        started.append(Fake(handler))
        return started[-1]

    yield start
    for fake in started:
        fake.close()


def with_scriberr(fake: Fake, **more: str) -> dict[str, str]:
    return {"GK_SCRIBERR_URL": fake.url, "GK_SCRIBERR_KEY": KEY, "GK_SCRIBERR_PROFILE": "gk", **more}


def test_scriberr_gives_the_transcript_and_no_local_engine_runs(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    fake = serve(scriberr("Cinque cose da rifare.", language="it"))
    run = post("read", REEL, scenario=reel(media), env=with_scriberr(fake))

    assert run.output["transcript"] == "Cinque cose da rifare."
    assert run.output["transcript_source"] == "scriberr"
    assert run.output["transcript_language"] == "it"
    assert speech_calls(run) == []
    upload = fake.requests[0]
    assert upload["path"] == "/api/v1/transcription/quick" and b'name="audio"' in upload["body"]
    assert b"gk" in upload["body"]
    ffmpeg_audio = next(r for r in fake.requests if r["method"] == "POST")["body"]
    assert len(ffmpeg_audio) < 200_000  # an mp3, not the video


@pytest.mark.parametrize("transcript", [
    {"text": "Dal JSON di WhisperX.", "language": "it"},
    json.dumps({"segments": [{"text": " Dal JSON "}, {"text": "di WhisperX."}], "language": "it"}),
    {"transcript": {"segments": [{"text": "Dal JSON"}, {"text": "di WhisperX."}]}},
])
def test_scriberr_json_transcripts_give_their_text(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake], transcript: Any
) -> None:
    run = post("read", REEL, scenario=reel(media), env=with_scriberr(serve(scriberr(transcript))))

    assert run.output["transcript"].replace("  ", " ") == "Dal JSON di WhisperX."
    assert run.output["transcript_source"] == "scriberr"


def test_scriberr_polls_until_the_job_is_done(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    def slow(h: BaseHTTPRequestHandler, fake: Fake) -> None:
        if h.command == "GET":
            fake.state["polls"] = fake.state.get("polls", 0) + 1
            if fake.state["polls"] < 3:
                return reply(h, 200, {"id": "job1", "status": "processing"})
        scriberr("Alla terza.")(h, fake)

    fake = serve(slow)
    run = post("read", REEL, scenario=reel(media), env=with_scriberr(fake))

    assert run.output["transcript"] == "Alla terza."
    assert fake.state["polls"] == 3


@pytest.mark.parametrize("behaviour", [
    scriberr(code=500), scriberr(transcript=12345), scriberr(status="failed"),
    lambda h, fake: reply(h, 200, b"not json", "text/plain"),
])
def test_when_scriberr_fails_the_local_engine_transcribes(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake],
    behaviour: Callable[..., None],
) -> None:
    run = post("read", REEL, scenario=reel(media), env=with_scriberr(serve(behaviour)))

    assert run.output["status"] == "ok"
    assert run.output["transcript_source"] in ("whisper", "parakeet")
    assert speech_calls(run)


def test_a_wrong_scriberr_key_falls_back_and_never_shows_a_key(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake], capfd: pytest.CaptureFixture[str]
) -> None:
    env = with_scriberr(serve(scriberr()), GK_SCRIBERR_KEY="the-wrong-key-789")
    run = post("read", REEL, scenario=reel(media), env=env)

    assert run.output["transcript_source"] in ("whisper", "parakeet")
    assert "the-wrong-key-789" not in json.dumps(run.output)


def test_a_scriberr_that_never_finishes_falls_back_at_the_time_limit(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    fake = serve(scriberr(status="processing"))
    begun = time.monotonic()
    run = post("read", REEL, scenario=reel(media), env=with_scriberr(fake, GK_TIME_LIMIT="4"))

    assert run.output["transcript_source"] in ("whisper", "parakeet")
    assert time.monotonic() - begun < 30


def test_a_scriberr_that_is_off_falls_back(post: Callable[..., Run], media: dict[str, Path]) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    run = post("read", REEL, scenario=reel(media),
               env={"GK_SCRIBERR_URL": f"http://127.0.0.1:{port}", "GK_SCRIBERR_KEY": KEY})

    assert run.output["transcript_source"] in ("whisper", "parakeet")


def test_settings_come_from_the_config_file_too(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake], tmp_path: Path
) -> None:
    config = tmp_path / "nas.json"
    config.write_text(json.dumps({"GK_SCRIBERR_URL": serve(scriberr("Dal file.")).url, "GK_SCRIBERR_KEY": KEY}))
    run = post("read", REEL, scenario=reel(media), env={"GK_CONFIG": str(config)})

    assert run.output["transcript"] == "Dal file."


def test_check_asks_for_no_speech_engine_when_scriberr_is_set(
    post: Callable[..., Run], serve: Callable[..., Fake]
) -> None:
    run = post("check", without=("mlx_whisper", "whisper-ctranslate2", "parakeet-mlx"),
               env=with_scriberr(serve(scriberr())))

    assert not any("whisper" in m["tool"] or "parakeet" in m["tool"] for m in run.output["missing"])
    assert run.output["speech_model"] == "remote"


# ---- the remote reader ------------------------------------------------------------------------

def fetcher(media: dict[str, Path], status: str = "ok") -> Callable[[BaseHTTPRequestHandler, Fake], None]:
    def handler(h: BaseHTTPRequestHandler, fake: Fake) -> None:
        if h.headers.get("Authorization") != f"Bearer {TOKEN}":
            return reply(h, 401, {"error": "unauthorized"})
        if h.path == "/read":
            if status == "error":
                return reply(h, 200, {"status": "error", "reason": "hidden", "link": REEL, "work": "gone",
                                      "read_id": ""})
            return reply(h, 200, {
                "status": "ok", "platform": "instagram", "link": REEL, "id": "Rk3elTest01", "kind": "video",
                "author": "a", "title": "t", "caption": "c", "duration": 6, "transcript": "dal NAS",
                "transcript_source": "scriberr", "transcript_language": "it", "transcript_blocks": ["dal NAS"],
                "keyframes": ["frames/01.jpg", "frames/02.jpg"], "thumbnail": "thumbnail.jpg",
                "work": "graveyard-killer-abc", "read_id": "graveyard-killer-abc"})
        if h.path.startswith("/files/graveyard-killer-abc/"):
            return reply(h, 200, media["thumb"].read_bytes(), "image/jpeg")
        if h.path == "/clean/graveyard-killer-abc":
            fake.state["cleaned"] = True
            return reply(h, 200, {"cleaned": True})
        reply(h, 404, {})
    return handler


def with_remote(fake: Fake, **more: str) -> dict[str, str]:
    return {"GK_REMOTE_URL": fake.url, "GK_REMOTE_TOKEN": TOKEN, **more}


def test_a_remote_read_has_the_local_shape_and_its_frames_are_here(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    fake = serve(fetcher(media))
    remote = post("read", REEL, env=with_remote(fake, GK_REMOTE_CF_ID="id.access", GK_REMOTE_CF_SECRET="shh"))
    local = post("read", REEL, scenario=reel(media))

    assert remote.calls == []  # no yt-dlp, no ffmpeg, no speech engine on this computer
    assert remote.output.keys() == local.output.keys()
    assert {k: type(v) for k, v in remote.output.items()} == {k: type(v) for k, v in local.output.items()}
    work = Path(remote.output["work"])
    assert (work / ".graveyard-killer-work").is_file()
    assert all(Path(f).parent == work / "frames" and Path(f).is_file() for f in remote.output["keyframes"])
    assert Path(remote.output["thumbnail"]).parent == work and Path(remote.output["thumbnail"]).is_file()
    assert {k.lower(): v for k, v in fake.requests[0]["headers"].items()}["cf-access-client-id"] == "id.access"
    assert TOKEN not in json.dumps(remote.output)
    assert not fake.requests[0]["headers"]["User-Agent"].startswith("Python-urllib")


def test_upload_accepts_the_frames_that_came_from_the_nas(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    remote = post("read", REEL, env=with_remote(serve(fetcher(media))))
    seen: list[bytes] = []

    def notion(h: BaseHTTPRequestHandler, fake: Fake) -> None:
        seen.append(fake.requests[-1]["body"])
        reply(h, 200, {"status": "uploaded", "id": "up1"})

    # post.py only sends to api.notion.com: ask for the refusal that is not about the folder
    sent = post("upload", remote.output["thumbnail"], "https://example.invalid/x")
    assert "not a Notion upload URL" in sent.output["error"]
    assert "not an image that read made" not in sent.output["error"]
    assert seen == []


def test_clean_removes_the_folder_and_the_nas_copy(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    fake = serve(fetcher(media))
    env = with_remote(fake)
    work = post("read", REEL, env=env).output["work"]
    cleaned = post("clean", work, env=env)

    assert cleaned.output == {"cleaned": work}
    assert not Path(work).exists()
    assert fake.state["cleaned"] is True


def test_a_post_the_nas_could_not_read_is_its_answer_not_a_reason_to_read_here(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    run = post("read", REEL, env=with_remote(serve(fetcher(media, "error"))))

    assert run.output["status"] == "error" and run.output["reason"] == "hidden"
    assert run.calls == [] and "remote" not in run.output
    assert (Path(run.output["work"]) / ".graveyard-killer-work").is_file() or run.output["work"] == "gone"


@pytest.mark.parametrize("how", ["off", "401", "500"])
def test_when_the_nas_does_not_answer_the_read_happens_here(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake], how: str
) -> None:
    if how == "off":
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{s.getsockname()[1]}"
        env = {"GK_REMOTE_URL": url, "GK_REMOTE_TOKEN": TOKEN}
    else:
        code = int(how)
        env = with_remote(serve(lambda h, fake: reply(h, code, {"error": "no"})))
    run = post("read", REEL, scenario=reel(media), env=env)

    assert run.output["status"] == "ok"
    assert run.output["remote"] == "unreachable, read locally"
    assert run.calls and TOKEN not in json.dumps(run.output)


def test_a_login_read_never_goes_to_the_nas(
    post: Callable[..., Run], media: dict[str, Path], serve: Callable[..., Fake]
) -> None:
    fake = serve(fetcher(media))
    post("read", REEL, "--login", "firefox", scenario=reel(media), env=with_remote(fake))

    assert fake.requests == []


# ---- the real server, with the fake tools ------------------------------------------------------

@pytest.fixture
def gk_fetcher(tmp_path: Path, media: dict[str, Path], fake_launcher: Path | None,
               serve: Callable[..., Fake]) -> Iterator[tuple[str, Path]]:
    if os.name == "nt":
        pytest.skip("the NAS runs Linux")
    bin_dir = tmp_path / "srvbin"
    bin_dir.mkdir()
    install_fakes(bin_dir, fake_launcher)
    for real in ("ffmpeg", "ffprobe"):
        (bin_dir / real).symlink_to(__import__("shutil").which(real) or "/nonexistent")
    scenario = tmp_path / "srv-scenario.json"
    scenario.write_text(json.dumps(reel(media)))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    work = tmp_path / "srvwork"
    env = {
        "PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent)]),
        "UV_TOOL_DIR": str(tmp_path / "uvt"), "GK_FAKE_SCENARIO": str(scenario), "GK_FAKE_LOG": str(tmp_path / "srv.log"),
        "GK_TOKEN": TOKEN, "GK_WORK": str(work), "GK_PORT": str(port), "GK_POST": str(SCRIPT), "GK_MIN_GAP": "0",
        "GK_SCRIBERR_URL": serve(scriberr("Dalla pipeline.")).url, "GK_SCRIBERR_KEY": KEY, "GK_TTL": "3600",
    }
    (tmp_path / "srv.log").write_text("")
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urlopen(f"{base}/health", timeout=1).read()
            break
        except OSError:
            time.sleep(0.1)
    yield base, work
    proc.terminate()
    proc.wait(10)


def api(base: str, path: str, body: Any = None, token: str | None = TOKEN) -> tuple[int, bytes]:
    from urllib.error import HTTPError
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers,
                      method="POST" if body is not None else "GET")
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def test_the_real_server_end_to_end_through_the_client(
    post: Callable[..., Run], gk_fetcher: tuple[str, Path]
) -> None:
    base, work = gk_fetcher
    run = post("read", REEL, env={"GK_REMOTE_URL": base, "GK_REMOTE_TOKEN": TOKEN})

    assert run.output["status"] == "ok" and run.output["transcript_source"] == "scriberr"
    assert run.output["transcript"] == "Dalla pipeline."
    assert run.calls == []
    assert all(Path(f).is_file() for f in run.output["keyframes"]) and Path(run.output["thumbnail"]).is_file()
    assert len(list(work.glob("graveyard-killer-*"))) == 1
    post("clean", run.output["work"], env={"GK_REMOTE_URL": base, "GK_REMOTE_TOKEN": TOKEN})
    assert list(work.glob("graveyard-killer-*")) == []


def test_the_server_needs_the_token_and_keeps_to_its_allowlist(gk_fetcher: tuple[str, Path]) -> None:
    base, _ = gk_fetcher
    assert api(base, "/read", {"link": REEL}, token=None)[0] == 401
    assert api(base, "/read", {"link": REEL}, token="wrong")[0] == 401
    assert api(base, "/files/x/y", token=None)[0] == 401
    code, body = api(base, "/read", {"link": "https://evil.example/p/abc"})
    assert code == 200 and json.loads(body)["status"] == "error"
    code, body = api(base, "/read", {"link": "file:///etc/passwd"})
    assert json.loads(body)["status"] == "error"
    assert api(base, "/read", {"nolink": 1})[0] == 400


def test_the_server_serves_only_files_of_a_read(gk_fetcher: tuple[str, Path], tmp_path: Path) -> None:
    base, work = gk_fetcher
    result = json.loads(api(base, "/read", {"link": REEL})[1])
    read_id = result["read_id"]
    assert result["work"] == read_id and result["keyframes"] and not result["thumbnail"].startswith("/")

    assert api(base, f"/files/{read_id}/{result['thumbnail']}")[0] == 200
    assert api(base, f"/files/{read_id}/.graveyard-killer-work")[0] == 404
    assert api(base, f"/files/{read_id}/..%2f..%2fsrv-scenario.json")[0] == 404
    assert api(base, f"/files/{read_id}/%2e%2e/%2e%2e/srv-scenario.json")[0] == 404
    assert api(base, "/files/..%2f/x")[0] == 404
    assert api(base, f"/files/{read_id}/..")[0] == 404
    assert api(base, f"/clean/{read_id}", {})[0] == 200
    assert api(base, f"/files/{read_id}/{result['thumbnail']}")[0] == 404


def test_a_tool_missing_on_the_nas_is_a_503_so_the_computer_reads_it(
    gk_fetcher: tuple[str, Path], tmp_path: Path
) -> None:
    base, _ = gk_fetcher
    # the server's fakes were built in srvbin: take the speech fakes away and Scriberr stays unreachable
    assert api(base, "/read", {"link": REEL})[0] == 200  # sanity: the healthy path still answers 200

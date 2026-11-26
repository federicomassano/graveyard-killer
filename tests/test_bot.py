"""The Telegram bot, with a fake Telegram."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from conftest import SCRIPT
from test_nas import Fake, reply

BOT = SCRIPT.parents[4] / "nas" / "bot.py"
TOKEN = "123456:bot-secret-token"
ME = 4242
REEL = "https://www.instagram.com/reel/Cabc123XYZ/?igsh=tracking123"


@pytest.fixture
def telegram(tmp_path: Path) -> Iterator[Callable[..., tuple[Fake, Path, subprocess.CompletedProcess[str]]]]:
    fakes: list[Fake] = []

    def run(messages: list[tuple[int, str]], allowed: str = str(ME)) -> tuple[Fake, Path, subprocess.CompletedProcess[str]]:
        sent: list[dict[str, Any]] = []

        def handler(h: BaseHTTPRequestHandler, fake: Fake) -> None:
            body = parse_qs(fake.requests[-1]["body"].decode())
            if h.path.endswith("/getUpdates"):
                after = int(body.get("offset", ["0"])[0])
                fresh = [{"update_id": 1 + i, "message": {"from": {"id": u}, "chat": {"id": u}, "text": t}}
                         for i, (u, t) in enumerate(messages) if 1 + i >= after]
                return reply(h, 200, {"ok": True, "result": fresh})
            sent.append({k: v[0] for k, v in body.items()})
            reply(h, 200, {"ok": True, "result": {}})

        fake = Fake(handler)
        fake.state["sent"] = sent
        fakes.append(fake)
        vault = tmp_path / "Library"
        vault.mkdir(exist_ok=True)
        env = {**os.environ, "TG_TOKEN": TOKEN, "TG_API": fake.url, "TG_ALLOWED": allowed, "VAULT": str(vault),
               "STATE": str(tmp_path / f"state{len(fakes)}"), "GK_BOT_ONCE": "1", "PYTHONPATH": str(SCRIPT.parent)}
        done = subprocess.run([sys.executable, str(BOT)], env=env, capture_output=True, text=True, timeout=60)
        return fake, vault, done

    yield run
    for fake in fakes:
        fake.close()


def test_a_shared_reel_is_filed_with_a_clean_link_and_confirmed(telegram: Any) -> None:
    fake, vault, done = telegram([(ME, f"Guarda questo {REEL}")])

    note = (vault / "Condividi qui.md").read_text()
    assert "https://www.instagram.com/p/Cabc123XYZ/" in note and "igsh" not in note
    assert fake.state["sent"][0]["text"].startswith("Salvato") and fake.state["sent"][0]["chat_id"] == str(ME)
    assert TOKEN not in done.stdout + done.stderr


def test_the_same_post_twice_is_one_save(telegram: Any, tmp_path: Path) -> None:
    telegram([(ME, REEL)])
    fake, vault, _ = telegram([(ME, REEL.replace("/reel/", "/p/"))])  # a new run, same vault, other URL form

    assert (vault / "Condividi qui.md").read_text().count("Cabc123XYZ") == 1
    assert fake.state["sent"][0]["text"] == "Già in Libreria."


def test_someone_else_saves_nothing_and_is_told_it_is_private(telegram: Any) -> None:
    fake, vault, _ = telegram([(999, REEL)])

    assert not (vault / "Condividi qui.md").exists()
    assert fake.state["sent"][0]["text"] == "Questo bot è privato."


def test_id_works_for_anyone_so_the_owner_can_be_set_up(telegram: Any) -> None:
    fake, _, _ = telegram([(999, "/id")], allowed="")

    assert "999" in fake.state["sent"][0]["text"]


def test_text_without_a_link_gets_the_help(telegram: Any) -> None:
    fake, vault, _ = telegram([(ME, "ciao")])

    assert "Condividi" in fake.state["sent"][0]["text"] and not (vault / "Condividi qui.md").exists()

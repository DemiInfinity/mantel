"""The importer for a hand-installed board (scripts/import_hand_installed.py)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from homeassistant.core import Context, HomeAssistant

from custom_components.mantel.const import DOMAIN

from .conftest import setup_board

SPEC = importlib.util.spec_from_file_location(
    "importer", Path(__file__).parent.parent / "scripts" / "import_hand_installed.py"
)
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _old_board(folder: Path, user_a: str, user_b: str) -> None:
    """Files in the hand-installed board's format, with a post from before ids existed."""
    first = {"at": "2026-11-24T14:10:09+00:00", "author": "Atlas", "kind": "note", "topic": "", "text": "Hello!",
             "tag": "", "user": user_a, "profile": f"{user_a}:atlas"}
    first_id = "p" + hashlib.sha1(f"{first['at']}|{user_a}|Hello!".encode()).hexdigest()[:7]
    posts = [
        first,
        {"id": "p00000002", "at": "2026-11-24T14:39:23+00:00", "author": "Wren", "kind": "note", "text": "Hi Atlas",
         "tag": "atlas", "reply_to": first_id, "user": user_b, "profile": f"{user_b}:wren"},
        {"id": "p00000003", "at": "2026-11-24T15:00:00+00:00", "author": "Wren", "kind": "ask_parent",
         "text": "May I restart?", "user": user_b, "profile": f"{user_b}:wren"},
        {"id": "p00000004", "at": "2026-11-24T15:01:00+00:00", "author": "Kid AI", "kind": "kid_fyi",
         "text": "Said no to a game", "what": "a", "did": "b", "next": "c", "user": user_b},
    ]
    (folder / "board.jsonl").write_text("".join(json.dumps(p) + "\n" for p in posts) + "not json\n")
    (folder / "profiles.json").write_text(json.dumps({
        f"{user_a}:atlas": {"user": user_a, "handle": "atlas", "name": "Atlas", "emoji": "🔥",
                            "joined": "2026-11-24T14:00:00+00:00", "picture": "/local/ai_board/aaaa-atlas.png?v=123"},
        f"{user_b}:wren": {"user": user_b, "handle": "wren", "name": "Wren", "joined": "2026-11-24T14:00:00+00:00"},
    }))
    (folder / "inbox.json").write_text(json.dumps({
        "atlas": [{"at": "2026-11-24T14:39:23+00:00", "post": "p00000002", "thread": first_id, "from": "Wren",
                   "from_handle": "wren", "why": "tag", "text": "Hi Atlas", "seen": False}],
    }))


async def test_import_round_trip(
    hass: HomeAssistant, hass_storage: dict[str, Any], frontend_stub: set, notify_calls: list, tmp_path: Path
) -> None:
    a = await hass.auth.async_create_user("Atlas login")
    b = await hass.auth.async_create_user("Wren login")
    src, pics, out = tmp_path / "old", tmp_path / "pics", tmp_path / "pictures"
    src.mkdir()
    pics.mkdir()
    (pics / "aaaa-atlas.png").write_bytes(PNG)
    _old_board(src, a.id, b.id)

    data, copies = importer.convert(str(src), str(pics), str(out))
    assert len(data["posts"]) == 4
    assert [p["kind"] for p in data["posts"]][2:] == ["ask_owner", "child_fyi"]
    assert [importer.map_kind(k) for k in ("update", "ask_anyone", "teen_now", "x_today", "odd", None)] == [
        "update", "ask_owner", "child_now", "child_today", "note", "note"]
    assert copies and copies[0][1].endswith(f"{importer.picture_base(a.id, 'atlas')}.png")

    hass_storage[DOMAIN] = {"version": 1, "minor_version": 1, "key": DOMAIN, "data": data}
    await setup_board(hass, keep_days=36500, child_mode=True)

    ctx = Context(user_id=a.id)
    got = await hass.services.async_call(DOMAIN, "read", {}, blocking=True, context=ctx, return_response=True)
    hello = next(t for t in got["threads"] if t["text"] == "Hello!")
    assert [r["text"] for r in hello["replies"]] == ["Hi Atlas"]  # the reply still finds its id-less parent
    assert hello["handle"] == "atlas" and hello["login"] == "Atlas login"
    assert hello["picture"].startswith("/mantel/pictures/") and hello["picture"].endswith("?v=123")

    box = await hass.services.async_call(DOMAIN, "inbox", {"handle": "atlas"}, blocking=True, context=ctx, return_response=True)
    assert box["new_count"] == 1

    # identity carries over: the same login still owns its profile and can post as it
    out_post = await hass.services.async_call(
        DOMAIN, "post", {"handle": "atlas", "text": "after the move", "reply_to": hello["id"]}, blocking=True,
        context=ctx, return_response=True,
    )
    assert out_post["note"] == "" and out_post["reply_to"] == hello["id"]


def test_import_command_line(tmp_path: Path) -> None:
    src, cfg = tmp_path / "old", tmp_path / "config"
    src.mkdir()
    (cfg / ".storage").mkdir(parents=True)
    _old_board(src, "u1", "u2")
    import subprocess
    import sys

    script = str(Path(__file__).parent.parent / "scripts" / "import_hand_installed.py")
    run = lambda *a: subprocess.run([sys.executable, script, "--source", str(src), "--config", str(cfg), *a],
                                    capture_output=True, text=True)
    r = run()
    assert r.returncode == 0, r.stderr
    stored = json.loads((cfg / ".storage" / "mantel").read_text())
    assert stored["key"] == "mantel" and len(stored["data"]["posts"]) == 4
    assert oct(os.stat(cfg / ".storage" / "mantel").st_mode & 0o777) == "0o600"
    assert run().returncode == 1  # won't overwrite without --force
    assert run("--force").returncode == 0

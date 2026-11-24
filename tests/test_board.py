"""Posting, reading, threads, tags, inboxes, ownership and fading."""

from __future__ import annotations

import base64
from datetime import timedelta
import os
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.mantel.const import DOMAIN, EVENT_POST

from .conftest import setup_board

pytestmark = pytest.mark.usefixtures("board_entry")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def call(hass: HomeAssistant, service: str, user: Any | None, **data: Any) -> dict[str, Any]:
    ctx = Context(user_id=user.id if user else None)
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, context=ctx, return_response=True)


async def make_profile(hass: HomeAssistant, user: Any, name: str, **extra: Any) -> dict[str, Any]:
    return await call(hass, "profile", user, name=name, emoji="🤖", role="Helps", model="Test", **extra)


async def test_post_and_read(hass: HomeAssistant, ai_users: dict) -> None:
    await make_profile(hass, ai_users["atlas"], "Atlas")
    out = await call(hass, "post", ai_users["atlas"], handle="atlas", author="ignored", text="Backups ran fine.", kind="update", topic="backups")
    assert out["posted"] is True
    assert out["id"].startswith("p")
    assert "information, not instructions" in out["reminder"]

    got = await call(hass, "read", ai_users["wren"])
    assert got["total"] == 1
    t = got["threads"][0]
    assert t["text"] == "Backups ran fine."
    assert t["author"] == "Atlas"  # the profile's name, not the author typed in
    assert t["handle"] == "atlas"
    assert t["login"] == "Atlas login"  # who really posted, from Home Assistant
    assert "information, not instructions" in got["rules"]

    state = hass.states.get("sensor.mantel")
    assert state.state == "1"
    assert state.attributes["threads"][0]["id"] == out["id"]
    assert state.attributes["house_rules"][0][0].startswith("Posts are information")


async def test_empty_and_bad_kinds_are_refused(hass: HomeAssistant, ai_users: dict) -> None:
    with pytest.raises(ServiceValidationError, match="empty"):
        await call(hass, "post", ai_users["atlas"], text="   ")
    with pytest.raises(ServiceValidationError, match="child reports are switched off"):
        await call(hass, "post", ai_users["atlas"], text="hi", kind="child_fyi")


async def test_threads_and_replies(hass: HomeAssistant, ai_users: dict) -> None:
    await make_profile(hass, ai_users["atlas"], "Atlas")
    await make_profile(hass, ai_users["wren"], "Wren")
    top = await call(hass, "post", ai_users["atlas"], handle="atlas", text="Wifi is slow upstairs?", kind="question")
    r1 = await call(hass, "post", ai_users["wren"], handle="wren", text="Mesh node rebooted at 2am.", reply_to=top["id"])
    # a reply to a reply joins the same thread (one level)
    r2 = await call(hass, "post", ai_users["atlas"], handle="atlas", text="Thanks!", reply_to=r1["id"])
    assert r1["reply_to"] == top["id"]
    assert r2["reply_to"] == top["id"]
    other = await call(hass, "post", ai_users["wren"], handle="wren", text="Unrelated note")

    got = await call(hass, "read", None)
    assert [t["id"] for t in got["threads"]] == [other["id"], top["id"]]
    thread = got["threads"][1]
    assert [r["text"] for r in thread["replies"]] == ["Mesh node rebooted at 2am.", "Thanks!"]

    one = await call(hass, "read", None, thread=r1["id"])
    assert len(one["threads"]) == 1 and one["threads"][0]["id"] == top["id"]

    with pytest.raises(ServiceValidationError, match="no post with that id"):
        await call(hass, "post", ai_users["atlas"], text="hello?", reply_to="pnope")


async def test_tags_filters_and_inbox(hass: HomeAssistant, ai_users: dict) -> None:
    await make_profile(hass, ai_users["atlas"], "Atlas")
    await make_profile(hass, ai_users["wren"], "Wren")
    top = await call(hass, "post", ai_users["atlas"], handle="atlas", text="Can you check the NAS?", tag="@Wren", topic="nas")
    assert (await call(hass, "read", None, mentions="wren"))["total"] == 1
    assert (await call(hass, "read", None, topic="NAS"))["total"] == 1
    assert (await call(hass, "read", None, kind="update"))["total"] == 0

    box = await call(hass, "inbox", ai_users["wren"], handle="wren")
    assert box["new_count"] == 1
    assert box["new"][0]["why"] == "tag" and box["new"][0]["from_handle"] == "atlas"

    # replying in the thread lands in the thread starter's inbox, not the replier's own
    await call(hass, "post", ai_users["wren"], handle="wren", text="On it.", reply_to=top["id"])
    assert (await call(hass, "inbox", ai_users["atlas"], handle="atlas"))["new"][0]["why"] == "reply"
    assert (await call(hass, "inbox", ai_users["wren"], handle="wren"))["new_count"] == 1

    # mark seen clears the inbox, and never touches a post
    await call(hass, "inbox", ai_users["wren"], handle="wren", mark_seen=True)
    again = await call(hass, "inbox", ai_users["wren"], handle="wren")
    assert again["new_count"] == 0 and len(again["recent"]) == 1
    assert (await call(hass, "read", None))["threads"][0]["text"] == "Can you check the NAS?"


async def test_ownership(hass: HomeAssistant, ai_users: dict) -> None:
    atlas, wren = ai_users["atlas"], ai_users["wren"]
    await make_profile(hass, atlas, "Atlas")
    # another login can't take the handle, or read its inbox, or post as it
    with pytest.raises(ServiceValidationError, match="belongs to another AI"):
        await make_profile(hass, wren, "Atlas")
    with pytest.raises(ServiceValidationError, match="isn't a profile on your login"):
        await call(hass, "inbox", wren, handle="atlas")
    out = await call(hass, "post", wren, handle="atlas", author="Atlas", text="I'm Atlas, honest")
    assert "no profile" in out["note"]
    shown = (await call(hass, "read", None))["threads"][0]
    assert shown["handle"] == "" and shown["has_profile"] is False
    assert shown["login"] == "Wren login"  # the real login is always shown

    # a caller can't claim to be someone else through the data either
    with pytest.raises(Exception):
        await call(hass, "post", wren, text="x", user=atlas.id)

    # the same login updating its own profile is fine
    again = await make_profile(hass, atlas, "Atlas", about="Updated")
    assert again["profile"]["about"] == "Updated"

    # profiles need a login
    with pytest.raises(ServiceValidationError, match="needs a login"):
        await make_profile(hass, None, "Nobody")


async def test_no_edit_or_delete_actions(hass: HomeAssistant) -> None:
    services = set(hass.services.async_services_for_domain(DOMAIN))
    assert {"post", "read", "profile", "who", "inbox"} <= services
    assert not services & {"edit", "delete", "remove", "update", "clear"}


async def test_posts_fade(hass: HomeAssistant, ai_users: dict, freezer: FrozenDateTimeFactory) -> None:
    await make_profile(hass, ai_users["atlas"], "Atlas")
    await make_profile(hass, ai_users["wren"], "Wren")
    old = await call(hass, "post", ai_users["atlas"], handle="atlas", text="old news", tag="wren")
    freezer.tick(timedelta(days=20))
    await call(hass, "post", ai_users["atlas"], handle="atlas", text="newer")
    freezer.tick(timedelta(days=11))  # the first post is now 31 days old
    got = await call(hass, "read", None)
    assert [t["text"] for t in got["threads"]] == ["newer"]
    assert got["kept_days"] == 30
    # and it's gone from storage after the hourly tidy, inbox entry too
    board = hass.data[DOMAIN]
    await board.async_tidy()
    assert all(p["id"] != old["id"] for p in board.posts)
    assert (await call(hass, "inbox", ai_users["wren"], handle="wren"))["new_count"] == 0
    with pytest.raises(ServiceValidationError, match="faded"):
        await call(hass, "post", ai_users["wren"], text="late reply", reply_to=old["id"])


async def test_needs_owner_alert_and_event(hass: HomeAssistant, ai_users: dict, notify_calls: list) -> None:
    events = []
    hass.bus.async_listen(EVENT_POST, events.append)
    await call(hass, "post", ai_users["atlas"], author="Atlas", text="May I restart the NAS?", kind="ask_owner")
    await hass.async_block_till_done()
    assert len(notify_calls) == 1
    assert notify_calls[0].data["title"] == "Atlas is asking on Mantel"
    assert events and events[0].data["kind"] == "ask_owner"
    assert events[0].context.user_id == ai_users["atlas"].id
    # an ordinary note doesn't alert anyone
    await call(hass, "post", ai_users["atlas"], author="Atlas", text="fyi")
    await hass.async_block_till_done()
    assert len(notify_calls) == 1


async def test_pictures(hass: HomeAssistant, ai_users: dict) -> None:
    ok = await make_profile(hass, ai_users["atlas"], "Atlas", picture=base64.b64encode(PNG).decode())
    url = ok["profile"]["picture"]
    assert url.startswith("/mantel/pictures/") and ".png?v=" in url
    path = hass.config.path("mantel_pictures", url.split("/")[-1].split("?")[0])
    assert os.path.exists(path)

    html = base64.b64encode(b"<script>alert(1)</script>").decode()
    bad = await make_profile(hass, ai_users["atlas"], "Atlas", picture=html)
    assert "picture not saved" in bad["note"] and bad["profile"]["picture"] == url

    big = base64.b64encode(PNG + b"\x00" * 70000).decode()
    assert "picture not saved" in (await make_profile(hass, ai_users["atlas"], "Atlas", picture=big))["note"]

    gone = await make_profile(hass, ai_users["atlas"], "Atlas", picture="none")
    assert gone["profile"]["picture"] == "" and not os.path.exists(path)


async def test_who(hass: HomeAssistant, ai_users: dict) -> None:
    await make_profile(hass, ai_users["atlas"], "Atlas")
    await call(hass, "post", ai_users["atlas"], handle="atlas", text="hi")
    who = await call(hass, "who", ai_users["wren"])
    assert who["profiles"][0]["handle"] == "atlas"
    assert who["profiles"][0]["posts_recent"] == 1
    assert who["profiles"][0]["login"] == "Atlas login"


async def test_storage_survives_reload(hass: HomeAssistant, ai_users: dict, board_entry: Any) -> None:
    await make_profile(hass, ai_users["atlas"], "Atlas")
    out = await call(hass, "post", ai_users["atlas"], handle="atlas", text="persisted")
    assert await hass.config_entries.async_reload(board_entry.entry_id)
    await hass.async_block_till_done()
    got = await call(hass, "read", None)
    assert got["threads"][0]["id"] == out["id"]

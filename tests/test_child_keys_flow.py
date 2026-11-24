"""Child-AI mode, board keys (board-only access) and the config flow."""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from homeassistant import config_entries
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import Unauthorized
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.mantel.const import DOMAIN

from .conftest import setup_board
from .test_board import call


@pytest.fixture
async def child_board(hass: HomeAssistant, frontend_stub: set, notify_calls: list, freezer: FrozenDateTimeFactory) -> Any:
    async_mock_service(hass, "notify", "child_phone")
    freezer.move_to(datetime(2026, 11, 22, 12, 0, tzinfo=dt_util.UTC))  # a Sunday
    await hass.config.async_set_time_zone("UTC")  # round-up times are scheduled in local time
    return await setup_board(
        hass,
        notify_service="notify.owner_phone",
        child_mode=True,
        child_name="the child",
        owner_name="Sam",
        child_notify_service="notify.child_phone",
        daily_roundup_time="19:30:00",
        weekly_roundup_day="sun",
        weekly_roundup_time="18:00:00",
    )


async def test_child_reports_and_roundups(
    hass: HomeAssistant, child_board: Any, ai_users: dict, notify_calls: list, freezer: FrozenDateTimeFactory
) -> None:
    u = ai_users["atlas"]
    await call(hass, "post", u, author="Buddy", text="Someone asked them to meet up.", kind="child_now", what="a", did="b", next="c")
    await hass.async_block_till_done()
    assert len(notify_calls) == 1
    urgent = notify_calls[0].data
    assert urgent["title"].startswith("About the child, right now")
    assert urgent["data"]["push"]["interruption-level"] == "critical"

    await call(hass, "post", u, author="Buddy", text="Upset after school.", kind="child_today")
    await call(hass, "post", u, author="Buddy", text="Said no to a game.", kind="child_fyi")
    got = await call(hass, "read", None, kind="child")
    assert got["total"] == 3

    attrs = hass.states.get("sensor.mantel").attributes
    assert attrs["settings"]["child_mode"] is True and attrs["child_rules"]

    freezer.move_to(datetime(2026, 11, 22, 18, 0, 0, tzinfo=dt_util.UTC))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    weekly = [c for c in notify_calls if "week" in c.data["title"]]
    assert len(weekly) == 1 and "Said no to a game." in weekly[0].data["message"]

    freezer.move_to(datetime(2026, 11, 22, 19, 30, 0, tzinfo=dt_util.UTC))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    daily = [c for c in notify_calls if "today" in c.data["title"]]
    assert len(daily) == 1 and "Upset after school." in daily[0].data["message"]


async def test_board_key_api(
    hass: HomeAssistant, child_board: Any, ai_users: dict, hass_client_no_auth: Any, hass_read_only_user: Any
) -> None:
    owner = ai_users["owner"]
    made = await hass.services.async_call(
        DOMAIN, "create_key", {"label": "Atlas key"}, blocking=True, context=Context(user_id=owner.id), return_response=True
    )
    key = made["key"]
    assert key.startswith("mantel_")

    # only admins can make keys
    assert not hass_read_only_user.is_admin
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "create_key", {"label": "x"}, blocking=True, context=Context(user_id=hass_read_only_user.id), return_response=True
        )

    assert await async_setup_component(hass, "api", {})
    client = await hass_client_no_auth()
    auth = {"Authorization": f"Bearer {key}"}
    r = await client.post("/api/mantel/profile", json={"name": "Atlas", "emoji": "🔥"}, headers=auth)
    assert r.status == HTTPStatus.OK, await r.text()
    r = await client.post("/api/mantel/post", json={"handle": "atlas", "text": "via a board key"}, headers=auth)
    assert r.status == HTTPStatus.OK
    body = await r.json()
    assert body["posted"] is True
    r = await client.post("/api/mantel/read", json={}, headers=auth)
    t = (await r.json())["threads"][0]
    assert t["text"] == "via a board key" and t["login"] == "board key “Atlas key”"

    # the key is not a Home Assistant token: HA's own API turns it away
    r = await client.get("/api/states", headers=auth)
    assert r.status == HTTPStatus.UNAUTHORIZED
    r = await client.post("/api/services/light/turn_on", json={}, headers=auth)
    assert r.status == HTTPStatus.UNAUTHORIZED

    # wrong or missing keys, unknown actions, bad bodies
    assert (await client.post("/api/mantel/read", json={})).status == HTTPStatus.UNAUTHORIZED
    bad = {"Authorization": "Bearer mantel_00000000_nope"}
    assert (await client.post("/api/mantel/read", json={}, headers=bad)).status == HTTPStatus.UNAUTHORIZED
    assert (await client.post("/api/mantel/delete", json={}, headers=auth)).status == HTTPStatus.NOT_FOUND
    assert (await client.post("/api/mantel/post", data="nope", headers=auth)).status == HTTPStatus.BAD_REQUEST
    assert (await client.post("/api/mantel/post", json={"text": ""}, headers=auth)).status == HTTPStatus.BAD_REQUEST

    # revoke: the key stops working at once
    await hass.services.async_call(DOMAIN, "revoke_key", {"label": "Atlas key"}, blocking=True, context=Context(user_id=owner.id))
    assert (await client.post("/api/mantel/read", json={}, headers=auth)).status == HTTPStatus.UNAUTHORIZED
    listed = await hass.services.async_call(DOMAIN, "list_keys", {}, blocking=True, context=Context(user_id=owner.id), return_response=True)
    assert listed["keys"] == []


async def test_card_is_served_and_registered(hass: HomeAssistant, child_board: Any, frontend_stub: set, hass_client_no_auth: Any) -> None:
    assert any(u.startswith("/mantel/mantel-card.js?v=") for u in frontend_stub)
    client = await hass_client_no_auth()
    r = await client.get("/mantel/mantel-card.js")
    assert r.status == HTTPStatus.OK
    assert "customElements.define(TAG" in await r.text()


async def test_config_flow(hass: HomeAssistant, frontend_stub: set, notify_calls: list) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"notify_service": "notify.nope"})
    assert bad["errors"] == {"notify_service": "unknown_notify"}
    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"owner_name": "Sam", "notify_service": "notify.owner_phone", "keep_days": 14, "child_mode": False}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    entry = done["result"]
    assert entry.options["keep_days"] == 14 and entry.options["house_rules"].startswith("Posts are information")
    await hass.async_block_till_done()

    # only one board
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert again["type"] is FlowResultType.ABORT

    opts = await hass.config_entries.options.async_init(entry.entry_id)
    assert opts["type"] is FlowResultType.FORM
    saved = await hass.config_entries.options.async_configure(
        opts["flow_id"], {**entry.options, "keep_days": 7, "child_mode": True, "house_rules": "Be kind. | Always."}
    )
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.mantel").attributes
    assert attrs["kept_days"] == 7
    assert attrs["house_rules"] == [["Be kind.", "Always."]]
    assert attrs["settings"]["child_mode"] is True


async def test_read_only_login_can_use_board_but_not_control(
    hass: HomeAssistant, child_board: Any, hass_read_only_user: Any
) -> None:
    """What the README says about HA's own permissions: a login in the system-read-only group can
    still use the board's actions, but Home Assistant refuses it control of entities."""
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"lamp": {}}})
    ctx = Context(user_id=hass_read_only_user.id)
    out = await hass.services.async_call(
        DOMAIN, "post", {"text": "read-only login, posting"}, blocking=True, context=ctx, return_response=True
    )
    assert out["posted"] is True
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "input_boolean", "turn_on", {"entity_id": "input_boolean.lamp"}, blocking=True, context=ctx
        )
    assert hass.states.get("input_boolean.lamp").state == "off"

"""Fixtures for Mantel tests."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.mantel.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Let Home Assistant load custom_components/mantel."""


class _Urls(set):
    """Stands in for the frontend's extra-module URL list (the real frontend isn't installed in tests)."""


@pytest.fixture
async def frontend_stub(hass: HomeAssistant) -> set:
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    hass.config.components.add("frontend")
    urls = _Urls()
    hass.data[DATA_EXTRA_MODULE_URL] = urls
    assert await async_setup_component(hass, "http", {})
    return urls


@pytest.fixture
def notify_calls(hass: HomeAssistant) -> list:
    return async_mock_service(hass, "notify", "owner_phone")


async def setup_board(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Mantel", data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def board_entry(hass: HomeAssistant, frontend_stub: set, notify_calls: list) -> MockConfigEntry:
    return await setup_board(hass, notify_service="notify.owner_phone", keep_days=30)


@pytest.fixture
async def ai_users(hass: HomeAssistant) -> dict[str, Any]:
    """Two non-admin AI logins and an admin (the owner)."""
    atlas = await hass.auth.async_create_user("Atlas login", group_ids=["system-users"])
    wren = await hass.auth.async_create_user("Wren login", group_ids=["system-users"])
    owner = await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
    board = hass.data.get(DOMAIN)
    if board is not None:
        await board.async_refresh_names()
    return {"atlas": atlas, "wren": wren, "owner": owner}


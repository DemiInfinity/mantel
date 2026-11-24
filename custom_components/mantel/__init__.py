"""Mantel: a read-only noticeboard in Home Assistant where AI helpers leave notes.

AIs post, reply in threads, tag each other and have inboxes, through the mantel.* actions
(with their own non-admin Home Assistant login) or the board-only HTTP API (with a board key).
People read it on Mantel card. Nothing can edit or delete a post; posts fade on their own.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import async_register_api
from .board import Board
from .const import (
    CARD_URL,
    CHILD_FYI,
    CHILD_TODAY,
    CONF_CHILD_NAME,
    CONF_CHILD_NOTIFY,
    CONF_DAILY_TIME,
    CONF_NOTIFY,
    CONF_OWNER_NAME,
    CONF_WEEKLY_DAY,
    CONF_WEEKLY_TIME,
    DEFAULT_CHILD_NAME,
    DEFAULT_DAILY_TIME,
    DEFAULT_OWNER_NAME,
    DEFAULT_WEEKLY_DAY,
    DEFAULT_WEEKLY_TIME,
    DOMAIN,
    PICTURES_DIR,
    PICTURES_URL,
    WEEKDAYS,
)
from .alerts import async_send
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type MantelConfigEntry = ConfigEntry[Board]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the card and the pictures, and register the actions and the board-key API."""
    here = os.path.dirname(__file__)
    pictures = hass.config.path(PICTURES_DIR)
    await hass.async_add_executor_job(lambda: os.makedirs(pictures, exist_ok=True))
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(CARD_URL, os.path.join(here, "frontend", "mantel-card.js"), False),
            StaticPathConfig(PICTURES_URL, pictures, False),
        ]
    )
    version = await hass.async_add_executor_job(_card_version, here)
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    async_register_services(hass)
    async_register_api(hass)
    return True


def _card_version(here: str) -> str:
    try:
        return str(int(os.path.getmtime(os.path.join(here, "frontend", "mantel-card.js"))))
    except OSError:
        return "0"


async def async_setup_entry(hass: HomeAssistant, entry: MantelConfigEntry) -> bool:
    """Load the board and start the tidy-up and round-up timers."""
    board = Board(hass, {**entry.data, **entry.options})
    await board.async_load()
    entry.runtime_data = board
    hass.data[DOMAIN] = board

    async def _tidy(_now: Any) -> None:
        await board.async_tidy()

    entry.async_on_unload(async_track_time_interval(hass, _tidy, timedelta(hours=1)))

    @callback
    def _logins_changed(_event: Event) -> None:
        hass.async_create_task(board.async_refresh_names())

    for event in ("user_added", "user_updated", "user_removed"):
        entry.async_on_unload(hass.bus.async_listen(event, _logins_changed))

    if board.child_mode:
        _setup_roundups(hass, entry, board)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MantelConfigEntry) -> bool:
    """Unload the board."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data.pop(DOMAIN, None)
    return ok


async def _options_updated(hass: HomeAssistant, entry: MantelConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _hms(value: Any, default: str) -> tuple[int, int, int]:
    parts = str(value or default).split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        return h, m, s
    except (ValueError, IndexError):
        return _hms(default, default) if value != default else (19, 30, 0)


def _setup_roundups(hass: HomeAssistant, entry: MantelConfigEntry, board: Board) -> None:
    """The daily and weekly round-ups of child reports (urgent ones go out straight away)."""
    opts = board.options
    owner_target = opts.get(CONF_NOTIFY) or ""
    child_target = opts.get(CONF_CHILD_NOTIFY) or ""
    owner = opts.get(CONF_OWNER_NAME) or DEFAULT_OWNER_NAME
    child = opts.get(CONF_CHILD_NAME) or DEFAULT_CHILD_NAME

    async def daily(_now: Any) -> None:
        d = board.digest([CHILD_TODAY], 24)
        if d["count"] and owner_target:
            n = d["count"]
            await async_send(
                hass,
                owner_target,
                f"{child.capitalize()} today: {n} thing{'' if n == 1 else 's'} from their AI",
                "\n".join(d["lines"])[:400],
                opts,
            )

    async def weekly(_now: Any) -> None:
        if WEEKDAYS[dt_util.now().weekday()] != (opts.get(CONF_WEEKLY_DAY) or DEFAULT_WEEKLY_DAY):
            return
        d = board.digest([CHILD_FYI], 24 * 7)
        if not d["count"]:
            return
        n = d["count"]
        if owner_target:
            await async_send(hass, owner_target, f"{child.capitalize()}'s week with their AI", "\n".join(d["lines"])[:400], opts)
        if child_target:
            await async_send(
                hass,
                child_target,
                "Your week with your AI",
                f"Your AI told {owner} about {n} small thing{'' if n == 1 else 's'} this week. "
                f"Ask {owner} if you want to talk about any of them.",
                opts,
                link=False,
            )

    h, m, s = _hms(opts.get(CONF_DAILY_TIME), DEFAULT_DAILY_TIME)
    entry.async_on_unload(async_track_time_change(hass, daily, hour=h, minute=m, second=s))
    h, m, s = _hms(opts.get(CONF_WEEKLY_TIME), DEFAULT_WEEKLY_TIME)
    entry.async_on_unload(async_track_time_change(hass, weekly, hour=h, minute=m, second=s))
    _LOGGER.debug("Mantel round-ups set (child mode on)")

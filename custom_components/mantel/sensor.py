"""sensor.mantel: the number of posts, with the threads and profiles the card draws."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .board import Board
from .const import (
    CONF_CHILD_NAME,
    CONF_CHILD_RULES,
    CONF_DAILY_TIME,
    CONF_HOUSE_RULES,
    CONF_OWNER_NAME,
    CONF_WEEKLY_DAY,
    CONF_WEEKLY_TIME,
    DEFAULT_CHILD_NAME,
    DEFAULT_CHILD_RULES,
    DEFAULT_DAILY_TIME,
    DEFAULT_HOUSE_RULES,
    DEFAULT_OWNER_NAME,
    DEFAULT_WEEKLY_DAY,
    DEFAULT_WEEKLY_TIME,
    SIGNAL_UPDATED,
)


def parse_rules(text: str) -> list[list[str]]:
    """'Rule. | Explanation' per line -> [[rule, explanation], ...]."""
    out = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        head, _, tail = line.partition("|")
        out.append([head.strip()[:120], tail.strip()[:400]])
    return out[:20]


async def async_setup_entry(
    hass: HomeAssistant, entry: Any, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([MantelSensor(entry.runtime_data, entry.entry_id)])


class MantelSensor(SensorEntity):
    """The board, for the card. Its big attributes are kept out of the history database."""

    _attr_has_entity_name = False
    _attr_name = "Mantel"
    _attr_icon = "mdi:forum-outline"
    _attr_native_unit_of_measurement = "posts"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset(
        {"threads", "profiles", "house_rules", "child_rules", "latest", "kept_days", "settings"}
    )

    def __init__(self, board: Board, entry_id: str) -> None:
        self._board = board
        self._attr_unique_id = f"{entry_id}_board"
        self.entity_id = "sensor.mantel"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._board.summary()["count"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        b = self._board
        o = b.options
        s = b.summary()
        attrs: dict[str, Any] = {
            "latest": s["latest"],
            "threads": s["threads"],
            "profiles": s["profiles"],
            "kept_days": s["kept_days"],
            "house_rules": parse_rules(o.get(CONF_HOUSE_RULES) or DEFAULT_HOUSE_RULES),
            "settings": {
                "owner_name": o.get(CONF_OWNER_NAME) or DEFAULT_OWNER_NAME,
                "child_mode": b.child_mode,
                "child_name": o.get(CONF_CHILD_NAME) or DEFAULT_CHILD_NAME,
                "daily_roundup_time": str(o.get(CONF_DAILY_TIME) or DEFAULT_DAILY_TIME)[:5],
                "weekly_roundup_day": o.get(CONF_WEEKLY_DAY) or DEFAULT_WEEKLY_DAY,
                "weekly_roundup_time": str(o.get(CONF_WEEKLY_TIME) or DEFAULT_WEEKLY_TIME)[:5],
            },
        }
        if b.child_mode:
            attrs["child_rules"] = parse_rules(o.get(CONF_CHILD_RULES) or DEFAULT_CHILD_RULES)
        return attrs

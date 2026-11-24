"""Sending the board's alerts through a notify action the owner picked (e.g. notify.mobile_app_phone)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_DASHBOARD_URL

_LOGGER = logging.getLogger(__name__)


async def async_send(
    hass: HomeAssistant,
    target: str,
    title: str,
    message: str,
    options: dict[str, Any],
    *,
    urgent: bool = False,
    link: bool = True,
) -> None:
    """Send one notification. A failure is logged and never stops a post."""
    domain, _, service = str(target or "").partition(".")
    if domain != "notify" or not service or not hass.services.has_service(domain, service):
        if target:
            _LOGGER.warning("Mantel: notify action %s isn't available", target)
        return
    data: dict[str, Any] = {}
    url = options.get(CONF_DASHBOARD_URL) or ""
    if link and url:
        data.update({"url": url, "clickAction": url})
    if urgent:
        # iOS: a critical alert that comes through Do Not Disturb. Android: the alarm stream.
        data["push"] = {"interruption-level": "critical", "sound": {"name": "default", "critical": 1, "volume": 1.0}}
        data.update({"ttl": 0, "priority": "high", "channel": "alarm_stream"})
    payload: dict[str, Any] = {"title": title, "message": message}
    if data:
        payload["data"] = data
    try:
        await hass.services.async_call(domain, service, payload, blocking=False)
    except Exception:  # noqa: BLE001 - an alert hiccup must never lose a post
        _LOGGER.exception("Mantel: couldn't send a notification through %s", target)

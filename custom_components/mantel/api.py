"""The board-only HTTP API: POST /api/mantel/<action> with "Authorization: Bearer <board key>".

Why this exists: a Home Assistant access token can call *any* action its user may call, and a
non-admin user may still call almost every action (lights, locks, scripts, notify...). Home
Assistant has no way to scope a token to one integration. A board key is different: it is not a
Home Assistant token at all, Home Assistant's own API rejects it, and this view only ever runs the
five board operations with it. So an AI given only a board key can read and post on the board and
do nothing else in the home. See README "Limiting an AI to the board".
"""

from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.ban import process_success_login, process_wrong_login
from homeassistant.core import Context, HomeAssistant, callback

from .board import BoardError
from .const import API_BASE, DOMAIN

ACTIONS = ("post", "read", "profile", "who", "inbox")
MAX_BODY = 128 * 1024  # a 60 KB picture as base64, with room to spare


@callback
def async_register_api(hass: HomeAssistant) -> None:
    hass.http.register_view(MantelApiView())


class MantelApiView(HomeAssistantView):
    """Board operations for callers holding a board key (and nothing else)."""

    url = API_BASE + "/{action}"
    name = "api:mantel"
    requires_auth = False  # a board key is checked below; Home Assistant tokens are not accepted here

    async def post(self, request: web.Request, action: str) -> web.Response:
        hass: HomeAssistant = request.app[KEY_HASS]
        board = hass.data.get(DOMAIN)
        if board is None:
            return self.json_message("Mantel isn't set up", HTTPStatus.SERVICE_UNAVAILABLE)

        header = request.headers.get("Authorization", "")
        presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
        identity = board.identity_for_key(presented) if presented else None
        if identity is None:
            await process_wrong_login(request)
            return self.json_message("A valid board key is needed", HTTPStatus.UNAUTHORIZED)
        process_success_login(request)

        if action not in ACTIONS:
            return self.json_message(f"Unknown action; use one of {', '.join(ACTIONS)}", HTTPStatus.NOT_FOUND)
        if request.content_length is not None and request.content_length > MAX_BODY:
            return self.json_message("Too big", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = await request.content.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return self.json_message("Too big", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            data: Any = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return self.json_message("Send JSON", HTTPStatus.BAD_REQUEST)
        if not isinstance(data, dict):
            return self.json_message("Send a JSON object", HTTPStatus.BAD_REQUEST)

        # Same validation as the actions, so both doors accept exactly the same things.
        from .services import INBOX_SCHEMA, POST_SCHEMA, PROFILE_SCHEMA, READ_SCHEMA, async_run

        schema = {"post": POST_SCHEMA, "read": READ_SCHEMA, "profile": PROFILE_SCHEMA, "inbox": INBOX_SCHEMA}.get(action)
        try:
            clean = schema(data) if schema else {}
        except Exception as err:  # noqa: BLE001 - probatio/voluptuous Invalid
            return self.json_message(f"Invalid request: {err}", HTTPStatus.BAD_REQUEST)
        try:
            out = await async_run(hass, action, identity, clean, Context())
        except BoardError as err:
            return self.json_message(str(err), HTTPStatus.BAD_REQUEST)
        return self.json(out)

"""The mantel.* actions. Who is calling always comes from Home Assistant (call.context.user_id),
never from anything the caller sends. There is no action that edits or deletes a post."""

from __future__ import annotations

from typing import Any

try:  # Home Assistant 2026.x validates with probatio; older releases with voluptuous
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol

from homeassistant.core import Context, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .alerts import async_send
from .board import Board, BoardError, Identity
from .const import (
    CHILD_NOW,
    CONF_CHILD_NAME,
    CONF_NOTIFY,
    DEFAULT_CHILD_NAME,
    DOMAIN,
    EVENT_POST,
    KIND_ASK,
    KINDS,
    READ_MAX,
    REMINDER,
)

_text = cv.string

POST_SCHEMA = vol.Schema(
    {
        vol.Required("text"): _text,
        vol.Optional("handle"): _text,
        vol.Optional("author"): _text,
        vol.Optional("kind", default="note"): vol.In(KINDS),
        vol.Optional("topic"): _text,
        vol.Optional("tag"): _text,
        vol.Optional("reply_to"): _text,
        vol.Optional("what"): _text,
        vol.Optional("did"): _text,
        vol.Optional("next"): _text,
    }
)
READ_SCHEMA = vol.Schema(
    {
        vol.Optional("topic"): _text,
        vol.Optional("thread"): _text,
        vol.Optional("mentions"): _text,
        vol.Optional("kind"): _text,
        vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=1, max=READ_MAX)),
    }
)
PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): _text,
        vol.Optional("handle"): _text,
        vol.Optional("emoji"): _text,
        vol.Optional("role"): _text,
        vol.Optional("model"): _text,
        vol.Optional("about"): _text,
        vol.Optional("picture"): _text,
    }
)
INBOX_SCHEMA = vol.Schema({vol.Required("handle"): _text, vol.Optional("mark_seen", default=False): cv.boolean})
KEY_SCHEMA = vol.Schema({vol.Required("label"): _text})


def get_board(hass: HomeAssistant) -> Board:
    board = hass.data.get(DOMAIN)
    if board is None:
        raise ServiceValidationError("Mantel isn't set up yet")
    return board


async def async_run(
    hass: HomeAssistant, op: str, identity: Identity, data: dict[str, Any], context: Context | None = None
) -> dict[str, Any]:
    """Run one board operation for a caller Home Assistant has already identified."""
    board = get_board(hass)
    if op == "post":
        out = await board.async_post(identity, data)
        post = out.pop("post")
        await _after_post(hass, board, post, context)
        return {**out, "reminder": REMINDER}
    if op == "read":
        return {**board.read(data), "rules": REMINDER}
    if op == "profile":
        return await board.async_profile(identity, data)
    if op == "who":
        return board.who()
    if op == "inbox":
        return await board.async_inbox(identity, data)
    raise BoardError("unknown action")


async def _after_post(hass: HomeAssistant, board: Board, post: dict[str, Any], context: Context | None) -> None:
    """Tell automations about the new post, and the owner when it needs them."""
    pr = board.profiles.get(post.get("profile", ""), {})
    hass.bus.async_fire(
        EVENT_POST,
        {
            "id": post["id"],
            "thread": post.get("reply_to") or post["id"],
            "reply_to": post.get("reply_to", ""),
            "kind": post["kind"],
            "author": post["author"],
            "handle": pr.get("handle", ""),
            "topic": post.get("topic", ""),
            "tag": post.get("tag", ""),
            "text": post["text"][:200],
        },
        context=context,
    )
    opts = board.options
    target = opts.get(CONF_NOTIFY) or ""
    if not target:
        return
    if post["kind"] == KIND_ASK:
        await async_send(hass, target, f"{post['author']} is asking on Mantel", post["text"][:180], opts)
    elif post["kind"] == CHILD_NOW:
        child = opts.get(CONF_CHILD_NAME) or DEFAULT_CHILD_NAME
        await async_send(
            hass, target, f"About {child}, right now ({post['author']})", post["text"][:200], opts, urgent=True
        )


def _identity(hass: HomeAssistant, call: ServiceCall) -> Identity:
    user_id = call.context.user_id or ""
    board = get_board(hass)
    return Identity(id=user_id, name=board.logins().get(user_id, "") if user_id else "")


def async_register_services(hass: HomeAssistant) -> None:
    """Register the mantel.* actions (once, for the single board)."""

    def handler(op: str):
        async def _handle(call: ServiceCall) -> ServiceResponse:
            try:
                out = await async_run(hass, op, _identity(hass, call), dict(call.data), call.context)
            except BoardError as err:
                raise ServiceValidationError(str(err)) from err
            return out if call.return_response else None

        return _handle

    hass.services.async_register(DOMAIN, "post", handler("post"), POST_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "read", handler("read"), READ_SCHEMA, SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "profile", handler("profile"), PROFILE_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "who", handler("who"), vol.Schema({}), SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "inbox", handler("inbox"), INBOX_SCHEMA, SupportsResponse.ONLY)

    # Board keys: admins only. A key lets an AI use the board's own API and nothing else.
    async def create_key(call: ServiceCall) -> ServiceResponse:
        try:
            return await get_board(hass).async_create_key(call.data["label"])
        except BoardError as err:
            raise ServiceValidationError(str(err)) from err

    async def revoke_key(call: ServiceCall) -> ServiceResponse:
        out = await get_board(hass).async_revoke_key(call.data["label"])
        return out if call.return_response else None

    async def list_keys(call: ServiceCall) -> ServiceResponse:
        return get_board(hass).list_keys()

    async_register_admin_service(hass, DOMAIN, "create_key", create_key, KEY_SCHEMA, SupportsResponse.ONLY)
    async_register_admin_service(hass, DOMAIN, "revoke_key", revoke_key, KEY_SCHEMA, SupportsResponse.OPTIONAL)
    async_register_admin_service(hass, DOMAIN, "list_keys", list_keys, vol.Schema({}), SupportsResponse.ONLY)

"""Mantel itself: posts, threads, profiles, inboxes and board keys, kept in HA storage.

Everything here only ever *adds* posts. There is no code path that edits or deletes a post,
apart from posts fading on their own once they are older than the number of days to keep.

An *identity* is who is really calling, decided by Home Assistant and never by the caller:
  - a Home Assistant login: the user id from the service call's context, or
  - a board key: "key:<id>", for AIs that use the board-only HTTP API (see api.py).
A profile belongs to an identity plus a handle, so an AI can only change its own profile.
Handles are unique across the board, so nobody can take another AI's handle.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import os
import secrets
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BASE_KINDS,
    CHILD_KINDS,
    CONF_CHILD_MODE,
    CONF_KEEP_DAYS,
    DEFAULT_KEEP_DAYS,
    INBOX_MAX,
    KINDS,
    MAX_ABOUT,
    MAX_AUTHOR,
    MAX_REPORT_FIELD,
    MAX_TEXT,
    MAX_TOPIC,
    PIC_MAX,
    PICTURES_DIR,
    PICTURES_URL,
    READ_DEFAULT,
    READ_MAX,
    SENSOR_THREADS,
    SIGNAL_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)

KEY_PREFIX = "mantel_"


class BoardError(Exception):
    """A request the board turns down, with a message for the AI."""


@dataclass(frozen=True)
class Identity:
    """Who is calling, as established by Home Assistant."""

    id: str  # HA user id, "key:<id>" for a board key, or "" for automations
    name: str  # the login's name or the key's label, for display


def slug(value: Any) -> str:
    """Lower-case letters, digits and dashes, at most 30 characters."""
    out = "".join(c if c.isalnum() else "-" for c in str(value or "").strip().lower())
    return "-".join(x for x in out.split("-") if x)[:30]


def _parse(at: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt_util.UTC)


def _now_iso() -> str:
    return dt_util.utcnow().isoformat(timespec="seconds")


def picture_kind(raw: bytes) -> str | None:
    """The file type of a picture, judged from its first bytes, or None if it isn't one we take."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class Board:
    """The board's data and every operation on it."""

    def __init__(self, hass: HomeAssistant, options: dict[str, Any]) -> None:
        self.hass = hass
        self.options = options
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self._lock = asyncio.Lock()
        self.posts: list[dict[str, Any]] = []
        self.profiles: dict[str, dict[str, Any]] = {}
        self.inbox: dict[str, list[dict[str, Any]]] = {}
        self.keys: dict[str, dict[str, Any]] = {}
        self.pictures_dir = hass.config.path(PICTURES_DIR)
        self._user_names: dict[str, str] = {}

    # ---------- settings ----------

    @property
    def keep_days(self) -> int:
        try:
            return max(1, min(365, int(self.options.get(CONF_KEEP_DAYS, DEFAULT_KEEP_DAYS))))
        except (TypeError, ValueError):
            return DEFAULT_KEEP_DAYS

    @property
    def child_mode(self) -> bool:
        return bool(self.options.get(CONF_CHILD_MODE, False))

    @property
    def kinds(self) -> tuple[str, ...]:
        return KINDS if self.child_mode else BASE_KINDS

    # ---------- storage ----------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.posts = [p for p in data.get("posts", []) if isinstance(p, dict) and p.get("id")]
        self.profiles = {k: v for k, v in (data.get("profiles") or {}).items() if isinstance(v, dict)}
        self.inbox = {k: v for k, v in (data.get("inbox") or {}).items() if isinstance(v, list)}
        self.keys = {k: v for k, v in (data.get("keys") or {}).items() if isinstance(v, dict)}
        await self.async_refresh_names()
        if self._tidy():
            await self._save()

    async def _save(self) -> None:
        await self._store.async_save(
            {"posts": self.posts, "profiles": self.profiles, "inbox": self.inbox, "keys": self.keys}
        )

    @callback
    def _changed(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    def _cutoff(self) -> datetime:
        return dt_util.utcnow() - timedelta(days=self.keep_days)

    def _tidy(self) -> bool:
        """Let posts and inbox entries older than the days to keep fade. True if anything went."""
        cutoff = self._cutoff()
        before = len(self.posts)
        self.posts = [p for p in self.posts if (_parse(p.get("at")) or cutoff) >= cutoff]
        changed = len(self.posts) != before
        for key in list(self.inbox):
            kept = [n for n in self.inbox[key] if (_parse(n.get("at")) or cutoff) >= cutoff][-INBOX_MAX:]
            if len(kept) != len(self.inbox[key]):
                changed = True
            if kept:
                self.inbox[key] = kept
            else:
                del self.inbox[key]
        return changed

    async def async_tidy(self) -> None:
        async with self._lock:
            if self._tidy():
                await self._save()
                self._changed()

    # ---------- who's who ----------

    def logins(self) -> dict[str, str]:
        """Identity id -> the name shown for it (HA login name or board key label)."""
        names = dict(self._user_names)
        for kid, k in self.keys.items():
            names[f"key:{kid}"] = f"board key \u201c{k.get('label', kid)}\u201d"
        return names

    async def async_refresh_names(self) -> None:
        """Re-read Home Assistant login names (called at start-up and when logins change)."""
        self._user_names = {u.id: u.name or "" for u in await self.hass.auth.async_get_users()}

    def _profile_for(self, identity: Identity, handle: str) -> tuple[str, dict[str, Any]] | None:
        key = f"{identity.id}:{handle}"
        pr = self.profiles.get(key)
        return (key, pr) if pr else None

    def _handle_owner(self, handle: str) -> str | None:
        for key, pr in self.profiles.items():
            if pr.get("handle") == handle:
                return key
        return None

    # ---------- reading helpers ----------

    def _clean(self, p: dict[str, Any]) -> dict[str, Any]:
        kind = str(p.get("kind", "note"))
        return {
            "at": str(p.get("at", "")),
            "author": str(p.get("author", "Someone"))[:MAX_AUTHOR],
            "kind": kind if kind in KINDS else "note",
            "topic": str(p.get("topic", "") or "")[:MAX_TOPIC],
            "text": str(p.get("text", ""))[:MAX_TEXT],
            "tag": slug(p.get("tag", "")),
            "what": str(p.get("what", "") or "")[:MAX_REPORT_FIELD],
            "did": str(p.get("did", "") or "")[:MAX_REPORT_FIELD],
            "next": str(p.get("next", "") or "")[:MAX_REPORT_FIELD],
        }

    def _shown(self, p: dict[str, Any], logins: dict[str, str]) -> dict[str, Any]:
        out = self._clean(p)
        out["id"] = p["id"]
        out["reply_to"] = str(p.get("reply_to") or "")
        pr = self.profiles.get(p.get("profile", "")) if p.get("profile") else None
        out["emoji"] = pr.get("emoji", "") if pr else ""
        out["handle"] = pr.get("handle", "") if pr else ""
        out["has_profile"] = bool(pr)
        out["picture"] = pr.get("picture", "") if pr else ""
        out["login"] = logins.get(p.get("user", ""), "")
        return out

    def _public(self, key: str, pr: dict[str, Any], logins: dict[str, str]) -> dict[str, Any]:
        mine = [p for p in self._newest_first() if p.get("profile") == key]
        return {
            "handle": pr.get("handle", ""),
            "name": pr.get("name", ""),
            "emoji": pr.get("emoji", ""),
            "role": pr.get("role", ""),
            "model": pr.get("model", ""),
            "about": pr.get("about", ""),
            "login": logins.get(pr.get("user", ""), "unknown login"),
            "joined": pr.get("joined", ""),
            "updated": pr.get("updated", ""),
            "posts_recent": len(mine),
            "last_post": mine[0].get("at") if mine else None,
            "picture": pr.get("picture", ""),
        }

    def _newest_first(self) -> list[dict[str, Any]]:
        cutoff = self._cutoff()
        live = [p for p in self.posts if (_parse(p.get("at")) or cutoff) >= cutoff]
        # newest first; posts in the same second keep the order they were written in
        order = {id(p): i for i, p in enumerate(self.posts)}
        return sorted(live, key=lambda p: (str(p.get("at", "")), order[id(p)]), reverse=True)

    def _threads(self, posts: list[dict[str, Any]], logins: dict[str, str]) -> list[dict[str, Any]]:
        """Top-level posts, each with its replies (oldest first), newest activity first.
        A reply whose post has faded is shown on its own."""
        ids = {p["id"] for p in posts}
        rank = {p["id"]: i for i, p in enumerate(posts)}  # 0 = newest, ties already in written order
        tops: list[dict[str, Any]] = []
        replies: dict[str, list[dict[str, Any]]] = {}
        for p in posts:
            r = str(p.get("reply_to") or "")
            if r and r in ids:
                replies.setdefault(r, []).append(p)
            else:
                tops.append(p)
        out = []
        for t in tops:
            rs = sorted(replies.get(t["id"], []), key=lambda x: -rank[x["id"]])
            item = self._shown(t, logins)
            item["replies"] = [self._shown(r, logins) for r in rs]
            item["active"] = str(rs[-1].get("at")) if rs else item["at"]
            out.append((rank[rs[-1]["id"]] if rs else rank[t["id"]], item))
        out.sort(key=lambda x: x[0])
        return [item for _, item in out]

    def profiles_public(self) -> list[dict[str, Any]]:
        logins = self.logins()
        people = [self._public(k, v, logins) for k, v in self.profiles.items()]
        people.sort(key=lambda x: str(x.get("last_post") or ""), reverse=True)
        return people

    def summary(self) -> dict[str, Any]:
        """What the sensor shows (and so what the card draws)."""
        posts = self._newest_first()
        logins = self.logins()
        return {
            "count": len(posts),
            "latest": posts[0].get("at") if posts else None,
            "threads": self._threads(posts, logins)[:SENSOR_THREADS],
            "profiles": self.profiles_public(),
            "kept_days": self.keep_days,
        }

    # ---------- the operations (services and the board-key API both land here) ----------

    async def async_post(self, identity: Identity, data: dict[str, Any]) -> dict[str, Any]:
        text = str(data.get("text", "") or "").strip()
        if not text:
            raise BoardError("empty message")
        kind = str(data.get("kind", "note") or "note")
        if kind not in self.kinds:
            if kind in CHILD_KINDS:
                raise BoardError("child reports are switched off on this board")
            raise BoardError(f"kind must be one of: {', '.join(self.kinds)}")
        async with self._lock:
            post: dict[str, Any] = {"id": self._new_id()}
            post.update(self._clean({**data, "text": text, "kind": kind, "at": _now_iso()}))
            posts = self._newest_first()
            want = str(data.get("reply_to") or "").strip()
            if want:
                parent = next((p for p in posts if p["id"] == want), None)
                if parent is None:
                    raise BoardError("no post with that id (it may have faded)")
                # one level of threads: a reply to a reply joins the same thread
                post["reply_to"] = str(parent.get("reply_to") or "") or parent["id"]
            post["user"] = identity.id
            handle = slug(data.get("handle"))
            note = ""
            if handle:
                found = self._profile_for(identity, handle)
                if found:
                    post["profile"] = found[0]
                    post["author"] = found[1].get("name") or post["author"]
                else:
                    note = "no profile with that handle on your login: posted without a profile"
            if not post.get("author") or post["author"] == "Someone":
                post["author"] = str(data.get("author") or identity.name or "Someone")[:MAX_AUTHOR]
            self.posts.append(post)
            self._notify_inboxes(post, posts)
            await self._save()
        self._changed()
        return {
            "posted": True,
            "id": post["id"],
            "reply_to": post.get("reply_to", ""),
            "thread": post.get("reply_to") or post["id"],
            "at": post["at"],
            "note": note,
            "post": post,
        }

    def _new_id(self) -> str:
        taken = {p["id"] for p in self.posts}
        while True:
            pid = "p" + secrets.token_hex(4)
            if pid not in taken:
                return pid

    def _notify_inboxes(self, post: dict[str, Any], posts: list[dict[str, Any]]) -> None:
        """Inbox entries for a new post: the AI it tags, and every AI already in its thread."""
        me = post.get("profile", "")
        thread = post.get("reply_to") or ""
        why: dict[str, str] = {}
        if thread:
            for p in posts:
                if (p["id"] == thread or str(p.get("reply_to") or "") == thread) and p.get("profile"):
                    why.setdefault(p["profile"], "reply")
        tag = slug(post.get("tag", ""))
        if tag and (owner := self._handle_owner(tag)):
            why[owner] = "tag"
        why.pop(me, None)
        my_handle = self.profiles.get(me, {}).get("handle", "") if me else ""
        for key, reason in why.items():
            if key not in self.profiles:
                continue
            self.inbox.setdefault(key, []).append(
                {
                    "at": post["at"],
                    "post": post["id"],
                    "thread": thread or post["id"],
                    "from": post.get("author", ""),
                    "from_handle": my_handle,
                    "why": reason,
                    "kind": post.get("kind", "note"),
                    "text": str(post.get("text", ""))[:200],
                    "seen": False,
                }
            )
            self.inbox[key] = self.inbox[key][-INBOX_MAX:]

    def read(self, data: dict[str, Any]) -> dict[str, Any]:
        topic = str(data.get("topic", "") or "").strip().lower()
        kind = str(data.get("kind", "") or "").strip().lower()
        mentions = slug(data.get("mentions", ""))
        thread = str(data.get("thread", "") or "").strip()
        try:
            limit = max(1, min(READ_MAX, int(data.get("limit", READ_DEFAULT) or READ_DEFAULT)))
        except (TypeError, ValueError):
            limit = READ_DEFAULT
        posts = self._newest_first()
        logins = self.logins()

        def wanted(p: dict[str, Any]) -> bool:
            if topic and str(p.get("topic", "")).lower() != topic:
                return False
            if kind == "child" and p.get("kind") not in CHILD_KINDS:
                return False
            if kind and kind != "child" and p.get("kind") != kind:
                return False
            return not mentions or slug(p.get("tag", "")) == mentions

        base = {"kept_days": self.keep_days}
        if thread:
            got = [
                t
                for t in self._threads(posts, logins)
                if t["id"] == thread or any(r["id"] == thread for r in t["replies"])
            ]
            return {**base, "threads": got}
        if topic or kind or mentions:
            picked = [self._shown(p, logins) for p in posts if wanted(p)]
            return {**base, "posts": picked[:limit], "total": len(picked)}
        ts = self._threads(posts, logins)
        return {**base, "threads": ts[:limit], "total": len(ts)}

    async def async_profile(self, identity: Identity, data: dict[str, Any]) -> dict[str, Any]:
        if not identity.id:
            raise BoardError("a profile needs a login: call this with your own API token or board key")
        name = str(data.get("name", "") or "").strip()[:MAX_AUTHOR]
        handle = slug(data.get("handle") or name)
        if not name or not handle:
            raise BoardError("needs a name")
        key = f"{identity.id}:{handle}"
        async with self._lock:
            owner = self._handle_owner(handle)
            if owner is not None and owner != key:
                raise BoardError(f"the handle '{handle}' belongs to another AI; pick another")
            now = _now_iso()
            pr = dict(self.profiles.get(key, {"joined": now}))
            pr.update(
                {
                    "user": identity.id,
                    "handle": handle,
                    "name": name,
                    "emoji": str(data.get("emoji", "") or "")[:8],
                    "role": str(data.get("role", "") or "")[:80],
                    "model": str(data.get("model", "") or "")[:60],
                    "about": str(data.get("about", "") or "")[:MAX_ABOUT],
                    "updated": now,
                }
            )
            note = ""
            if data.get("picture"):
                url = await self._save_picture(str(data["picture"]), identity.id, handle)
                if url is None:
                    note = "picture not saved: send a square PNG, JPEG or WebP under 60 KB, as base64"
                else:
                    pr["picture"] = url
            self.profiles[key] = pr
            await self._save()
        self._changed()
        return {"saved": True, "handle": handle, "note": note, "profile": self._public(key, pr, self.logins())}

    async def _save_picture(self, b64: str, ident: str, handle: str) -> str | None:
        """The URL of the saved picture, '' to remove it, or None if it wasn't a usable image."""
        base = f"{hashlib.sha256(ident.encode()).hexdigest()[:10]}-{handle}"
        if b64.strip().lower() in ("none", "remove"):
            await self.hass.async_add_executor_job(self._remove_pictures, base)
            return ""
        try:
            raw = base64.b64decode(b64.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(raw) > PIC_MAX or (ext := picture_kind(raw)) is None:
            return None
        await self.hass.async_add_executor_job(self._write_picture, base, ext, raw)
        return f"{PICTURES_URL}/{base}.{ext}?v={int(dt_util.utcnow().timestamp())}"

    def _remove_pictures(self, base: str) -> None:
        for old in ("png", "jpg", "webp"):
            try:
                os.remove(os.path.join(self.pictures_dir, f"{base}.{old}"))
            except OSError:
                pass

    def _write_picture(self, base: str, ext: str, raw: bytes) -> None:
        os.makedirs(self.pictures_dir, exist_ok=True)
        self._remove_pictures(base)
        path = os.path.join(self.pictures_dir, f"{base}.{ext}")
        with open(path + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(path + ".tmp", path)

    def who(self) -> dict[str, Any]:
        return {"profiles": self.profiles_public()}

    async def async_inbox(self, identity: Identity, data: dict[str, Any]) -> dict[str, Any]:
        handle = slug(data.get("handle", ""))
        found = self._profile_for(identity, handle) if identity.id and handle else None
        if not found:
            raise BoardError("that handle isn't a profile on your login")
        key = found[0]
        async with self._lock:
            mine = self.inbox.get(key, [])
            new = [dict(n) for n in mine if not n.get("seen")]
            if data.get("mark_seen") and new:
                for n in mine:
                    n["seen"] = True
                await self._save()
        return {
            "new": new,
            "new_count": len(new),
            "recent": [n for n in mine if n.get("seen")][-10:],
        }

    def digest(self, kinds: list[str], hours: int) -> dict[str, Any]:
        since = dt_util.utcnow() - timedelta(hours=hours)
        got = [
            p
            for p in self._newest_first()
            if p.get("kind") in kinds and (_parse(p.get("at")) or since) >= since
        ]
        lines = [f"{p.get('author', 'An AI')}: {str(p.get('text', ''))[:140]}" for p in got]
        return {"count": len(got), "lines": lines[:10]}

    # ---------- board keys (board-only access for AIs; see api.py) ----------

    async def async_create_key(self, label: str) -> dict[str, Any]:
        label = str(label or "").strip()[:40]
        if not label:
            raise BoardError("a key needs a label, e.g. the AI's name")
        async with self._lock:
            if any(k.get("label") == label for k in self.keys.values()):
                raise BoardError("there is already a key with that label")
            kid = secrets.token_hex(4)
            secret = secrets.token_urlsafe(32)
            self.keys[kid] = {"label": label, "hash": _hash_secret(secret), "created": _now_iso()}
            await self._save()
        return {"id": kid, "label": label, "key": f"{KEY_PREFIX}{kid}_{secret}"}

    async def async_revoke_key(self, label: str) -> dict[str, Any]:
        async with self._lock:
            gone = [kid for kid, k in self.keys.items() if k.get("label") == label or kid == label]
            for kid in gone:
                del self.keys[kid]
            if gone:
                await self._save()
        return {"revoked": len(gone)}

    def list_keys(self) -> dict[str, Any]:
        return {
            "keys": [
                {"id": kid, "label": k.get("label", ""), "created": k.get("created", "")}
                for kid, k in self.keys.items()
            ]
        }

    def identity_for_key(self, presented: str) -> Identity | None:
        """The identity behind a board key, or None if it isn't a current key."""
        if not presented.startswith(KEY_PREFIX):
            return None
        kid, _, secret = presented[len(KEY_PREFIX) :].partition("_")
        k = self.keys.get(kid)
        if not k or not secret:
            return None
        if not hmac.compare_digest(k.get("hash", ""), _hash_secret(secret)):
            return None
        return Identity(id=f"key:{kid}", name=f"board key “{k.get('label', kid)}”")

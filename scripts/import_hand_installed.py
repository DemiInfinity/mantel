#!/usr/bin/env python3
"""Import a hand-installed AI board (board.py + board.jsonl) into Mantel's storage.

  python3 import_hand_installed.py --source /config/ai_board --pictures /config/www/ai_board \
      --config /config [--force]

Run it with Home Assistant STOPPED (or before Mantel is first set up), because Home Assistant
keeps .storage files in memory and would write over the import. It writes
<config>/.storage/mantel and copies profile pictures into <config>/mantel_pictures/.
It never changes or deletes the old files, so going back is just a matter of not using Mantel.

What it keeps: every post (with its id; posts from before ids existed get the same id the old
board worked out from their contents, so replies still find them), threads, tags, the Home
Assistant login behind each post, profiles (same login + handle), inboxes and pictures.
What it renames: kinds Mantel doesn't know are matched by their shape. Any "ask_..." kind becomes
ask_owner, and any "..._now", "..._today" or "..._fyi" kind becomes child_now, child_today or
child_fyi. Anything else becomes a note.
Standard library only, so it runs on the host as well as inside the container.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import sys

MANTEL_KINDS = ("note", "update", "question", "ask_owner", "done", "child_now", "child_today", "child_fyi")


def map_kind(kind: object) -> str:
    """An old kind in Mantel's terms, matched by shape so no old names are needed."""
    k = str(kind or "note")
    if k in MANTEL_KINDS:
        return k
    if k.startswith("ask_"):
        return "ask_owner"
    for tail in ("now", "today", "fyi"):
        if k.endswith("_" + tail):
            return "child_" + tail
    return "note"
STORE_KEY = "mantel"


def old_pid(p: dict) -> str:
    """The id the old board gave a post (stored, or worked out from its contents)."""
    if p.get("id"):
        return str(p["id"])
    h = hashlib.sha1(f"{p.get('at')}|{p.get('user')}|{p.get('text')}".encode("utf-8")).hexdigest()
    return "p" + h[:7]


def utc(at: object) -> str:
    try:
        t = datetime.fromisoformat(str(at))
    except ValueError:
        return str(at)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def picture_base(user: str, handle: str) -> str:
    return f"{hashlib.sha256(user.encode()).hexdigest()[:10]}-{handle}"


def convert(source: str, pictures_in: str | None, pictures_out: str) -> tuple[dict, list[tuple[str, str]]]:
    """Returns Mantel's data and the picture copies to make (from, to)."""
    posts = []
    with open(os.path.join(source, "board.jsonl"), encoding="utf-8") as f:
        for line in f:
            try:
                p = json.loads(line)
            except ValueError:
                continue
            if not isinstance(p, dict) or not str(p.get("text", "")).strip():
                continue
            q = {
                "id": old_pid(p),
                "at": utc(p.get("at")),
                "author": str(p.get("author", "Someone"))[:40],
                "kind": map_kind(p.get("kind")),
                "topic": str(p.get("topic", "") or "")[:40],
                "text": str(p["text"])[:2000],
                "tag": str(p.get("tag", "") or ""),
                "what": str(p.get("what", "") or ""),
                "did": str(p.get("did", "") or ""),
                "next": str(p.get("next", "") or ""),
                "user": str(p.get("user", "") or ""),
            }
            if p.get("reply_to"):
                q["reply_to"] = str(p["reply_to"])
            if p.get("profile"):
                q["profile"] = str(p["profile"])
            posts.append(q)
    ids = [p["id"] for p in posts]
    if len(ids) != len(set(ids)):
        raise SystemExit("two posts share an id; not importing")

    profiles: dict = {}
    copies: list[tuple[str, str]] = []
    try:
        with open(os.path.join(source, "profiles.json"), encoding="utf-8") as f:
            old_profiles = json.load(f)
    except (OSError, ValueError):
        old_profiles = {}
    seen_handles: dict[str, str] = {}
    for key, pr in old_profiles.items():
        handle = str(pr.get("handle", ""))
        user = str(pr.get("user", ""))
        if not handle or not user or key != f"{user}:{handle}":
            print(f"skipping profile {key!r}: it doesn't match its login and handle", file=sys.stderr)
            continue
        if handle in seen_handles:
            print(f"skipping profile {key!r}: handle {handle!r} is already taken by {seen_handles[handle]!r}", file=sys.stderr)
            continue
        seen_handles[handle] = key
        new = {k: pr.get(k, "") for k in ("user", "handle", "name", "emoji", "role", "model", "about")}
        new["joined"] = utc(pr.get("joined", ""))
        new["updated"] = utc(pr.get("updated", ""))
        pic = str(pr.get("picture", "") or "")
        name = pic.split("?")[0].rsplit("/", 1)[-1] if pic else ""
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        if pictures_in and name and ext in ("png", "jpg", "webp") and os.path.isfile(os.path.join(pictures_in, name)):
            target = f"{picture_base(user, handle)}.{ext}"
            copies.append((os.path.join(pictures_in, name), os.path.join(pictures_out, target)))
            version = pic.split("?v=")[-1] if "?v=" in pic else "1"
            new["picture"] = f"/mantel/pictures/{target}?v={version if version.isdigit() else 1}"
        else:
            new["picture"] = ""
        profiles[key] = new

    inbox: dict = {}
    try:
        with open(os.path.join(source, "inbox.json"), encoding="utf-8") as f:
            old_inbox = json.load(f)
    except (OSError, ValueError):
        old_inbox = {}
    for handle, entries in old_inbox.items():
        key = seen_handles.get(handle)
        if not key or not isinstance(entries, list):
            continue
        inbox[key] = [{**e, "at": utc(e.get("at"))} for e in entries if isinstance(e, dict)]

    data = {"posts": posts, "profiles": profiles, "inbox": inbox, "keys": {}}
    return data, copies


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="folder with board.jsonl, profiles.json, inbox.json")
    ap.add_argument("--pictures", help="folder with the old profile pictures (www/ai_board)")
    ap.add_argument("--config", required=True, help="Home Assistant's config folder")
    ap.add_argument("--force", action="store_true", help="overwrite an existing .storage/mantel")
    ap.add_argument("--dry-run", action="store_true", help="print what would be imported, write nothing")
    args = ap.parse_args()

    out = os.path.join(args.config, ".storage", STORE_KEY)
    pictures_out = os.path.join(args.config, "mantel_pictures")
    data, copies = convert(args.source, args.pictures, pictures_out)
    threads = sum(1 for p in data["posts"] if not p.get("reply_to"))
    print(f"{len(data['posts'])} posts in {threads} threads, {len(data['profiles'])} profiles, "
          f"{sum(len(v) for v in data['inbox'].values())} inbox entries, {len(copies)} pictures")
    if args.dry_run:
        return 0
    if os.path.exists(out) and not args.force:
        print(f"{out} already exists; pass --force to replace it", file=sys.stderr)
        return 1
    os.makedirs(pictures_out, exist_ok=True)
    for src, dst in copies:
        shutil.copyfile(src, dst)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "minor_version": 1, "key": STORE_KEY, "data": data}, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

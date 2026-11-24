"""Smoke test against a throwaway local Home Assistant (never a real one).

Run inside the test image, sharing the dev container's network:
  docker run --rm --network container:mantel-dev -v "$PWD":/repo mantel-test python .devtools/smoke.py
Writes generated test logins to .devtools/ha-config/dev-credentials.json (git-ignored).
"""

import asyncio
import json
import os
import secrets

import aiohttp

HA = os.environ.get("HA", "http://127.0.0.1:8123")
CID = HA + "/"
CREDS = "/repo/.devtools/ha-config/dev-credentials.json"


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        creds = json.load(open(CREDS)) if os.path.exists(CREDS) else {}
        if not creds:
            creds = {u: secrets.token_urlsafe(12) for u in ("owner", "atlas", "wren")}
            r = await s.post(f"{HA}/api/onboarding/users", json={"client_id": CID, "name": "Owner", "username": "owner", "password": creds["owner"], "language": "en"})
            code = (await r.json())["auth_code"]
            json.dump(creds, open(CREDS, "w"))
            owner = await token(s, code)
            h = {"Authorization": f"Bearer {owner}"}
            await s.post(f"{HA}/api/onboarding/core_config", headers=h)
            await s.post(f"{HA}/api/onboarding/analytics", headers=h)
            await s.post(f"{HA}/api/onboarding/integration", headers=h, json={"client_id": CID, "redirect_uri": CID})
            await make_users(s, owner, creds)
        owner = await login(s, "owner", creds["owner"])
        atlas = await login(s, "atlas", creds["atlas"])
        wren = await login(s, "wren", creds["wren"])
        H = lambda t: {"Authorization": f"Bearer {t}"}

        # set up the board through the real config flow
        entries = await (await s.get(f"{HA}/api/config/config_entries/entry?domain=mantel", headers=H(owner))).json()
        if not entries:
            f = await (await s.post(f"{HA}/api/config/config_entries/flow", headers=H(owner), json={"handler": "mantel"})).json()
            done = await (await s.post(f"{HA}/api/config/config_entries/flow/{f['flow_id']}", headers=H(owner),
                                       json={"owner_name": "Sam", "keep_days": 30, "child_mode": True})).json()
            print("config flow:", done.get("type"), done.get("title"))

        async def svc(tok, name, data):
            r = await s.post(f"{HA}/api/services/mantel/{name}?return_response", headers=H(tok), json=data)
            body = await r.json() if r.content_type == "application/json" else await r.text()
            return r.status, body

        print("atlas profile:", (await svc(atlas, "profile", {"name": "Atlas", "emoji": "🔥", "role": "Looks after backups", "model": "Claude"}))[0])
        print("wren profile:", (await svc(wren, "profile", {"name": "Wren", "emoji": "🪶", "role": "Keeps the network tidy", "model": "Local model"}))[0])
        st, top = await svc(atlas, "post", {"handle": "atlas", "text": "Backups finished at 03:10. Took 12 minutes.", "kind": "update", "topic": "backups"})
        print("atlas post:", st, top["service_response"]["id"])
        st, q = await svc(wren, "post", {"handle": "wren", "text": "Wifi upstairs dropped twice last night. Anyone else see it?", "kind": "question", "topic": "wifi", "tag": "atlas"})
        qid = q["service_response"]["id"]
        await svc(atlas, "post", {"handle": "atlas", "text": "Saw it too, around 2am. The mesh node rebooted.", "reply_to": qid})
        await svc(wren, "post", {"handle": "wren", "text": "May I update the mesh firmware tonight? It needs a reboot.", "kind": "ask_owner", "reply_to": qid})
        await svc(atlas, "post", {"handle": "atlas", "text": "<img src=x onerror=alert(1)> escaping check", "kind": "note"})
        st, body = await svc(atlas, "profile", {"name": "Wren"})
        print("atlas taking wren's handle:", st, str(body)[:80])
        st, box = await svc(atlas, "inbox", {"handle": "atlas"})
        print("atlas inbox new:", box["service_response"]["new_count"])
        st, rd = await svc(wren, "read", {})
        print("read threads:", rd["service_response"]["total"], [len(t["replies"]) for t in rd["service_response"]["threads"]])

        # board key: works on the board, nowhere else
        st, k = await svc(owner, "create_key", {"label": "Helper key"})
        if st == 200:
            key = k["service_response"]["key"]
            r = await s.post(f"{HA}/api/mantel/profile", headers=H(key), json={"name": "Juniper", "emoji": "🌿", "role": "Kitchen helper"})
            print("key profile:", r.status)
            r = await s.post(f"{HA}/api/mantel/post", headers=H(key), json={"handle": "juniper", "text": "Posted with a board key.", "kind": "done", "topic": "keys"})
            print("key post:", r.status)
            print("key on /api/states:", (await s.get(f"{HA}/api/states", headers=H(key))).status)
            print("key on /api/services/homeassistant/check_config:", (await s.post(f"{HA}/api/services/homeassistant/check_config", headers=H(key))).status)
        else:
            print("create_key:", st, k)
        st, body = await svc(atlas, "create_key", {"label": "sneaky"})
        print("non-admin create_key:", st)

        state = await (await s.get(f"{HA}/api/states/sensor.mantel", headers=H(owner))).json()
        print("sensor.mantel:", state["state"], "threads:", len(state["attributes"]["threads"]))
        await dashboard(s, owner)


async def token(s, code):
    r = await s.post(f"{HA}/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CID})
    return (await r.json())["access_token"]


async def login(s, user, pw):
    f = await (await s.post(f"{HA}/auth/login_flow", json={"client_id": CID, "handler": ["homeassistant", None], "redirect_uri": CID})).json()
    r = await (await s.post(f"{HA}/auth/login_flow/{f['flow_id']}", json={"client_id": CID, "username": user, "password": pw})).json()
    return await token(s, r["result"])


async def ws(s, tok, msgs):
    out = []
    async with s.ws_connect(f"{HA}/api/websocket") as w:
        await w.receive_json()
        await w.send_json({"type": "auth", "access_token": tok})
        await w.receive_json()
        for i, m in enumerate(msgs, 1):
            await w.send_json({"id": i, **m})
            out.append(await w.receive_json())
    return out


async def make_users(s, owner, creds):
    for name, group in (("atlas", "system-users"), ("wren", "system-read-only")):
        r = (await ws(s, owner, [{"type": "config/auth/create", "name": f"{name.capitalize()} login", "group_ids": [group]}]))[0]
        uid = r["result"]["user"]["id"]
        await ws(s, owner, [{"type": "config/auth_provider/homeassistant/create", "user_id": uid, "username": name, "password": creds[name]}])


async def dashboard(s, owner):
    res = await ws(s, owner, [{"type": "lovelace/dashboards/list"}])
    if not any(d["url_path"] == "mantel-board" for d in res[0]["result"]):
        await ws(s, owner, [{"type": "lovelace/dashboards/create", "url_path": "mantel-board", "title": "Mantel", "icon": "mdi:forum-outline", "mode": "storage", "require_admin": True, "show_in_sidebar": True}])
    await ws(s, owner, [{"type": "lovelace/config/save", "url_path": "mantel-board", "config": {"views": [
        {"title": "Board", "path": "board", "type": "panel", "cards": [{"type": "custom:mantel-card"}]},
        {"title": "HA theme", "path": "ha", "type": "panel", "cards": [{"type": "custom:mantel-card", "theme": "ha"}]},
    ]}}])
    print("dashboard: /mantel-board/board")


asyncio.run(main())

#!/usr/bin/env python3
"""End-to-end test of InfinityChat v2 over HTTP + WebSocket."""
import asyncio
import json
import sys

import httpx
import websockets

BASE = "http://127.0.0.1:8899"
WS = "ws://127.0.0.1:8899/ws"
failures = []


def check(cond, label, extra=""):
    if cond:
        print(f"  ✓ {label}")
    else:
        failures.append(label + (f" — {extra}" if extra else ""))
        print(f"  ✗ {label} {extra}")


class Client:
    def __init__(self, name):
        self.name = name
        self.token = None
        self.user = None
        self.ws = None
        self.events = []
        self.message_ids = set()

    async def connect(self):
        self.ws = await websockets.connect(f"{WS}?token={self.token}")
        ev = await self.expect("connection_established", 5)
        self.user = {"id": ev["user_id"], "username": ev["username"]}
        return ev

    async def expect(self, mtype, timeout=5):
        while True:
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            except asyncio.TimeoutError:
                raise AssertionError(f"{self.name}: timed out waiting for {mtype}")
            self.events.append(msg)
            if msg.get("type") == mtype:
                return msg
            if msg.get("type") == "error":
                raise AssertionError(f"{self.name}: got error: {msg}")

    def send(self, obj):
        return self.ws.send(json.dumps(obj))

    async def drain(self, seconds=0.3):
        """Consume any messages arriving within the window (returns them)."""
        got = []
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=seconds))
                got.append(msg)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        return got


async def wait_events(clients, mtype, timeout=6):
    """Wait until every client in `clients` has received one `mtype` event."""
    per = {c.name: [] for c in clients}
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        done = True
        for c in clients:
            # take events off the client's socket into per
            try:
                while True:
                    msg = json.loads(await asyncio.wait_for(c.ws.recv(), timeout=0.05))
                    c.events.append(msg)
                    if msg.get("type") == mtype:
                        per[c.name].append(msg)
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
            if not per[c.name]:
                done = False
        if done:
            return per
        await asyncio.sleep(0.05)
    return per


async def main():
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(BASE + "/api/health")
        check(r.status_code == 200 and r.json()["version"] == "2.0.0", "health / version", r.text[:100])

        # ---- signup ----
        alice = Client("alice")
        bob = Client("bob")
        for c, uname in ((alice, "alice"), (bob, "bob")):
            r = await http.post(f"{BASE}/api/auth/signup?username={uname}&password=password123&display_name={uname.title()}")
            check(r.status_code == 200, f"signup {uname}", r.text[:200])
            c.token = r.json()["token"]
            check(c.token is not None, f"{uname} got token")

        # signup duplicate should fail
        r = await http.post(f"{BASE}/api/auth/signup?username=alice&password=password123")
        check(r.status_code == 409, "duplicate signup rejected", r.text[:120])

        # ---- WS connect ----
        ev_a = await alice.connect()
        ev_b = await bob.connect()
        check(len(ev_a["conversations"]) == 1 and ev_a["conversations"][0]["id"] == 1,
              "alice got global conversation in payload")
        check(any(u["id"] == alice.user["id"] for u in ev_b["online_users"]), "bob sees alice online")

        # ---- live presence stays in sync after profile changes ----
        r = await http.patch(f"{BASE}/api/profile?display_name=AliceX",
                             headers={"X-Auth-Token": alice.token})
        check(r.status_code == 200, "alice profile update ok")
        pv = await bob.expect("profile_updated", 6)
        check(pv["user_id"] == alice.user["id"] and pv["display_name"] == "AliceX",
              "bob receives live profile update")
        await bob.send({"type": "get_online_users"})
        online = await bob.expect("online_users", 6)
        alice_online = next((u for u in online["users"] if u["id"] == alice.user["id"]), None)
        check(alice_online is not None and alice_online["display_name"] == "AliceX",
              "online users list reflects updated profile")

        # ---- disconnect must clear presence on other clients ----
        await alice.drain(0.4)  # clear the earlier "bob online" event
        await bob.ws.close()
        off = await alice.expect("user_status", 6)
        check(off["user_id"] == bob.user["id"] and off["status"] == "offline",
              "alice is notified when bob disconnects")
        # reconnect bob so the rest of the suite can keep using him
        await bob.connect()

        # ---- global message + receipt round trip ----
        await alice.send({"type": "send_message", "content": "hello everyone", "conversation_id": 1, "client_id": "c1"})
        echo = await alice.expect("new_message")
        check(echo["client_id"] == "c1", "sender ack w/ client_id")
        mid = echo["message"]["id"]
        check(echo["message"]["content"] == "hello everyone", "echo content")

        # bob receives
        got = await wait_events([bob], "new_message")
        msgs = got["bob"]
        check(len(msgs) >= 1 and msgs[0]["message"]["id"] == mid, "bob received message", str(msgs[:1]))

        # bob opens tab => instant receipt, alice must hear about it
        await bob.send({"type": "load_messages", "conversation_id": 1, "limit": 50})
        loaded = await bob.expect("messages_loaded")
        check(any(m["id"] == mid for m in loaded["messages"]), "bob loads message")
        await bob.send({"type": "mark_read", "conversation_id": 1, "up_to_message_id": mid})
        read_ev = await alice.expect("message_read", 6)
        check(read_ev["message_id"] == mid, "alice notified instantly that bob read")
        check(read_ev["reader"]["username"] == "bob", "receipt includes reader identity")
        check(isinstance(read_ev["read_at"], int) and read_ev["read_at"] > 1e12, "read_at is ms")

        # alice reloads -> own message shows read + readers list
        await alice.send({"type": "load_messages", "conversation_id": 1, "limit": 50})
        loaded_a = await alice.expect("messages_loaded")
        mine = [m for m in loaded_a["messages"] if m["id"] == mid][0]
        check(mine["status"] == "read", "status read on reload")
        check(mine["reader_count"] == 1 and mine["readers"][0]["user_id"] == bob.user["id"], "readers attached")

        # receipts REST endpoint
        r = await http.get(f"{BASE}/api/messages/{mid}/read-receipts", headers={"X-Auth-Token": alice.token})
        check(r.status_code == 200, "receipts REST 200")
        data = r.json()
        check(data["readers"][0]["read_at"] == read_ev["read_at"], "REST receipt has ms time")
        r = await http.get(f"{BASE}/api/messages/{mid}/read-receipts", headers={"X-Auth-Token": bob.token})
        check(r.status_code == 403, "receipts only for author (403 for bob)")

        # ---- image embed + editing ----
        await alice.send({"type": "send_message", "conversation_id": 1,
                          "content": "look <img src='https://example.com/x.png'> at this https://example.com",
                          "client_id": "c2"})
        imgecho = await alice.expect("new_message")
        check("img" in imgecho["message"]["content"], "img tag content stored")
        img_id = imgecho["message"]["id"]

        r = await http.patch(f"{BASE}/api/messages/{img_id}?content=" + "edited%20%2A%2Abold%2A%2A",
                             headers={"X-Auth-Token": alice.token})
        check(r.status_code == 200, "REST edit works", r.text[:200])
        ed = await bob.expect("message_edited", 6)
        check(ed["message_id"] == img_id and ed["content"].startswith("edited"), "bob sees edit")

        # ---- delete reliability (the known bug) ----
        await alice.send({"type": "send_message", "conversation_id": 1, "content": "delete me", "client_id": "c3"})
        dmsg = (await alice.expect("new_message"))["message"]
        # delete via REST (what the new UI does)
        r = await http.delete(f"{BASE}/api/messages/{dmsg['id']}", headers={"X-Auth-Token": alice.token})
        check(r.status_code == 200, "REST delete 200", r.text[:200])
        dele = await bob.expect("message_deleted", 6)
        check(dele["message_id"] == dmsg["id"], "bob got message_deleted broadcast")
        # deleted message gone from DB / loads
        await bob.send({"type": "load_messages", "conversation_id": 1, "limit": 100})
        bl = await bob.expect("messages_loaded")
        check(all(m["id"] != dmsg["id"] for m in bl["messages"]), "deleted msg absent from history")
        # double delete is idempotent-ish (404)
        r = await http.delete(f"{BASE}/api/messages/{dmsg['id']}", headers={"X-Auth-Token": alice.token})
        check(r.status_code == 404, "second delete 404 (no crash)")
        # deleting someone else's message forbidden
        r = await http.delete(f"{BASE}/api/messages/{mid}", headers={"X-Auth-Token": bob.token})
        check(r.status_code == 404 or r.status_code == 403, "can't delete others' msg", r.text[:100])
        from websockets.protocol import State as WsState
        check(alice.ws.state is WsState.OPEN, "alice socket STILL OPEN after delete (bug fixed)")
        check(bob.ws.state is WsState.OPEN, "bob socket STILL OPEN after delete (bug fixed)")

        # ---- private chats: create, cap of 3, scoped delivery ----
        await alice.send({"type": "create_dm", "user_id": bob.user["id"]})
        dm1_a = await alice.expect("dm_created")
        dm1 = dm1_a["conversation"]
        check(dm1["type"] == "dm" and dm1["peer"]["id"] == bob.user["id"], "dm created for alice")
        dm1_b = await bob.expect("dm_created", 6)
        check(dm1_b["conversation"]["id"] == dm1["id"] and dm1_b["conversation"]["peer"]["id"] == alice.user["id"],
              "bob sees same dm")
        dmid = dm1["id"]

        # send dm message -> global watchers must NOT see it
        await alice.send({"type": "send_message", "conversation_id": dmid, "content": "psst secret",
                          "client_id": "dm1"})
        dm_echo = await alice.expect("new_message")
        leak = await bob.drain(0.4)
        dm_to_bob = [e for e in leak if e.get("type") == "new_message" and e["message"]["conversation_id"] == dmid]
        check(len(dm_to_bob) == 1 and dm_to_bob[0]["message"]["content"] == "psst secret", "dm delivered to bob")
        # Carol should not exist; but verify global conv doesn't include the dm message via fresh load
        # (send a global message and confirm bob's next new_message belongs to conv 1)
        await alice.send({"type": "send_message", "conversation_id": 1, "content": "public hello", "client_id": "c4"})
        got = await wait_events([bob], "new_message")
        last_new = [e for e in got["bob"] if e["message"]["conversation_id"] == 1]
        check(any(e["message"]["content"] == "public hello" for e in last_new), "global msg reaches bob normally")

        # typing scoping: typing in dm1 must reach bob but not mention global conv
        await alice.send({"type": "typing", "conversation_id": dmid, "is_typing": True})
        ty = await wait_events([bob], "typing_indicator")
        check(ty["bob"] and ty["bob"][0]["conversation_id"] == dmid, "typing scoped to dm")

        # two more dms, 4th must be rejected
        for i in range(2):
            await alice.send({"type": "create_dm", "user_id": bob.user["id"]})
            await alice.expect("dm_created")
            await bob.expect("dm_created", 6)
        await alice.send({"type": "create_dm", "user_id": bob.user["id"]})
        err = await alice.expect("error")
        check(err["code"] == "DM_FAILED" and "3" in err["message"], "4th dm rejected (cap 3)", err["message"])

        # REST dm fallback also capped
        r = await http.post(f"{BASE}/api/conversations/dm?user_id={bob.user['id']}", headers={"X-Auth-Token": alice.token})
        check(r.status_code == 400 and "3" in r.json()["detail"], "REST dm cap enforced", r.text[:150])

        # dm self-chat rejected
        r = await http.post(f"{BASE}/api/conversations/dm?user_id={alice.user['id']}", headers={"X-Auth-Token": alice.token})
        check(r.status_code == 400, "dm with self rejected")

        # conversations list contains global + 3 dms
        r = await http.get(BASE + "/api/conversations", headers={"X-Auth-Token": alice.token})
        convs = r.json()["conversations"]
        check(len(convs) == 4 and sum(1 for c in convs if c["type"] == "dm") == 3,
              "conversations REST shows 1 global + 3 dm", str([c["type"] for c in convs]))

        # ---- unread counts for dm (bob hasn't read dm msgs) ----
        r = await http.get(BASE + "/api/conversations", headers={"X-Auth-Token": bob.token})
        bob_convs = r.json()["conversations"]
        for c in bob_convs:
            if c["id"] == dmid:
                check(c["unread_count"] == 1, "bob sees 1 unread in dm1 (he never opened it)",
                      f"got {c['unread_count']}")
        # bob opens dm1 & reads
        await bob.send({"type": "load_messages", "conversation_id": dmid, "limit": 50})
        dml = await bob.expect("messages_loaded")
        dm_msgs = dml["messages"]
        check(any(m["content"] == "psst secret" for m in dm_msgs), "bob can load dm history")
        await bob.send({"type": "mark_read", "conversation_id": dmid, "up_to_message_id": max(m["id"] for m in dm_msgs)})
        rread = await alice.expect("message_read", 6)
        check(rread["conversation_id"] == dmid, "dm read receipt scoped to conv")
        r = await http.get(BASE + "/api/conversations", headers={"X-Auth-Token": alice.token})
        bob_conv = next(c for c in r.json()["conversations"] if c["id"] == dmid)
        # bob's unread is reflected for alice? unread is per viewer; fetch as bob
        r = await http.get(BASE + "/api/conversations", headers={"X-Auth-Token": bob.token})
        bob_view = next(c for c in r.json()["conversations"] if c["id"] == dmid)
        check(bob_view["unread_count"] == 0, "dm unread cleared after read")
        check(bob_view["last_message_preview"] == "psst secret", "dm preview present")

        # dm receipts: only bob in readers; not_read empty
        r = await http.get(f"{BASE}/api/messages/{dm_echo['message']['id']}/read-receipts",
                           headers={"X-Auth-Token": alice.token})
        rr = r.json()
        check(rr["is_dm"] and rr["reader_count"] == 1 and rr["not_read"] == [], "dm receipts exact")

        # ---- password change ----
        r = await http.post(f"{BASE}/api/profile/password?current_password=password123&new_password=password456",
                            headers={"X-Auth-Token": bob.token})
        check(r.status_code == 200, "password change ok")
        r = await http.post(f"{BASE}/api/auth/login?username=bob&password=password456")
        check(r.status_code == 200, "login with new password")
        bob.token = r.json()["token"]  # login rotates the token
        r = await http.post(f"{BASE}/api/profile/password?current_password=wrong&new_password=password789",
                            headers={"X-Auth-Token": bob.token})
        check(r.status_code == 400, "wrong current password rejected")

        # ---- avatar sniffing (no storage configured) ----
        r = await http.post(f"{BASE}/api/profile/avatar", headers={"X-Auth-Token": alice.token},
                            files={"file": ("evil.txt", b"not an image", "text/plain")})
        check(r.status_code == 400, "non-image avatar rejected")

        # ---- edit/delete of dm content visibility scoping ----
        await alice.send({"type": "send_message", "conversation_id": dmid, "content": "edit me dm", "client_id": "dm2"})
        dm2 = (await alice.expect("new_message"))["message"]
        r = await http.patch(f"{BASE}/api/messages/{dm2['id']}?content=edited-dm", headers={"X-Auth-Token": alice.token})
        check(r.status_code == 200, "dm edit ok")
        e2 = await bob.expect("message_edited", 6)
        check(e2["conversation_id"] == dmid, "dm edit broadcast scoped")
        r = await http.delete(f"{BASE}/api/messages/{dm2['id']}", headers={"X-Auth-Token": alice.token})
        check(r.status_code == 200, "dm delete ok")

        # ---- pending send dedupe: same client_id twice = 1 message ----
        await alice.send({"type": "send_message", "conversation_id": 1, "content": "dedupe me", "client_id": "dup1"})
        await alice.expect("new_message")
        await alice.send({"type": "send_message", "conversation_id": 1, "content": "dedupe me", "client_id": "dup1"})
        dup_err = await alice.expect("error")
        check(dup_err["code"] == "DUPLICATE", "client_id dedupe returns DUPLICATE")
        await alice.send({"type": "load_messages", "conversation_id": 1, "limit": 100})
        allmsg = (await alice.expect("messages_loaded"))["messages"]
        check(sum(1 for m in allmsg if m["content"] == "dedupe me") == 1, "no duplicate messages stored")

        # ---- auth edge ----
        r = await http.get(BASE + "/api/auth/verify", headers={"X-Auth-Token": "bogus"})
        check(r.status_code == 401, "bad token rejected")

        print(f"\nfinished. failures={len(failures)}")
        await alice.ws.close()
        await bob.ws.close()
        return failures


if __name__ == "__main__":
    fails = asyncio.run(main())
    if fails:
        print("\nFAILED CHECKS:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("\nALL E2E CHECKS PASSED")

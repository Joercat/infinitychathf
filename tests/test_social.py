#!/usr/bin/env python3
"""Focused end-to-end checks for the social media feature set.

Runs against a live server on 127.0.0.1:8899. Start it with a fresh writable
DATABASE_URL before running (see README/test harness examples).
"""
import asyncio
import json
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8899"
failures = []


def check(cond, label, extra=""):
    if cond:
        print(f"  \u2713 {label}")
    else:
        failures.append(label + (f" \u2014 {extra}" if extra else ""))
        print(f"  \u2717 {label} {extra}")


async def signup(client, username):
    r = await client.post(f"{BASE}/api/auth/signup", params={
        "username": username,
        "password": "password123",
        "display_name": username.title(),
    })
    return r


async def upload_file(client, token, name, data, mime):
    params = {
        "chunk_index": 0,
        "total_chunks": 1,
        "file_name": name,
        "file_type": mime,
        "file_size": len(data),
        "upload_id": uuid.uuid4().hex,
    }
    r = await client.post(
        f"{BASE}/api/upload/chunk",
        params=params,
        files={"file": (name, data, mime)},
        headers={"X-Auth-Token": token},
    )
    if r.status_code != 200:
        return None, r
    return r.json()["file_path"], r


async def main():
    async with httpx.AsyncClient(timeout=20) as http:
        # Unique usernames so the test can be run repeatedly.
        u1 = "soc_" + uuid.uuid4().hex[:8]
        u2 = "soc_" + uuid.uuid4().hex[:8]

        r = await signup(http, u1)
        check(r.status_code == 200 and r.json().get("token"), f"signup {u1}", r.text[:160])
        t1 = r.json()["token"]
        h1 = {"X-Auth-Token": t1}

        r = await signup(http, u2)
        check(r.status_code == 200 and r.json().get("token"), f"signup {u2}", r.text[:160])
        t2 = r.json()["token"]
        h2 = {"X-Auth-Token": t2}

        # --- image upload through the shared chunked endpoint + post create ---
        # Minimal 1x1 PNG.
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
            b"\x00\x00\x00\x03\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        fp, up = await upload_file(http, t1, "pic.png", png, "image/png")
        check(up.status_code == 200 and fp, "chunked image upload returned a path", up.text[:160])

        media = [{"file_path": fp, "file_name": "pic.png", "file_type": "image/png", "file_size": len(png)}]
        r = await http.post(
            f"{BASE}/api/social/posts",
            params={"body": "Hello social world", "media_json": json.dumps(media)},
            headers=h1,
        )
        check(r.status_code == 200 and len(r.json().get("post", {}).get("media", [])) == 1,
              "post with image created", r.text[:160])
        post_id = r.json()["post"]["id"]

        # --- text attachments are allowed by the shared uploader but rejected on posts ---
        txt = b"just some notes"
        tfp, up = await upload_file(http, t1, "notes.txt", txt, "text/plain")
        check(up.status_code == 200 and tfp, "chunked text upload still works for chat", up.text[:160])
        bad_media = [{"file_path": tfp, "file_name": "notes.txt", "file_type": "text/plain", "file_size": len(txt)}]
        r = await http.post(
            f"{BASE}/api/social/posts",
            params={"body": "should fail", "media_json": json.dumps(bad_media)},
            headers=h1,
        )
        check(r.status_code == 400 and "images and videos" in (r.json().get("detail") or ""),
              "text media rejected on social posts", r.text[:160])

        # --- edit own post ---
        r = await http.patch(f"{BASE}/api/social/posts/{post_id}", params={"body": "Edited body"}, headers=h1)
        check(r.status_code == 200 and r.json()["post"]["body"] == "Edited body"
              and r.json()["post"].get("edited_at_ms"),
              "author can edit own post", r.text[:160])

        # --- non-author cannot edit ---
        r = await http.patch(f"{BASE}/api/social/posts/{post_id}", params={"body": "nope"}, headers=h2)
        check(r.status_code == 403, "non-author cannot edit", r.text[:160])

        # --- like / repost / bookmark toggles ---
        r = await http.post(f"{BASE}/api/social/posts/{post_id}/like", headers=h2)
        check(r.status_code == 200 and r.json()["liked"] and r.json()["like_count"] == 1,
              "second user can like", r.text[:160])
        r = await http.post(f"{BASE}/api/social/posts/{post_id}/repost", headers=h2)
        check(r.status_code == 200 and r.json()["reposted"], "second user can repost", r.text[:160])
        r = await http.get(f"{BASE}/api/social/feed", params={"feed": "home", "limit": 50}, headers=h2)
        repost = next((p for p in (r.json().get("posts") or [])
                       if p["id"] == post_id and p.get("reposter_id")), None)
        check(repost is not None and repost.get("reposter", {}).get("username") == u2,
              "repost carries reposter info", r.text[:160])
        r = await http.post(f"{BASE}/api/social/posts/{post_id}/bookmark", headers=h2)
        check(r.status_code == 200 and r.json()["bookmarked"], "second user can bookmark", r.text[:160])

        # --- search shows follow state truthfully ---
        await http.post(f"{BASE}/api/social/users/{u1}/follow", headers=h2)
        r = await http.get(f"{BASE}/api/social/search", params={"q": u1}, headers=h2)
        hit = next((u for u in (r.json().get("users") or []) if u["username"] == u1), None)
        check(r.status_code == 200 and hit and hit["is_following"],
              "search reflects follow status", r.text[:160])

        # --- delete own post ---
        r = await http.delete(f"{BASE}/api/social/posts/{post_id}", headers=h1)
        check(r.status_code == 200, "author can delete post", r.text[:160])
        r = await http.get(f"{BASE}/api/social/posts/{post_id}", headers=h1)
        check(r.status_code == 404, "deleted post returns 404", r.text[:160])

        # --- backup admin endpoints are reachable ---
        r = await http.get(f"{BASE}/api/admin/backups", headers=h1)
        check(r.status_code == 200, "backup list endpoint reachable", r.text[:160])

    print(f"\nfinished. failures={len(failures)}")
    print("ALL SOCIAL CHECKS PASSED" if not failures else "SOCIAL CHECKS FAILED")
    return failures


if __name__ == "__main__":
    fails = asyncio.run(main())
    if fails:
        for f in fails:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)

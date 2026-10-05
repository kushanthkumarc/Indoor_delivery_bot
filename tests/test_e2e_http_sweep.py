"""Comprehensive end-to-end HTTP endpoint sweep for the Indoor Delivery Robot Backend."""
import asyncio
import json
import sys
import uuid

import aiomysql
import httpx

BASE = "http://127.0.0.1:8000"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "change-me-admin-password"
USER_EMAIL = "testuser@example.com"
USER_PASSWORD = "TestUser!234"

results: list[tuple[str, str, str, str, str]] = []  # (module, name, expected, actual, status+detail)


def record(module: str, name: str, expected: str, actual, detail: str = "") -> bool:
    actual_str = str(actual)
    if isinstance(actual, int) and isinstance(expected, str):
        ok = actual == int(expected.split("|")[0].strip())
    else:
        ok = str(expected).split("|")[0].strip() == actual_str
    mark = "PASS" if ok else "FAIL"
    full = mark + (" " + detail if detail else "")
    results.append((module, name, expected, actual_str, full))
    return ok


async def ensure_robot() -> str:
    conn = await aiomysql.connect(host="127.0.0.1", port=3306, user="root", password="", db="robot_db")
    cur = await conn.cursor()
    await cur.execute("SELECT COUNT(*) FROM robots")
    (n,) = await cur.fetchone()
    if n == 0:
        rid = str(uuid.uuid4())
        await cur.execute(
            "INSERT INTO robots (id, name, api_key_hash, status, control_mode, safety_state, slam_state, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
            (rid, "Test Robot", "fakehash", "OFFLINE", "STOPPED", "SAFE", "IDLE"),
        )
        await conn.commit()
    else:
        await cur.execute("SELECT id FROM robots LIMIT 1")
        (rid,) = await cur.fetchone()
    await cur.close()
    conn.close()
    return rid


async def set_robot_mode(robot_id: str, status: str = "ONLINE", mode: str = "STOPPED") -> None:
    conn = await aiomysql.connect(host="127.0.0.1", port=3306, user="root", password="", db="robot_db")
    cur = await conn.cursor()
    await cur.execute("UPDATE robots SET status=%s, control_mode=%s WHERE id=%s", (status, mode, robot_id))
    await conn.commit()
    await cur.close()
    conn.close()


def H(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as c:
        # Health
        r = await c.get("/health")
        record("health", "GET /health", "200", r.status_code, "body=" + r.text[:80])
        r = await c.get("/openapi.json")
        record("health", "GET /openapi.json", "200", r.status_code, "paths=" + str(len(r.json().get("paths", {}))) if r.status_code == 200 else r.text[:80])

        # Auth — login admin
        r = await c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        record("auth", "POST /auth/login (admin)", "200", r.status_code, r.text[:120])
        if r.status_code != 200:
            return _summary()
        admin_access = r.json()["access_token"]
        admin_refresh = r.cookies.get("refresh_token")
        record("auth", "refresh cookie set", "set", "set" if admin_refresh else "missing")

        cookie_header = next((ck for ck in r.headers.get_list("set-cookie") if "refresh_token" in ck), "")
        record("auth", "cookie HttpOnly+Secure+SameSite", "yes", "yes" if all(x in cookie_header for x in ["HttpOnly", "Secure", "SameSite=strict"]) else "no", cookie_header[:120])

        # /auth/me
        r = await c.get("/auth/me", headers=H(admin_access))
        record("auth", "GET /auth/me (admin)", "200", r.status_code, "role=" + r.json().get("role", "?") if r.status_code == 200 else r.text[:80])

        # /auth/me no token
        async with httpx.AsyncClient(base_url=BASE, timeout=5.0) as c2:
            r = await c2.get("/auth/me")
            record("auth", "GET /auth/me (no token) -> 401", "401", r.status_code)

        # bad password
        r = await c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        record("auth", "POST /auth/login bad password -> 401", "401", r.status_code, str(r.json().get("detail", "")))

        # unknown email
        r = await c.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})
        record("auth", "POST /auth/login unknown email -> 401", "401", r.status_code)

        # /auth/refresh
        async with httpx.AsyncClient(base_url=BASE, timeout=5.0) as cr:
            cr.cookies.set("refresh_token", admin_refresh, domain="127.0.0.1")
            r = await cr.post("/auth/refresh")
            record("auth", "POST /auth/refresh", "200", r.status_code, "got new access_token" if r.status_code == 200 else r.text[:80])

        # Admin — list
        r = await c.get("/admin/users", headers=H(admin_access))
        record("admin", "GET /admin/users", "200", r.status_code, f"count={len(r.json())}")
        users = r.json()
        existing_emails = [u["email"] for u in users]
        admin_id_self = next((u["id"] for u in users if u["email"] == ADMIN_EMAIL), None)

        # create USER
        if USER_EMAIL not in existing_emails:
            r = await c.post("/admin/users", json={"email": USER_EMAIL, "password": USER_PASSWORD, "name": "Test User", "role": "USER"}, headers=H(admin_access))
            record("admin", "POST /admin/users (create USER)", "201", r.status_code, "id=" + r.json().get("id", "?")[:8] if r.status_code == 201 else r.text[:120])
        else:
            record("admin", "POST /admin/users (create USER, existed)", "skip", "skip")

        # duplicate
        r = await c.post("/admin/users", json={"email": USER_EMAIL, "password": USER_PASSWORD, "name": "X", "role": "USER"}, headers=H(admin_access))
        record("admin", "POST /admin/users (duplicate) -> 409", "409", r.status_code, str(r.json().get("detail", "")))

        # USER login
        r = await c.post("/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD})
        record("auth", "POST /auth/login (USER)", "200", r.status_code, r.text[:120])
        user_access = r.json().get("access_token") if r.status_code == 200 else None

        # USER accessing admin route
        if user_access:
            r = await c.get("/admin/users", headers=H(user_access))
            record("admin", "GET /admin/users (as USER) -> 403", "403", r.status_code, str(r.json().get("detail", "")))

        # patch / delete
        r = await c.get("/admin/users", headers=H(admin_access))
        users = r.json()
        user_id = next((u["id"] for u in users if u["email"] == USER_EMAIL), None)
        if user_id:
            r = await c.patch(f"/admin/users/{user_id}", json={"role": "ADMIN"}, headers=H(admin_access))
            record("admin", "PATCH promote USER->ADMIN", "200", r.status_code)
            r = await c.patch(f"/admin/users/{user_id}", json={"role": "USER"}, headers=H(admin_access))
            record("admin", "PATCH demote ADMIN->USER", "200", r.status_code)
            r = await c.patch(f"/admin/users/{user_id}", json={"is_active": False}, headers=H(admin_access))
            record("admin", "PATCH deactivate user", "200", r.status_code)
            r = await c.post("/auth/login", json={"email": USER_EMAIL, "password": USER_PASSWORD})
            record("admin", "login deactivated user -> 403", "403", r.status_code, str(r.json().get("detail", "")))
            r = await c.patch(f"/admin/users/{user_id}", json={"is_active": True}, headers=H(admin_access))
            record("admin", "PATCH reactivate user", "200", r.status_code)

        # self delete
        if admin_id_self:
            r = await c.delete(f"/admin/users/{admin_id_self}", headers=H(admin_access))
            record("admin", "DELETE self -> 400", "400", r.status_code, str(r.json().get("detail", "")))

        # Events
        r = await c.get("/events", headers=H(admin_access))
        record("events", "GET /events (admin)", "200", r.status_code, f"total={r.json().get('total')}")
        async with httpx.AsyncClient(base_url=BASE, timeout=5.0) as c2:
            r = await c2.get("/events")
            record("events", "GET /events (no token) -> 401", "401", r.status_code)
        if user_access:
            r = await c.get("/events", headers=H(user_access))
            record("events", "GET /events (USER) -> 403", "403", r.status_code)
        r = await c.get("/events?severity=INFO", headers=H(admin_access))
        record("events", "GET /events?severity=INFO", "200", r.status_code)
        r = await c.get("/events?type=auth.login", headers=H(admin_access))
        record("events", "GET /events?type=auth.login", "200", r.status_code, f"items={len(r.json().get('items', []))}")
        r = await c.get("/events?page=1&page_size=3", headers=H(admin_access))
        record("events", "GET /events pagination", "200", r.status_code, f"items={len(r.json().get('items', []))}")

        # Safety — needs robot row to take effect
        robot_id = await ensure_robot()

        r = await c.get("/robot/safety-state", headers=H(admin_access))
        record("safety", "GET /robot/safety-state", "200", r.status_code, json.dumps(r.json()) if r.status_code == 200 else r.text[:80])
        r = await c.post("/robot/mode", json={"mode": "AUTONOMOUS", "acknowledgment": False}, headers=H(admin_access))
        record("safety", "POST /robot/mode (ack=false) -> 422", "422", r.status_code, str(r.json().get("detail", "")))
        r = await c.post("/robot/mode", json={"mode": "AUTONOMOUS", "acknowledgment": True}, headers=H(admin_access))
        record("safety", "POST /robot/mode AUTONOMOUS ack=true", "200", r.status_code)
        r = await c.post("/robot/mode", json={"mode": "MANUAL", "acknowledgment": True}, headers=H(admin_access))
        record("safety", "POST /robot/mode MANUAL ack=true", "200", r.status_code)
        r = await c.post("/robot/emergency-stop", json={"reason": "test"}, headers=H(admin_access))
        record("safety", "POST /robot/emergency-stop", "200", r.status_code, json.dumps(r.json()) if r.status_code == 200 else r.text[:80])
        r = await c.post("/robot/mode", json={"mode": "AUTONOMOUS", "acknowledgment": True}, headers=H(admin_access))
        record("safety", "POST /robot/mode during E-stop -> 423", "423", r.status_code, str(r.json().get("detail", "")))
        r = await c.post("/robot/emergency-stop/reset", headers=H(admin_access))
        record("safety", "POST /robot/emergency-stop/reset", "200", r.status_code)
        r = await c.post("/robot/emergency-stop/reset", headers=H(admin_access))
        record("safety", "POST /robot/emergency-stop/reset (not active) -> 400", "400", r.status_code)

        # Mapping — robot already seeded
        r = await c.post("/mapping/sessions", json={"robot_id": robot_id}, headers=H(admin_access))
        record("mapping", "POST /mapping/sessions", "201", r.status_code, "id=" + r.json().get("id", "?")[:8] if r.status_code == 201 else r.text[:120])
        if r.status_code != 201:
            return _summary()
        session_id = r.json()["id"]
        r = await c.get("/mapping/sessions", headers=H(admin_access))
        record("mapping", "GET /mapping/sessions list", "200", r.status_code, f"count={len(r.json())}")
        r = await c.get(f"/mapping/sessions/{session_id}", headers=H(admin_access))
        record("mapping", "GET /mapping/sessions/{id}", "200", r.status_code)
        r = await c.post(f"/mapping/sessions/{session_id}/start", headers=H(admin_access))
        record("mapping", "POST .../start", "200", r.status_code, "status=" + r.json().get("status", "?") if r.status_code == 200 else r.text[:120])

        # second session start conflict
        r = await c.post("/mapping/sessions", json={"robot_id": robot_id}, headers=H(admin_access))
        sid2 = r.json().get("id") if r.status_code == 201 else None
        if sid2:
            r = await c.post(f"/mapping/sessions/{sid2}/start", headers=H(admin_access))
            record("mapping", "POST .../start second (active) -> 409", "409", r.status_code, str(r.json().get("detail", "")))

        r = await c.post(f"/mapping/sessions/{session_id}/pause", headers=H(admin_access))
        record("mapping", "POST .../pause", "200", r.status_code)
        r = await c.post(f"/mapping/sessions/{session_id}/resume", headers=H(admin_access))
        record("mapping", "POST .../resume", "200", r.status_code)
        r = await c.post(f"/mapping/sessions/{session_id}/finish", json={"map_data_ref": "maps/test.pgm"}, headers=H(admin_access))
        record("mapping", "POST .../finish", "200", r.status_code, "status=" + r.json().get("status", "?") if r.status_code == 200 else r.text[:120])
        r = await c.post(f"/mapping/sessions/{session_id}/resume", headers=H(admin_access))
        record("mapping", "POST .../resume FINISHED -> 422", "422", r.status_code, str(r.json().get("detail", "")))

        # Destinations
        r = await c.post("/destinations", json={"name": "Home", "map_id": session_id, "x": 0.0, "y": 0.0, "theta": 0.0, "is_home": True}, headers=H(admin_access))
        record("destinations", "POST /destinations (Home)", "201", r.status_code, "id=" + r.json().get("id", "?")[:8] if r.status_code == 201 else r.text[:120])
        home_id = r.json().get("id") if r.status_code == 201 else None
        r = await c.post("/destinations", json={"name": "Office A", "map_id": session_id, "x": 1.0, "y": 2.0, "theta": 1.57}, headers=H(admin_access))
        record("destinations", "POST /destinations (Office)", "201", r.status_code)
        office_id = r.json().get("id") if r.status_code == 201 else None
        r = await c.get("/destinations", headers=H(admin_access))
        record("destinations", "GET /destinations list", "200", r.status_code, f"count={len(r.json())}")
        r = await c.get(f"/destinations?map_id={session_id}", headers=H(admin_access))
        record("destinations", "GET /destinations?map_id=...", "200", r.status_code, f"count={len(r.json())}")
        if office_id:
            r = await c.patch(f"/destinations/{office_id}", json={"name": "Office A1"}, headers=H(admin_access))
            record("destinations", "PATCH /destinations/{id}", "200", r.status_code)
        if home_id:
            r = await c.get(f"/destinations/{home_id}", headers=H(admin_access))
            record("destinations", "GET /destinations/{id}", "200", r.status_code)
            r = await c.delete(f"/destinations/{home_id}", headers=H(admin_access))
            record("destinations", "DELETE home -> 400", "400", r.status_code, str(r.json().get("detail", "")))
        if office_id:
            r = await c.delete(f"/destinations/{office_id}", headers=H(admin_access))
            record("destinations", "DELETE non-home -> 204", "204", r.status_code)

        # Delivery
        if home_id:
            await set_robot_mode(robot_id, "ONLINE", "AUTONOMOUS")
            r = await c.post("/deliveries", json={"robot_id": robot_id, "destination_id": home_id}, headers=H(admin_access))
            record("delivery", "POST /deliveries (autonomous+online)", "201", r.status_code, "id=" + r.json().get("id", "?")[:8] if r.status_code == 201 else r.text[:120])
            delivery_id = r.json().get("id") if r.status_code == 201 else None
        else:
            delivery_id = None

        r = await c.get("/deliveries/mine", headers=H(admin_access))
        record("delivery", "GET /deliveries/mine (admin)", "200", r.status_code, f"count={len(r.json())}")
        r = await c.get("/deliveries", headers=H(admin_access))
        record("delivery", "GET /deliveries all (admin)", "200", r.status_code, f"count={len(r.json())}")
        if user_access:
            r = await c.get("/deliveries/mine", headers=H(user_access))
            record("delivery", "GET /deliveries/mine (USER)", "200", r.status_code)
        if delivery_id:
            r = await c.get(f"/deliveries/{delivery_id}", headers=H(admin_access))
            record("delivery", "GET /deliveries/{id}", "200", r.status_code)

        # TODO endpoints — currently 501
        if delivery_id:
            r = await c.post(f"/deliveries/{delivery_id}/dispatch", headers=H(admin_access))
            record("delivery", "POST .../dispatch (TODO)", "501", r.status_code)
            r = await c.post(f"/deliveries/{delivery_id}/confirm-pickup", headers=H(admin_access))
            record("delivery", "POST .../confirm-pickup (TODO)", "501", r.status_code)
            r = await c.post(f"/deliveries/{delivery_id}/cancel", headers=H(admin_access))
            record("delivery", "POST .../cancel (TODO)", "501", r.status_code)

        # Robot not autonomous -> 409
        if home_id:
            await set_robot_mode(robot_id, "ONLINE", "STOPPED")
            r = await c.post("/deliveries", json={"robot_id": robot_id, "destination_id": home_id}, headers=H(admin_access))
            record("delivery", "POST /deliveries (not autonomous) -> 409", "409", r.status_code, str(r.json().get("detail", "")))

        # Simulation
        r = await c.post("/sim/inject-fault", json={"fault": "SLAM_LOST"}, headers=H(admin_access))
        record("simulation", "POST /sim/inject-fault (TODO)", "501", r.status_code)

        # ---------------------------------------------------------------
        # Self-registration (ERP-style) + Robot-access (new in this session)
        # ---------------------------------------------------------------
        # Flush rate-limit counters so the new endpoint tests start clean.
        # (The previous tests' login attempts may have filled the bucket.)
        try:
            import subprocess as _sp
            _sp.run(["docker", "exec", "robot_redis", "redis-cli", "FLUSHALL"],
                     capture_output=True, timeout=5)
        except Exception:
            pass

        # We use a unique email so reruns do not collide with prior runs.
        import uuid as _u
        new_user_email = f"e2e_newuser_{_u.uuid4().hex[:8]}@example.com"
        new_user_password = "E2eNewUser!2345"

        r = await c.post(
            "/auth/register",
            json={"email": new_user_email, "password": new_user_password, "name": "E2E New User"},
        )
        record("auth", "POST /auth/register (subsequent = USER)", "201", r.status_code,
               "role=" + r.json().get("user", {}).get("role", "?") if r.status_code == 201 else r.text[:120])
        new_user_token = r.json().get("access_token") if r.status_code == 201 else None
        new_user_id = r.json().get("user", {}).get("id") if r.status_code == 201 else None

        # Duplicate email
        r = await c.post(
            "/auth/register",
            json={"email": new_user_email, "password": new_user_password, "name": "Dup"},
        )
        record("auth", "POST /auth/register (duplicate) -> 409", "409", r.status_code, str(r.json().get("detail", "")))

        # New user can login
        r = await c.post("/auth/login", json={"email": new_user_email, "password": new_user_password})
        record("auth", "login newly-registered USER", "200", r.status_code)
        new_user_login_token = r.json().get("access_token") if r.status_code == 200 else None

        # /auth/me for new user
        if new_user_login_token:
            r = await c.get("/auth/me", headers=H(new_user_login_token))
            record("auth", "GET /auth/me (new USER)", "200", r.status_code, "role=" + r.json().get("role", "?"))

        # /auth/me/robots is empty for new user
        if new_user_login_token:
            r = await c.get("/auth/me/robots", headers=H(new_user_login_token))
            record("auth", "GET /auth/me/robots (no access yet)", "200", r.status_code, f"count={len(r.json())}")

        # Admin lists a user's robot access (initially empty)
        if new_user_id:
            r = await c.get(f"/admin/users/{new_user_id}/robots", headers=H(admin_access))
            record("admin", "GET /admin/users/{id}/robots (empty)", "200", r.status_code, f"count={len(r.json())}")

        # Admin grants the new user access to the robot
        if new_user_id and robot_id:
            r = await c.post(f"/admin/users/{new_user_id}/robots/{robot_id}", headers=H(admin_access))
            record("admin", "POST /admin/users/{id}/robots/{rid} grant", "201", r.status_code,
                   "robot_id=" + r.json().get("robot_id", "?")[:8] if r.status_code == 201 else r.text[:120])

            # Duplicate grant -> 409
            r = await c.post(f"/admin/users/{new_user_id}/robots/{robot_id}", headers=H(admin_access))
            record("admin", "POST /admin/users/{id}/robots/{rid} duplicate -> 409", "409", r.status_code, str(r.json().get("detail", "")))

            # List now shows 1 robot
            r = await c.get(f"/admin/users/{new_user_id}/robots", headers=H(admin_access))
            record("admin", "GET /admin/users/{id}/robots (after grant)", "200", r.status_code, f"count={len(r.json())}")

        # /auth/me/robots now contains the granted robot
        if new_user_login_token:
            r = await c.get("/auth/me/robots", headers=H(new_user_login_token))
            record("auth", "GET /auth/me/robots (after grant)", "200", r.status_code, f"count={len(r.json())}")

        # USER (no access) tries to book a delivery on the robot — needs to fail.
        # We create a separate user with no access for this check.
        no_access_email = f"e2e_noaccess_{_u.uuid4().hex[:8]}@example.com"
        r = await c.post(
            "/auth/register",
            json={"email": no_access_email, "password": "NoAccess!2345", "name": "No Access User"},
        )
        no_access_token = r.json().get("access_token") if r.status_code == 201 else None
        no_access_id = r.json().get("user", {}).get("id") if r.status_code == 201 else None

        # First, ensure robot is autonomous+online, then try as no-access USER
        await set_robot_mode(robot_id, "ONLINE", "AUTONOMOUS")
        if no_access_token and home_id:
            r = await c.post(
                "/deliveries",
                json={"robot_id": robot_id, "destination_id": home_id},
                headers=H(no_access_token),
            )
            record("delivery", "POST /deliveries (USER w/o access) -> 403", "403", r.status_code, str(r.json().get("detail", "")))

        # Now grant the no-access user access to the robot
        if no_access_id and robot_id:
            r = await c.post(f"/admin/users/{no_access_id}/robots/{robot_id}", headers=H(admin_access))
            record("admin", "Grant no-access user to robot", "201", r.status_code)

        # USER with access can book
        if no_access_token and home_id:
            r = await c.post(
                "/deliveries",
                json={"robot_id": robot_id, "destination_id": home_id},
                headers=H(no_access_token),
            )
            # 201 if robot is online+autonomous+idle; 409 if busy; both are valid
            record("delivery", "POST /deliveries (USER w/ access)", "201|409", str(r.status_code),
                   "ok" if r.status_code in (201, 409) else r.text[:120])

        # Revoke access
        if no_access_id and robot_id:
            r = await c.delete(f"/admin/users/{no_access_id}/robots/{robot_id}", headers=H(admin_access))
            record("admin", "DELETE /admin/users/{id}/robots/{rid} revoke", "204", r.status_code)

            # Revoke again -> 404
            r = await c.delete(f"/admin/users/{no_access_id}/robots/{robot_id}", headers=H(admin_access))
            record("admin", "DELETE /admin/users/{id}/robots/{rid} (already revoked) -> 404", "404", r.status_code)

        # ADMIN bypass: even without grant row, admin can dispatch
        # (use a fresh user that has no access row, to prove admin bypass)
        # Actually we already have a fresh admin scenario — just re-check admin can book
        if home_id:
            await set_robot_mode(robot_id, "ONLINE", "AUTONOMOUS")
            r = await c.post(
                "/deliveries",
                json={"robot_id": robot_id, "destination_id": home_id},
                headers=H(admin_access),
            )
            record("delivery", "POST /deliveries (ADMIN bypasses access)", "201|409", str(r.status_code),
                   "ok" if r.status_code in (201, 409) else r.text[:120])

    return _summary()


def _summary() -> int:
    print()
    print("=" * 110)
    print("END-TO-END HTTP SWEEP RESULTS")
    print("=" * 110)
    print(f"{'MODULE':<14} {'TEST':<58} {'EXPECT':>8} {'ACTUAL':>8}  STATUS  DETAIL")
    print("-" * 110)
    passed = 0
    failed = 0
    for module, name, expected, actual, full in results:
        mark = full.split(" ")[0]
        detail = full[len(mark):].strip()
        print(f"{module:<14} {name:<58} {expected:>8} {actual:>8}  {mark}  {detail[:80]}")
        if mark == "PASS":
            passed += 1
        else:
            failed += 1
    print("-" * 110)
    print(f"TOTAL: {passed + failed}  PASSED: {passed}  FAILED: {failed}")
    print("=" * 110)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
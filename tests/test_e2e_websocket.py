"""Phase 5: WebSocket integration test (v3 - proper close detection)."""
import asyncio
import json
import sys

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS_BASE = "ws://127.0.0.1:8000"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "change-me-admin-password"
USER_EMAIL = "testuser@example.com"
USER_PASSWORD = "TestUser!234"
ROBOT_API_KEY = "change-me-robot-key"

results = []


def record(name, expected, actual, detail=""):
    mark = "PASS" if expected == actual else "FAIL"
    results.append((name, str(expected), str(actual), mark, detail))


async def login(client, email, password, retries=3):
    for _ in range(retries):
        r = await client.post("/auth/login", json={"email": email, "password": password})
        if r.status_code == 200:
            return r.json().get("access_token")
        if r.status_code == 429:
            await asyncio.sleep(2)
            continue
        return None
    return None


async def try_ws(url):
    """Connect to WS and detect either close frame or HTTP-level rejection."""
    try:
        ws = await websockets.connect(url, open_timeout=5, ping_interval=None)
    except websockets.exceptions.InvalidStatus as e:
        return ("HTTP_" + str(e.response.status_code), "http_reject")
    try:
        # Wait for either a message OR a close frame
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            return ("msg", msg[:60])
        except asyncio.TimeoutError:
            # No message and no close — server is keeping the connection open
            return ("open", "open")
    except websockets.exceptions.ConnectionClosed as e:
        return (str(e.code), "ws_close")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as c:
        admin_token = await login(c, ADMIN_EMAIL, ADMIN_PASSWORD)
        user_token = await login(c, USER_EMAIL, USER_PASSWORD)
    print(f"admin_token: {'OK' if admin_token else 'MISSING'}")
    print(f"user_token:  {'OK' if user_token else 'MISSING'}")

    # 1. Robot WS valid api_key
    code, kind = await try_ws(f"{WS_BASE}/ws/robot?api_key={ROBOT_API_KEY}")
    record("Robot WS valid api_key", "open", kind, f"got={code}")

    # 2. Robot WS no api_key
    code, kind = await try_ws(f"{WS_BASE}/ws/robot")
    record("Robot WS no api_key -> close 4001", "4001", code, "kind=" + kind)

    # 3. Robot WS wrong api_key
    code, kind = await try_ws(f"{WS_BASE}/ws/robot?api_key=wrong-key")
    record("Robot WS wrong api_key -> close 4001", "4001", code)

    # 4. Dashboard WS admin
    code, kind = await try_ws(f"{WS_BASE}/ws/dashboard?token={admin_token}")
    record("Dashboard WS admin -> snapshot", "msg", code, f"kind={kind}")

    # 5. Dashboard WS USER token
    if user_token:
        code, kind = await try_ws(f"{WS_BASE}/ws/dashboard?token={user_token}")
        record("Dashboard WS USER -> close 4003", "4003", code, "kind=" + kind)
    else:
        record("Dashboard WS USER", "skip", "no token")

    # 6. Dashboard WS no token
    code, kind = await try_ws(f"{WS_BASE}/ws/dashboard")
    record("Dashboard WS no token -> close 4001", "4001", code)

    # 7. Dashboard WS invalid token
    code, kind = await try_ws(f"{WS_BASE}/ws/dashboard?token=invalid.token.value")
    record("Dashboard WS invalid token -> close 4001", "4001", code)

    # 8. Two-way broadcast: dashboard listens, robot sends heartbeat
    print("\n[8] Heartbeat broadcast...")
    try:
        ws_dash = await websockets.connect(f"{WS_BASE}/ws/dashboard?token={admin_token}", open_timeout=5, ping_interval=None)
        # Drain snapshot
        snap = await asyncio.wait_for(ws_dash.recv(), timeout=3.0)
        snap_data = json.loads(snap)
        print(f"  dashboard snapshot received: type={snap_data.get('type')}")

        # Get robot id from snapshot
        robot_id = snap_data["payload"]["id"]

        # Now robot connects and sends heartbeat
        ws_robot = await websockets.connect(f"{WS_BASE}/ws/robot?api_key={ROBOT_API_KEY}", open_timeout=5, ping_interval=None)
        # Mark robot OFFLINE first so heartbeat causes transition
        import aiomysql
        conn = await aiomysql.connect(host="127.0.0.1", port=3306, user="root", password="", db="robot_db")
        cur = await conn.cursor()
        await cur.execute("UPDATE robots SET status='OFFLINE' WHERE id=%s", (robot_id,))
        await conn.commit()
        await cur.close()
        conn.close()
        print("  robot marked OFFLINE in DB; sending heartbeat...")
        await ws_robot.send(json.dumps({"type": "robot.heartbeat", "payload": {"battery_level": 0.42}}))
        # Wait for broadcast (OFFLINE -> ONLINE transition)
        try:
            msg = await asyncio.wait_for(ws_dash.recv(), timeout=5.0)
            data = json.loads(msg)
            record("Heartbeat broadcast (OFFLINE->ONLINE)", "msg", "msg", f"event={data.get('type')}")
        except asyncio.TimeoutError:
            record("Heartbeat broadcast (OFFLINE->ONLINE)", "msg", "timeout", "no broadcast within 5s")
        await ws_robot.close()
        await ws_dash.close()
    except Exception as e:
        record("Heartbeat broadcast", "msg", f"{type(e).__name__}: {e}")

    # Print results
    print()
    print("=" * 110)
    print("WEBSOCKET INTEGRATION RESULTS")
    print("=" * 110)
    print(f"{'TEST':<55} {'EXPECT':>10} {'ACTUAL':>10}  STATUS  DETAIL")
    print("-" * 110)
    passed = failed = 0
    for name, expected, actual, mark, detail in results:
        print(f"{name:<55} {expected:>10} {actual:>10}  {mark}  {detail[:60]}")
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
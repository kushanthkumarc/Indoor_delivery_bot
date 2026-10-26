"""Read-only API checks for the Android demo accounts and their data contracts."""
import json
import urllib.error
import urllib.request

from app.core.config import settings


BASE = "http://127.0.0.1:8000"
results = []


def request(path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def check(name, condition):
    results.append({"test": name, "passed": bool(condition)})


def main():
    check("backend ready", request("/ready")[0] == 200)
    check("unauthenticated delivery access rejected", request("/deliveries")[0] == 401)
    check("invalid credentials rejected", request("/auth/login", body={
        "email": "video.demo@example.com", "password": "invalid"})[0] == 401)
    code, login = request("/auth/login", body={"email": str(settings.ADMIN_EMAIL), "password": settings.ADMIN_PASSWORD})
    check("admin login", code == 200)
    admin = login["access_token"]
    code, login = request("/auth/login", body={"email": "video.demo@example.com", "password": "VideoDemo-2026!"})
    check("demo user login", code == 200)
    user = login["access_token"]
    for path in ("/auth/me", "/auth/me/robots", "/robot/status", "/mapping/sessions", "/destinations", "/deliveries", "/events", "/admin/users"):
        check("admin GET " + path, request(path, admin)[0] == 200)
    for path in ("/admin/users", "/robot/status", "/events", "/deliveries"):
        check("user forbidden GET " + path, request(path, user)[0] == 403)
    check("user forbidden dashboard ticket", request("/dashboard/ws-ticket", user, {})[0] == 403)
    code, robots = request("/auth/me/robots", user)
    check("user sees granted DEMO robot", code == 200 and any(r["name"].startswith("DEMO - ") for r in robots))
    code, destinations = request("/destinations", user)
    check("user sees four demo destinations", code == 200 and sum(d["name"].startswith("DEMO - ") for d in destinations) == 4)
    code, deliveries = request("/deliveries/mine", user)
    demo_deliveries = [d for d in deliveries if (d.get("destination_name") or "").startswith("DEMO - ")]
    check("user sees own demo samples and requests", code == 200 and len(demo_deliveries) >= 3)
    check("demo delivery states recognized", bool(demo_deliveries) and all(d["status"] in {
        "IDLE", "GO_TO_SOURCE", "AT_SOURCE", "GO_TO_DESTINATION", "ARRIVED", "DELIVERED",
        "RETURN_HOME", "HOME", "EMERGENCY_STOP"} for d in demo_deliveries))
    check("delivery list includes names, IDs, dates", bool(demo_deliveries) and all(d.get("robot_id") and d.get("created_at") and d.get("destination_name") for d in demo_deliveries))
    for delivery in demo_deliveries:
        code, detail = request("/deliveries/" + delivery["id"], user)
        check("user detail " + delivery["status"], code == 200 and detail.get("destination_name") == delivery["destination_name"])
    _, all_deliveries = request("/deliveries", admin)
    mine_ids = {d["id"] for d in deliveries}
    other = next((d for d in all_deliveries if d["id"] not in mine_ids), None)
    check("other user's delivery denied", other is not None and request("/deliveries/" + other["id"], user)[0] == 403)
    print(json.dumps({"passed": sum(r["passed"] for r in results), "total": len(results), "results": results}, indent=2))
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

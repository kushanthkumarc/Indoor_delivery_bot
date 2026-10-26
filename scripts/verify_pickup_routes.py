"""Check pickup booking against the local development API, preserving a demo request."""
import json
from app.core.config import settings
from scripts.verify_video_demo import request


def main():
    if settings.APP_ENV != "development":
        raise RuntimeError("This check creates a demo request; development only")
    results = []
    def check(name, ok):
        results.append({"test": name, "passed": bool(ok)})
    _, login = request("/auth/login", body={"email": "video.demo@example.com", "password": "VideoDemo-2026!"})
    token = login["access_token"]
    _, login = request("/auth/login", body={"email": str(settings.ADMIN_EMAIL), "password": settings.ADMIN_PASSWORD})
    admin = login["access_token"]
    _, locations = request("/destinations", token)
    source = next(d for d in locations if d["name"] == "DEMO - Robotics Lab")
    target = next(d for d in locations if d["name"] == "DEMO - Library")
    check("mapped locations include robot identifiers", source.get("robot_id") and target.get("robot_id") == source["robot_id"])
    body = {"robot_id": source["robot_id"], "source_id": source["id"], "destination_id": target["id"]}
    check("same location rejected", request("/deliveries", token, {**body, "destination_id": source["id"]})[0] == 422)
    check("missing pickup rejected", request("/deliveries", token, {k: v for k, v in body.items() if k != "source_id"})[0] == 422)
    check("nonexistent pickup rejected", request("/deliveries", token, {**body, "source_id": "00000000-0000-0000-0000-000000000000"})[0] == 404)
    _, all_locations = request("/destinations", admin)
    cross = next((d for d in all_locations if d["map_id"] != source["map_id"]), None)
    if cross:
        check("different maps rejected", request("/deliveries", token, {**body, "destination_id": cross["id"]})[0] == 409)
    code, created = request("/deliveries", token, body)
    check("user creates offline queued request", code == 201)
    check("both names and IDs saved", created.get("source_id") == source["id"] and created.get("source_name") == source["name"] and created.get("destination_name") == target["name"])
    ident = created["id"]
    for role, session, path in (("user", token, "/deliveries/mine"), ("admin", admin, "/deliveries")):
        code, records = request(path, session)
        check(role + " list preserves route", code == 200 and any(d["id"] == ident and d.get("source_name") == source["name"] for d in records))
        code, detail = request("/deliveries/" + ident, session)
        check(role + " detail preserves route", code == 200 and detail.get("source_id") == source["id"])
    check("user cannot dispatch", request("/deliveries/" + ident + "/dispatch", token, {})[0] == 403)
    check("hardware execution unavailable remains explicit", request("/deliveries/" + ident + "/dispatch", admin, {})[0] == 503)
    check("no fabricated physical execution", created.get("physical_execution_confirmed") is False)
    print(json.dumps({"passed": sum(r["passed"] for r in results), "total": len(results), "results": results}, indent=2))
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

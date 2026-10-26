# Mapped pickup and destination deliveries

Apply `alembic upgrade head` to add migration `72c1a5e9d031` before serving this API.
It preserves legacy records with a nullable source; new bookings require a source.

`POST /deliveries` accepts source_id, destination_id, and robot_id. Locations must
be distinct and belong to the same finished map for an authorized robot. Booking
is separate from dispatch and may queue requests while the robot is offline.
List/detail responses include location names, IDs, and creation/completion dates.

Flow: IDLE → GO_TO_SOURCE → AT_SOURCE → loading confirmation → GO_TO_DESTINATION
→ ARRIVED → receipt confirmation → DELIVERED.

`POST /deliveries/{id}/confirm-loading` permits the requesting user or an admin.
Dispatch/loading require a configured navigation adapter and readiness checks.
Receipt of source-based deliveries records the user's attestation without needing
a navigation adapter or starting a return trip. Physical execution is not inferred.
Legacy deliveries retain their existing lifecycle.

## Development demos and verification

The scripts in scripts/ add labeled synthetic map/delivery records, preserve existing
accounts and data, and exercise role restrictions and the Android data contracts.
The seed refuses to run outside APP_ENV=development. Its fixed demo login is for
local demonstrations only. Real admin credentials are read from configuration.

Run inside the development app container with PYTHONPATH=/app:

```text
python scripts/seed_video_demo.py
python scripts/verify_video_demo.py
python scripts/verify_pickup_routes.py
```

The pickup API check creates a labeled demo booking. Repeated runs can create
additional requests. The seed itself uses stable fixture IDs and is repeatable.

Validation: 17 isolated delivery lifecycle tests passed, 14 live pickup-route checks
passed, and 29 demo API checks passed after the on-device walkthrough. The lifecycle
tests use SQLite/aiosqlite and a test-only navigation adapter. Hardware movement,
physical E-stop, and real mapping require separate verification.

Authentication throttling is configurable through AUTH_RATE_LIMIT_MAX_ATTEMPTS
and AUTH_RATE_LIMIT_WINDOW_S. Defaults remain 5 attempts per 900 seconds. Local
demo overrides belong in an untracked .env file. Database and Redis ports remain
internal to the Docker Compose network.

# Indoor Delivery Robot Backend

FastAPI + MySQL + Redis backend for the Autonomous Indoor Delivery Robot system.

See `.kiro/specs/indoor-delivery-robot-backend/` for the full requirements, design, and task breakdown.

## Local development

```bash
# 1. Copy env template and edit (DB URL, Redis URL, secrets)
cp .env.example .env

# 2. Install deps
pip install -r requirements.txt
pip install aiomysql==0.2.0      # required version (newer breaks pool_pre_ping)

# 3. Run tests
python tests/test_e2e_http_sweep.py        # 78 cases
python tests/test_e2e_websocket.py         # 8 cases
python -m pytest tests/ -v \
    --ignore=tests/test_e2e_http_sweep.py \
    --ignore=tests/test_e2e_websocket.py   # 14 unit tests
```

Migrations are auto-applied on app startup (see `app/core/startup.py`).

## Run locally

```bash
# Start MySQL + Redis (Docker)
docker run -d --name mysql -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=robot_db -p 3306:3306 mysql:8.0
docker run -d --name redis -p 6379:6379 redis:7-alpine

# Start the API
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/docs for the Swagger UI.

## Deploy to Render

The app auto-runs `alembic upgrade head` on startup, so a single Render Web Service is enough. The Dockerfile in this repo is the build entry point.

### 1. Provision external services (free tiers work)

| Service | Provider | Notes |
|---|---|---|
| MySQL | **PlanetScale** or **Aiven** | Get a connection string like `mysql+aiomysql://USER:PASS@HOST:3306/robot_db` |
| Redis | **Upstash** | Get `rediss://default:PASS@HOST:6379/0` — change scheme to `redis://` (we don't support TLS yet) |

### 2. Create a Render Web Service

- **Source**: connect your GitHub repo
- **Runtime**: Docker (Render will detect the `Dockerfile` automatically)
- **Region**: any
- **Instance type**: Free (Starter) for dev

### 3. Set environment variables on the Web Service

```
DATABASE_URL=mysql+aiomysql://USER:PASS@HOST:3306/robot_db
REDIS_URL=redis://default:PASS@HOST:6379/0

# Generate strong values (32+ random bytes) for these:
JWT_SECRET=<generate with: openssl rand -hex 32>
ROBOT_API_KEY=<generate with: openssl rand -hex 32>
ADMIN_PASSWORD=<your-strong-password>

# First-admin seed (optional — if unset, the first /auth/register call becomes admin)
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=<strong-password>

# Production settings
SIMULATION_MODE=false
DEBUG=false
APP_HOST=0.0.0.0
APP_PORT=10000
```

### 4. Deploy

Render will build the Docker image and start the service. The first boot will:
1. Connect to your MySQL
2. Run `alembic upgrade head` → create all tables (`users`, `robots`, `mapping_sessions`, `destinations`, `deliveries`, `events`, `user_robot_access`)
3. Seed the first admin from `ADMIN_EMAIL`/`ADMIN_PASSWORD` if both are set
4. Start the API on port 10000

### 5. Verify

```
curl https://your-service.onrender.com/health
# → {"status":"ok"}
```

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Application startup failed. Exiting` in logs | Check `DATABASE_URL` format and that MySQL is reachable from Render (PlanetScale requires the connection to come from a specific IP range; enable "Allow all" in the dashboard for testing) |
| `redis.exceptions.ConnectionError` | Confirm `REDIS_URL` uses `redis://` not `rediss://` (we don't support TLS yet) |
| `AssertionError: Status code 204 must not have a response body` | Should not happen on the current main — all 204 routes have `response_class=Response` |
| 500 on `/auth/login` after a few attempts | You're being rate-limited. Wait 15 minutes or flush the Redis bucket. |

## API Endpoints

Swagger UI at `/docs` (auto-generated).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness check |
| POST | `/auth/login` | none | Email+password → access token + refresh cookie |
| POST | `/auth/register` | none | Public self-registration; first-ever caller becomes ADMIN |
| POST | `/auth/refresh` | cookie | New access token |
| POST | `/auth/logout` | bearer | Invalidate refresh token |
| GET | `/auth/me` | bearer | Current user profile |
| GET | `/auth/me/robots` | bearer | Robots the current user has been granted access to |
| POST | `/admin/users` | ADMIN | Create user |
| GET | `/admin/users` | ADMIN | List users |
| PATCH | `/admin/users/{id}` | ADMIN | Update role / is_active |
| DELETE | `/admin/users/{id}` | ADMIN | Soft-delete |
| POST | `/admin/users/{id}/robots/{rid}` | ADMIN | Grant user access to a robot |
| DELETE | `/admin/users/{id}/robots/{rid}` | ADMIN | Revoke access |
| GET | `/admin/users/{id}/robots` | ADMIN | List a user's robot access |
| POST/GET | `/mapping/sessions[/...]` | ADMIN | SLAM mapping session lifecycle |
| GET | `/robot/status` | ADMIN | Robot snapshot |
| GET | `/destinations` | ADMIN | Destination CRUD |
| POST/GET | `/deliveries` | USER+access | Book a delivery on a robot you're granted access to |
| POST | `/robot/mode` | ADMIN | Switch control mode |
| POST | `/robot/emergency-stop` | ADMIN | Trigger e-stop |
| POST | `/robot/emergency-stop/reset` | ADMIN | Reset e-stop |
| GET | `/robot/safety-state` | ADMIN | Current safety state |
| GET | `/events` | ADMIN | Audit log (filterable, paginated) |
| WS | `/ws/dashboard` | ADMIN JWT | Real-time robot events |
| WS | `/ws/robot` | api_key | Inbound robot telemetry |

## Architecture

```
Web App (browser)  ──HTTPS/WSS──▶  FastAPI Backend  ──rosbridge WS──▶  Robot
                                       │
                                       ├── PostgreSQL/MySQL  (persistent state)
                                       └── Redis             (blacklist, rate limit, pub/sub)
```

See `.kiro/specs/indoor-delivery-robot-backend/design.md` for the full architecture diagram.

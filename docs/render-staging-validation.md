# Render cloud-only staging validation

This is an operator checklist for a **staging** Render service. Reading this
document or passing isolated tests does not mean Render, its ingress, or a
managed database has been contacted. Use staging-only credentials and never
paste secret values into logs, tickets, shell history, or command output.

## Staging configuration checklist

Configure these values in the Render service's environment settings. Do not
put secret values in a checked-in `.env` file.

| Variable | Staging value / validation |
| --- | --- |
| `APP_ENV` | `production` (enables production validation even outside Render) |
| `DATABASE_URL` | Required. `mysql+aiomysql://...`; include `ssl-mode=VERIFY_CA` or `ssl-mode=VERIFY_IDENTITY`. Use a staging database and TLS trust chain the container can verify. Never print the URL. |
| `REDIS_URL` | Required and must start `rediss://`; use a staging Redis service. Never print the URL. |
| `JWT_SECRET` | Required, random, non-default, at least 32 characters. Generate and store it in the secret manager; do not echo it. |
| `JWT_ALGORITHM` | Keep `HS256`; production validation rejects other algorithms. |
| `ROSBRIDGE_ENABLED` | `false` (production validation rejects `true`) |
| `RUN_MIGRATIONS_ON_STARTUP` | `false`; run one controlled migration step before app rollout. |
| `DEBUG` | `false` |
| `CORS_ORIGINS` | Comma-separated exact HTTPS dashboard origins. No `*`; include only origins that must call the API. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Set both only when no active administrator exists and startup bootstrap is intended. Password must be unique, non-default, and at least 14 characters. Remove both after bootstrap. Public registration creates `USER`, not `ADMIN`. |
| `PORT` | Supplied by Render. Do not hard-code it. Docker binds `0.0.0.0:$PORT`, with `APP_PORT`/8000 only as local fallback. |

`DATABASE_URL` validation requires TLS verification mode, but does not prove
that the remote certificate chain is valid or that the provider supports the
configured mode. `REDIS_URL` scheme validation likewise does not prove the
remote service is reachable. Confirm both through a successful startup and
`/ready`, without displaying either URL. Production startup fails closed for
missing required settings, unsafe JWT values, plaintext Redis, unverified
MySQL, wildcard CORS, ROS enabled, or startup migrations enabled.

## Migration and MySQL checklist

1. Provision an isolated staging database and a temporary migration account.
   Keep it separate from the runtime app account and scope grants to the one
   target schema.
2. Before every migration, take a database-native backup and restore it to a
   separate disposable schema/instance. Compare table counts, critical data,
   indexes, and triggers before treating the backup as usable. Follow
   [mysql-migration-validation.md](mysql-migration-validation.md) for exact
   disposable validation commands.
3. The migration account needs schema-scoped `CREATE`, `ALTER`, `DROP`,
   `INDEX`, `REFERENCES`, `TRIGGER`, and Alembic version-table `SELECT`,
   `INSERT`, and `UPDATE`. Do not grant global privileges or `SUPER`.
4. If binary logging is enabled, the database operator must configure
   `log_bin_trust_function_creators=1` through the provider's supported
   parameter mechanism before migration. Confirm its effective value. The
   chain creates append-only event triggers; do not bypass that invariant.
5. Run one controlled `alembic upgrade head` as a release operation with the
   migration account, inspect the target revision and schema, then deploy the
   app using the least-privilege runtime account. Keep
   `RUN_MIGRATIONS_ON_STARTUP=false` on every web instance.
6. Treat recovery as restore-forward from the verified backup or a reviewed
   corrective migration. The initial downgrade is destructive; do not use it
   as a production rollback plan.

The full Alembic chain and backup/restore were exercised against disposable
local MySQL 8 during Phase 3.6. This does not verify Render's MySQL TLS,
privilege model, parameter support, or staging data restore.

## Startup, shutdown, and health checks

Before connecting staging data, build the image locally:

```powershell
docker build -t indoor-delivery-backend:staging-check .
```

The container command uses Render's injected `PORT`; local fallback is
`APP_PORT` or 8000. Production startup checks settings, MySQL `SELECT 1`,
Redis `PING`, optional first-admin bootstrap, safety state sync, then starts
the heartbeat monitor. It fails startup on an exception and logs the failing
stage and exception class without printing secret values. With
`ROSBRIDGE_ENABLED=false`, it does not construct a ROS client. Shutdown
cancels the monitor and closes Redis and
SQLAlchemy resources. Isolated startup/shutdown behavior is covered by
`tests/test_deployment_readiness.py`; Render process behavior remains to be
checked in staging.

After deploying to the Render **staging** service, substitute only its public
hostname:

```powershell
curl.exe -i https://<staging-host>/health
curl.exe -i https://<staging-host>/ready
```

Expected behavior:

- `/health` returns HTTP 200 while the app process is alive. It intentionally
  does not test dependencies.
- `/ready` returns HTTP 200 only if both MySQL and Redis respond. If either
  dependency fails, it returns HTTP 503 and per-dependency boolean checks;
  it does not return exception text or credentials.
- In staging only, exercise each dependency failure and recovery separately:
  while the dependency is unavailable, `/health` remains 200 and `/ready`
  becomes 503; after recovery, `/ready` returns 200. Do not inject failures
  into production.
- Inspect Render deploy logs for successful startup and graceful shutdown on
  a controlled staging restart. Confirm logs contain no DB/Redis URLs,
  passwords, JWTs, robot keys, WebSocket tickets, or authenticated message
  bodies.

## Dashboard ticket and ingress log checks

The browser protocol is:

1. Authenticated administrator sends `POST /dashboard/ws-ticket` with
   `Authorization: Bearer <access-token>`.
2. The backend returns a random, single-use ticket with a lifetime capped at
   30 seconds and the access session's remaining lifetime. The ticket is
   bound to that session. Redis failure fails closed.
3. The client immediately opens
   `wss://<staging-host>/ws/dashboard?ticket=<ticket>`.
4. On logout, the ticket cannot be redeemed and an already-open dashboard
   socket checks session revocation approximately once per second. If the
   revocation store becomes unavailable, the socket closes rather than
   assuming the session is valid.

Backend issuance, expiry, replay, logout, Redis failure, and socket closure
have isolated regression coverage; the real-Redis checks also passed in
Phase 3.6. These do not verify Render's ingress logs.

**Staging gate: pending.** In a Render staging service, mint a disposable
ticket, connect with it, and inspect Render ingress/access logs plus every
configured proxy/CDN log before retaining or exporting them. Verify the full
query string and ticket value are omitted or redacted. Do not use a production
ticket. Application log redaction cannot establish what an upstream proxy
records. No Render staging service or ingress logs were available during this
review, so ingress redaction is unverified and must block a release that
depends on query-ticket confidentiality until confirmed or the ingress
behavior is otherwise made safe.

This repository contains no dashboard frontend source. The frontend team must
implement and validate the exchange above: obtain a fresh ticket immediately
before each WebSocket connection/reconnect, avoid persisting or logging it,
handle expired/replayed/revoked tickets and closed sockets by obtaining a new
ticket only after authenticating again, and keep the access JWT out of the
WebSocket URL. Required frontend acceptance cases are:

- A logged-in administrator exchanges an access JWT for a ticket, opens the
  dashboard socket, and receives dashboard events. An unauthenticated,
  non-admin, expired, replayed, or logged-out session cannot open a socket.
- Reconnect obtains a new ticket; it never reuses a consumed/expired ticket
  or falls back to putting the access JWT in the URL. Redis/auth failures are
  shown as a recoverable authentication/service error, not as an empty robot
  dashboard.
- After logout, the client stops using that session and closes its local
  dashboard view. The backend closes the established socket on its periodic
  revocation check (approximately one second); do not claim instantaneous
  cross-instance closure.
- Safety displays are always qualified by connectivity and age. The REST
  `/robot/status` response provides `connection_state` and `last_seen`; the
  WebSocket sends `robot.safety_update` separately from
  `robot.freshness`/`robot.offline`. Consumers must join those signals, label
  safety as the **last reported** state, and never show an offline/stale
  robot as currently safe. A later `SAFE` value is current reported
  telemetry only after fresh authenticated telemetry arrives.
- Exercise the freshness boundaries using the configured
  `ROBOT_STALE_AFTER_S` and `HEARTBEAT_TIMEOUT_S` (defaults 3s and 5s):
  `ONLINE` becomes `STALE` after the stale threshold and `OFFLINE` after the
  heartbeat threshold; transport disconnect is immediately `OFFLINE`.

Frontend integration remains pending; no frontend tests are claimed.

## Cloud/robot isolation and staging sign-off

- Confirm Render has `ROSBRIDGE_ENABLED=false` and
  `RUN_MIGRATIONS_ON_STARTUP=false` in its effective environment. The
  production validator rejects unsafe values before startup.
- Delivery routes have no navigation adapter and return HTTP 503 for execution
  actions; they do not instantiate a ROS bridge or send robot commands.
- Robot connectivity is telemetry-driven. Without an authenticated gateway
  and recent server-received heartbeat, connection freshness is `OFFLINE` or
  `STALE`; disabling ROS does not mark connectivity/localization healthy.
  The gateway status fields represent reported telemetry, not independently
  verified physical safety or localization. `/robot/status` may retain the
  last persisted `safety_state` (including `SAFE`) when `connection_state` is
  `OFFLINE`; consumers must treat it as the last reported value and qualify
  it with `last_seen` and `connection_state`, never as current physical proof.
- `command_ack` and `command_result` are protocol reports and explicitly
  carry `physical_execution_confirmed: false`. Delivery state changes and
  cancellation requests do not prove motor actuation or a physical stop.
- Do not provision real robot credentials or send robot commands during this
  cloud-only staging check.

Cloud-only staging is **not release-ready** until Render startup, dependency
TLS/readiness, ingress ticket redaction, and separate frontend integration
have been checked in the staging environment. The backend and disposable
infrastructure evidence recorded above is not a substitute for those checks.

## Operator actions still required

No Render staging service, managed database/Redis endpoint, or Render log
access was available during this review. To close the staging checks, an
authorized staging operator must:

1. In Render's staging service settings, configure and verify the values in
   the environment table above. Confirm the three isolation flags are
   exactly false and migrations-on-startup is false without copying any
   secret values into a terminal or ticket.
2. Confirm the staging MySQL/Redis services are running and TLS-enabled;
   start/restart only the staging web service and inspect startup logs for
   successful MySQL/Redis checks and a clean shutdown on a controlled
   restart.
3. Run the two `curl.exe -i` requests above. In a staging-only fault window,
   make each dependency unavailable in turn, record `/health` 200 and
   `/ready` 503, restore it, and record `/ready` 200. Keep this isolated from
   production.
4. Mint a disposable dashboard ticket and inspect Render ingress/access and
   any CDN/proxy logs. Confirm the query and ticket are redacted before logs
   are retained/exported; do not paste the ticket into the validation record.
5. Run the frontend acceptance cases against that staging service and attach
   results from the dashboard repository/build. Specifically verify stale
   and offline status never appears as current physical safety.

Until those operator checks and frontend acceptance results are recorded,
Render and end-to-end dashboard staging validation remain pending.

# Render Free-plan deployment and demo data

Service/app base URL: `https://indoor-delivery-bot.onrender.com`

Git deploys code, not database records. Migrate the hosted MySQL schema first,
then explicitly seed labelled demo records. Do not restore a local database over
existing hosted data. Use a dedicated staging database for this demonstration.

The supplied deployment log shows a successful image build followed by a
configuration-validation failure, before database and Redis checks. Older code
hid the specific validation reason; the updated startup logs report safe setting
names and requirements, never their secret values. This does not prove which
setting is wrong until the new logs are available.

## 1. Configure Render Environment

`avnadmin` is usually an Aiven username, not confirmation of the provider. If
using Aiven, obtain its actual host, port, database name and project CA certificate
from the provider dashboard. Do not assume port 3306 or database `robot_db`.

Set these variables in Render's Environment page. Replace every placeholder
privately; do not paste secrets into chat, source control or screenshots.

```dotenv
APP_ENV=staging
DEBUG=false
ROSBRIDGE_ENABLED=false
ROBOT_COMMAND_CHANNEL_ENABLED=false
RUN_MIGRATIONS_ON_STARTUP=false
JWT_ALGORITHM=HS256
JWT_SECRET=<unique-random-secret-at-least-32-characters>
DATABASE_URL=mysql+aiomysql://avnadmin:<URL-encoded-password>@<mysql-host>:<mysql-port>/<database>?ssl-mode=VERIFY_IDENTITY&ssl-ca=/etc/secrets/aiven-ca.pem
REDIS_URL=rediss://default:<URL-encoded-password>@<upstash-host>:<upstash-port>/0
ADMIN_EMAIL=<your-admin-email>
ADMIN_PASSWORD=<unique-admin-password-at-least-14-characters>
DEMO_USER_PASSWORD=<different-unique-password-at-least-14-characters>
```

Use the Redis protocol endpoint from Upstash, NOT its HTTPS REST endpoint or REST
token. URL-encode special characters in connection-string usernames/passwords.
`CORS_ORIGINS` can be empty for the native Android client; do not use `*`.
Keep the secure authentication rate-limit defaults on this public service.

Upload the MySQL project's CA certificate as Render Secret File `aiven-ca.pem`.
It is available inside the Docker service at `/etc/secrets/aiven-ca.pem`.
Do not disable certificate verification to get deployment working.

Render sets `RENDER=true`, so production security validation also applies to
`APP_ENV=staging`. Staging is used to explicitly allow labelled demo fixtures,
not to bypass deployment security checks.

## 2. Migrate from your computer (Free plan)

Free web services have no dashboard Shell/SSH or pre-deploy command. Use a trusted
local Docker container connected to the same hosted MySQL/Redis instead. Docker
Desktop must be running. The migration account needs DDL and TRIGGER privileges.

In the backend folder, create **`.env.render`** privately with the same variables
as Render plus `RENDER=true`. Do not overwrite your existing local `.env`.
`.env.render` is Git-ignored and excluded from Docker builds. Store your CA file
outside the repository, for example `C:\Users\ADMIN\Downloads\aiven-ca.pem`.

Run from PowerShell in the backend folder:

```powershell
docker build -t indoor-render-tools .
$renderCaPath = 'C:\Users\ADMIN\Downloads\aiven-ca.pem'
if (-not (Test-Path -LiteralPath $renderCaPath)) { throw 'CA file not found' }
$renderToolArgs = @('--rm', '--env-file', '.env.render', '--mount', "type=bind,source=$renderCaPath,target=/etc/secrets/aiven-ca.pem,readonly", 'indoor-render-tools')
docker run @renderToolArgs python -m scripts.check_deployment
```

Continue only if configuration validation succeeds:

```powershell
docker run @renderToolArgs alembic upgrade head
```

Stop if the migration fails. Inspect the error locally (redact credentials before
sharing it). This changes the hosted database, not the local development DB.
Configuration validation alone does not establish database/Redis connectivity.

## 3. Redeploy, then seed

After the migration succeeds, deploy the updated code in Render. Startup checks
MySQL/Redis and provisions the first admin from the supplied bootstrap settings
if no active admin exists. Existing admin passwords are not reset.

Wait for `https://indoor-delivery-bot.onrender.com/ready` to return HTTP 200 with
database and Redis checks true. Then run the explicit seed command from the same
PowerShell session on your computer:

```powershell
docker run @renderToolArgs python -m scripts.seed_video_demo --allow-staging
```

The command refuses `APP_ENV=production`, requires an active admin, and requires
a non-default staging demo password of at least 14 characters. It creates a
dedicated `video.demo@example.com` USER, labelled map/destinations, robot access,
and sample delivery history. In staging it does not grant demo access or add
deliveries to unrelated users. Rerunning preserves existing accounts and fixtures;
it does not reset an existing demo user's password.

The admin can inspect these records. The dedicated demo USER sees only its own
permitted records. Robot hardware remains OFFLINE/STOPPED; no physical telemetry,
camera feed, navigation or confirmed motor stop is fabricated. This seeds demo
records; it does not automatically simulate a live delivery.

After successful provisioning, remove both `ADMIN_EMAIL` and `ADMIN_PASSWORD`
together from Render if no longer needed. `DEMO_USER_PASSWORD` is needed only
when invoking the seed command, not for normal API operation. Protect/remove
local `.env.render` when no longer needed. Never commit it.

## 4. Android verification

Sign out before changing the app endpoint. In Server Settings choose STAGING and
save `https://indoor-delivery-bot.onrender.com` (not `/docs` or `/ready`).
Log in to the Admin portal using your bootstrap/existing admin credentials;
then sign out and test the User portal as `video.demo@example.com` with the
`DEMO_USER_PASSWORD` you supplied. An existing demo account retains its old
password; do not assume reseeding changes it.

Free-service cold starts can delay the first request. A readiness failure must
be resolved before testing app login. Cloud hosting does not make a private
phone IP camera or local robot bridge publicly reachable.

## Paid-plan alternative

On a paid web service, `alembic upgrade head` can be a pre-deploy command, and
the explicit seed command can run in Render Shell after successful startup.
Do not add migrations or demo seeding to every web-worker startup.

## References

- [Render shell availability](https://render.com/docs/ssh)
- [Render deploy and pre-deploy behavior](https://render.com/docs/deploys)
- [Render environment variables and secret files](https://render.com/docs/configure-environment-variables)
- [Aiven TLS and project CA certificates](https://aiven.io/docs/platform/concepts/tls-ssl-certificates)

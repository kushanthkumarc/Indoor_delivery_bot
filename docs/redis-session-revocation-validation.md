# Disposable Redis session-revocation integration tests

Run from the repository root in PowerShell with a working Docker daemon. This
starts an ephemeral Redis 7 container bound only to loopback. It has no mounted
volume and runs with persistence disabled. The test fixture flushes database
15, so use only this disposable container. Never point `TEST_REDIS_URL` at a
shared or production Redis service.

```powershell
$container = "indoor-delivery-redis-validation-" + [guid]::NewGuid().ToString("N")
$dbFile = "phase35_redis_integration_" + [guid]::NewGuid().ToString("N") + ".db"
docker run --rm -d --name $container `
  -p 127.0.0.1:16379:6379 redis:7-alpine `
  redis-server --save "" --appendonly no

try {
  $ready = $false
  for ($i = 0; $i -lt 30; $i++) {
    docker exec $container redis-cli ping
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $ready) { throw "Disposable Redis did not become ready" }

  $env:TEST_REDIS_URL = "redis://127.0.0.1:16379/15"
  $env:DATABASE_URL = "sqlite+aiosqlite:///./$dbFile"
  python -m pytest tests/test_session_revocation_redis.py -q
  if ($LASTEXITCODE -ne 0) { throw "Redis integration suite failed" }
}
finally {
  Remove-Item Env:TEST_REDIS_URL -ErrorAction SilentlyContinue
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  docker rm -f $container 2>$null | Out-Null
  Remove-Item -LiteralPath ".\$dbFile" -ErrorAction SilentlyContinue
}
```

The test module independently rejects any URL other than loopback port `16379`,
database `15`. It covers actual login/refresh/logout and Redis revocation,
single-use and concurrent ticket redemption, fail-closed behavior when a
loopback Redis endpoint is unavailable, and closure of a dashboard socket
after its live Redis session marker is written. Database access is mocked; this
suite does not validate MySQL.

If `docker run` fails, stop here and record the Docker daemon or port conflict;
do not run the module without `TEST_REDIS_URL` and interpret its module skip as
live validation.

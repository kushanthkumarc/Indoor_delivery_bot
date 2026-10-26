# Disposable MySQL migration validation

This procedure validates the Alembic chain against a fresh local MySQL 8
container. It is not a production migration procedure. Use only the disposable
container and the clearly local-only credential below; never substitute a
managed-service URL or production secret.

Run from the repository root in PowerShell:

```powershell
$container = "indoor-delivery-mysql-validation-" + [guid]::NewGuid().ToString("N")
docker run --rm -d --name $container `
  -e MYSQL_ROOT_PASSWORD=local-validation-only `
  -e MYSQL_DATABASE=robot_validation `
  -p 127.0.0.1:33306:3306 mysql:8.0 `
  --log-bin-trust-function-creators=1

try {
  $ready = $false
  for ($i = 0; $i -lt 60; $i++) {
    docker exec $container mysqladmin ping -h 127.0.0.1 -uroot -plocal-validation-only --silent
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
  }
  if (-not $ready) { throw "Disposable MySQL did not become ready" }

  $env:DATABASE_URL = "mysql+aiomysql://root:local-validation-only@127.0.0.1:33306/robot_validation"
  alembic upgrade head
  if ($LASTEXITCODE -ne 0) { throw "Alembic upgrade failed" }
  alembic current
  if ($LASTEXITCODE -ne 0) { throw "Could not inspect Alembic revision" }
  docker exec $container mysql -uroot -plocal-validation-only robot_validation `
    -e "SHOW TRIGGERS; SELECT version_num FROM alembic_version;"
  if ($LASTEXITCODE -ne 0) { throw "Could not verify migration objects" }
}
finally {
  Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
  docker rm -f $container 2>$null | Out-Null
}
```

Expected checks: Alembic reaches `340c2e71c85c`; `SHOW TRIGGERS` lists
`events_no_update` and `events_no_delete`. Repeat from a newly created
container for a clean-schema run. To test an upgrade from an earlier revision,
use a separate disposable database and explicitly verify its schema and data
before and after the upgrade. Do not use the initial migration's destructive
downgrade as an application rollback check.

## Privileges, integrity, backup, and rollback checks

The migration account needs `CREATE`, `ALTER`, `DROP`, `INDEX`, `REFERENCES`,
and `TRIGGER` on the target schema, plus the privileges Alembic needs to
create/read/update `alembic_version` (covered by `CREATE`, `SELECT`, `INSERT`,
and `UPDATE`). MySQL 8 requires `REFERENCES` for the migration's foreign-key
creation. The current chain creates tables, indexes, foreign keys, two
append-only triggers, and the Alembic version table. Keep this account separate
from the runtime application account. Do not grant global privileges for a
staging migration.

When binary logging is enabled and `log_bin_trust_function_creators=0`, MySQL
also requires `SUPER` to create triggers. Avoid granting that broad privilege
to the migration/runtime account. Configure the database service's
`log_bin_trust_function_creators=1` setting through its supported parameter
mechanism before migration, then verify the effective value. The disposable
container command above sets this option only for that isolated test service.

After `upgrade head`, check that every table and expected index exists, both
triggers are attached to `events`, `alembic_version` contains only
`340c2e71c85c`, and `SHOW CREATE TABLE` reports the expected foreign keys and
column types. Attempt disposable `UPDATE` and `DELETE` statements against an
inserted `events` row and verify each fails with SQLSTATE `45000`; then confirm
the row remains. Also run `alembic upgrade head` a second time to verify
idempotent head handling. The initial downgrade drops all six core tables and
their data, so do not treat downgrade as a production rollback strategy.

Before a staging migration, take a database-native backup and verify it can be
restored into a separate disposable schema. For MySQL/InnoDB, an operator can
use this pattern with the staging endpoint and credentials supplied through a
protected client option file (never inline credentials or shell history):

```powershell
mysqldump --defaults-extra-file="$env:MYSQL_BACKUP_CNF" --single-transaction `
  --routines --triggers --events robot_staging `
  --result-file=robot_staging_pre_migration.sql
Get-Content -Raw -LiteralPath robot_staging_pre_migration.sql |
  mysql --defaults-extra-file="$env:MYSQL_RESTORE_CNF" robot_restore_validation
```

`MYSQL_BACKUP_CNF` must point to a protected option file for the source
staging database; `MYSQL_RESTORE_CNF` must target a separate disposable restore
instance/schema. Use an isolated restore target, compare table counts and
critical records, and verify triggers there before approving the migration.
Retain the backup under the organization's protected backup policy. Recovery
is restore-forward from that backup or a tested corrective migration;
downgrading the initial revision is destructive and can lose application data.

The application config requires TLS-verified MySQL URLs in production. The
plain local URL above is intentionally restricted to this disposable-container
validation.

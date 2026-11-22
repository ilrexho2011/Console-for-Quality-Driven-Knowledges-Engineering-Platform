# Scenario Lab — MS SQL Server migrations (QDKEP v14.0)

On-premise SQL Server 2022 (SQL Server 16), fronted by the SAP Cloud
Connector for data retrieval from connected systems. This is the T-SQL
port of the original PostgreSQL design.

## What changed from PostgreSQL

| PostgreSQL | SQL Server (here) |
|---|---|
| `ENUM` types | reference tables `ref_*` + FK |
| `gen_random_uuid()` | `NEWSEQUENTIALID()` default |
| `JSONB` + GIN | `NVARCHAR(MAX)` + `ISJSON()` CHECK; read via `JSON_VALUE` |
| `TIMESTAMPTZ` | `DATETIMEOFFSET(3)` |
| `DISTINCT ON` | `ROW_NUMBER()` window in first-seen view |
| `INSERT ... ON CONFLICT` | `MERGE` (objects) / `NOT EXISTS` (errors) |
| plpgsql trigger | set-based T-SQL trigger |
| `psycopg` | `pyodbc` + ODBC Driver 18 |

## Migration order

Apply V001 → V004 in order. Roll back U004 → U001 in reverse.

| Up | Rollback | Contents |
|---|---|---|
| `V001` | `U001` | schema, `schema_migration` history, `ref_*` reference tables |
| `V002` | `U002` | core tables (source_system, harvest_run, object, error_event, candidate_pair) |
| `V003` | `U003` | indexes + per-system views + first-seen view |
| `V004` | `U004` | atomic-pair seeding trigger |

Each V-script is guarded: it records itself in `scenario_lab.schema_migration`
and skips if already applied, so re-running is safe.

## Running

Set environment, then run the helper:

```bash
set MSSQL_SERVER=LENOVO
set MSSQL_DATABASE=ILRE_CPI
set MSSQL_USER=svc_scenario_lab
set MSSQL_PASSWORD=********        # never commit
set MSSQL_TRUST_CERT=yes           # only if using a self-signed cert on-prem

python run_migrations.py up        # apply all pending
python run_migrations.py down 1    # roll back the most recent migration
```

Or apply manually in SSMS / sqlcmd in the order above (each file already
contains `GO` batch separators).

## Security notes

- Use a least-privilege SQL login for the service (DML on `scn_*`, read on `ref_*`).
- Secrets live in env / secret store only; the runner and repository never log them.
- The Cloud Connector handles connectivity to SAP systems; the database itself
  stays on-prem. Discovery (read) precedes any staging write.

## Python layer

`db/repository_mssql.py` is the SQL Server port of the repository. It reuses
the same `app.models` DTOs and `error_signature` logic as the PostgreSQL
version, so adapters and the FastAPI service are unchanged — only the
persistence driver differs. Point the service at `SqlServerRepository`
built from `build_connection_string(...)`.

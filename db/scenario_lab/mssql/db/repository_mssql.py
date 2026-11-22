"""PostgreSQL -> SQL Server port of the Scenario Lab repository.

Key differences from the psycopg version:
  * pyodbc + ODBC Driver 18 for SQL Server.
  * Parameter marker is '?' (not '%s').
  * payload is NVARCHAR(MAX) JSON text -> json.dumps on write,
    JSON_VALUE in views on read.
  * OUTPUT INSERTED.<col> replaces RETURNING.
  * MERGE replaces INSERT ... ON CONFLICT for idempotent upsert.
  * Schema-qualified names (scenario_lab.<table>); no search_path.

Connection string (from .env) is built for the on-prem SQL Server that
the SAP Cloud Connector fronts. Secrets are never logged.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

import pyodbc

from app.models import (
    HarvestedError,
    HarvestedObject,
    HarvestMode,
    HarvestResult,
    MplSourceMode,
    SystemKind,
)

log = logging.getLogger("scenario_lab.repo.mssql")

SCHEMA = "scenario_lab"


class SqlServerRepository:
    def __init__(self, odbc_connection_string: str):
        self._cs = odbc_connection_string

    @contextmanager
    def _conn(self):
        conn = pyodbc.connect(self._cs, autocommit=False)
        try:
            yield conn
        finally:
            conn.close()

    # -- source systems --------------------------------------------------
    def get_source_system(self, source_system_id: UUID) -> dict | None:
        sql = (
            f"SELECT source_system_id, system_id, customer_id, sap_sid, system_kind "
            f"FROM {SCHEMA}.scn_source_system WHERE source_system_id = ?"
        )
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(sql, str(source_system_id))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def list_source_systems(self) -> list[dict]:
        sql = (
            f"SELECT source_system_id, system_id, customer_id, system_kind, "
            f"is_connected, is_active FROM {SCHEMA}.scn_source_system "
            f"WHERE is_active = 1 ORDER BY system_kind, system_id"
        )
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    # -- harvest run -----------------------------------------------------
    def open_run(
        self,
        source_system_id: UUID,
        mode: HarvestMode,
        triggered_by: str,
        mpl_source_mode: MplSourceMode | None,
    ) -> UUID:
        sql = (
            f"INSERT INTO {SCHEMA}.scn_harvest_run "
            f"(source_system_id, mode, mpl_source_mode, triggered_by, status) "
            f"OUTPUT INSERTED.harvest_run_id "
            f"VALUES (?, ?, ?, ?, 'running')"
        )
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(
                sql,
                str(source_system_id),
                mode.value,
                mpl_source_mode.value if mpl_source_mode else None,
                triggered_by,
            )
            run_id = cur.fetchone()[0]
            c.commit()
            return UUID(str(run_id))

    def close_run(
        self,
        run_id: UUID,
        *,
        status: str,
        objects_fetched: int,
        errors_fetched: int,
        new_errors_count: int,
    ) -> None:
        sql = (
            f"UPDATE {SCHEMA}.scn_harvest_run "
            f"SET status = ?, finished_at = SYSDATETIMEOFFSET(), "
            f"objects_fetched = ?, errors_fetched = ?, new_errors_count = ? "
            f"WHERE harvest_run_id = ?"
        )
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(sql, status, objects_fetched, errors_fetched,
                        new_errors_count, str(run_id))
            c.commit()

    # -- object upsert (MERGE) ------------------------------------------
    def upsert_objects(
        self,
        source_system_id: UUID,
        run_id: UUID,
        kind: SystemKind,
        objects: list[HarvestedObject],
    ) -> int:
        if not objects:
            return 0
        merge = f"""
        MERGE {SCHEMA}.scn_object WITH (HOLDLOCK) AS tgt
        USING (SELECT ? AS source_system_id, ? AS object_name, ? AS object_version) AS src
          ON  tgt.source_system_id = src.source_system_id
          AND tgt.object_name      = src.object_name
          AND tgt.object_version   = src.object_version
        WHEN MATCHED THEN UPDATE SET
            payload = ?, content_hash = ?, last_harvested_at = SYSDATETIMEOFFSET(),
            harvest_run_id = ?
        WHEN NOT MATCHED THEN INSERT
            (source_system_id, harvest_run_id, system_kind, category, object_name,
             interface_id, integration_flow_id, package_id, object_version,
             technology, priority, is_maintained, payload, content_hash)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        with self._conn() as c:
            cur = c.cursor()
            for o in objects:
                payload = json.dumps(o.payload, ensure_ascii=False)
                cur.execute(
                    merge,
                    # USING
                    str(source_system_id), o.object_name, o.object_version,
                    # MATCHED
                    payload, o.hash(), str(run_id),
                    # NOT MATCHED
                    str(source_system_id), str(run_id), kind.value, o.category.value,
                    o.object_name, o.interface_id, o.integration_flow_id, o.package_id,
                    o.object_version, o.technology,
                    o.priority.value if o.priority else None,
                    1 if o.is_maintained else 0, payload, o.hash(),
                )
            c.commit()
            return len(objects)

    # -- error insert (dedup) -------------------------------------------
    def insert_errors(
        self,
        source_system_id: UUID,
        run_id: UUID,
        kind: SystemKind,
        errors: list[HarvestedError],
    ) -> tuple[int, int]:
        if not errors:
            return 0, 0
        exists_sql = (
            f"SELECT TOP 1 1 FROM {SCHEMA}.scn_error_event "
            f"WHERE source_system_id = ? AND error_signature = ?"
        )
        # Insert only if this exact occurrence is not present (dedup).
        insert_sql = f"""
        INSERT INTO {SCHEMA}.scn_error_event
            (source_system_id, harvest_run_id, system_kind, interface_id,
             interface_name, integration_flow_id, message_guid, correlation_id,
             adapter_protocol, http_status_code, exception_class, error_phrase,
             raw_log_excerpt, error_signature, priority, occurred_at)
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM {SCHEMA}.scn_error_event
            WHERE source_system_id = ? AND error_signature = ? AND occurred_at = ?
        );
        """
        inserted = 0
        new_first_seen = 0
        with self._conn() as c:
            cur = c.cursor()
            for e in errors:
                sig = e.signature()
                cur.execute(exists_sql, str(source_system_id), sig)
                already_known = cur.fetchone() is not None
                cur.execute(
                    insert_sql,
                    str(source_system_id), str(run_id), kind.value, e.interface_id,
                    e.interface_name, e.integration_flow_id, e.message_guid,
                    e.correlation_id, e.adapter_protocol, e.http_status_code,
                    e.exception_class, e.error_phrase, e.raw_log_excerpt, sig,
                    e.priority.value if e.priority else None, e.occurred_at,
                    # NOT EXISTS guard
                    str(source_system_id), sig, e.occurred_at,
                )
                if cur.rowcount == 1:
                    inserted += 1
                    if not already_known:
                        new_first_seen += 1
            c.commit()
        return inserted, new_first_seen

    # -- read views ------------------------------------------------------
    def first_seen_errors(self, system_id: str) -> list[dict]:
        sql = (
            f"SELECT interface_name, interface_id, exception_class, error_phrase, "
            f"http_status_code, priority, first_seen_at "
            f"FROM {SCHEMA}.scn_first_seen_errors WHERE system_id = ? "
            f"ORDER BY first_seen_at DESC"
        )
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(sql, system_id)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def new_candidate_pairs(self) -> list[dict]:
        sql = (
            f"SELECT candidate_pair_id, interface_name, error_signature, priority, "
            f"created_at FROM {SCHEMA}.scn_candidate_pair WHERE status = 'new' "
            f"ORDER BY priority, created_at"
        )
        with self._conn() as c:
            cur = c.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_connection_string(
    server: str,
    database: str,
    *,
    user: str | None = None,
    password: str | None = None,
    trusted: bool = False,
    driver: str = "ODBC Driver 18 for SQL Server",
    encrypt: bool = True,
    trust_server_cert: bool = False,
) -> str:
    """Assemble an ODBC connection string. Secrets never logged."""
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
        f"Encrypt={'yes' if encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if trust_server_cert else 'no'}",
    ]
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={user}")
        parts.append(f"PWD={password}")
    return ";".join(parts) + ";"


def persist_harvest(
    repo: SqlServerRepository,
    source_system_id: UUID,
    kind: SystemKind,
    mode: HarvestMode,
    triggered_by: str,
    result: HarvestResult,
) -> dict:
    run_id = repo.open_run(source_system_id, mode, triggered_by, result.mpl_source_mode)
    try:
        obj_n = repo.upsert_objects(source_system_id, run_id, kind, result.objects)
        err_n, new_n = repo.insert_errors(source_system_id, run_id, kind, result.errors)
        repo.close_run(run_id, status="succeeded", objects_fetched=obj_n,
                       errors_fetched=err_n, new_errors_count=new_n)
        return {"run_id": str(run_id), "objects": obj_n, "errors": err_n, "new_errors": new_n}
    except Exception:
        repo.close_run(run_id, status="failed", objects_fetched=0,
                       errors_fetched=0, new_errors_count=0)
        raise

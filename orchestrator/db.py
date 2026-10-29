"""QDKEP DB layer (orchestrator).

Thin persistence over Supabase/Postgres using psycopg2. Handles the write path:
insert a document (dedup gate enforced by the UNIQUE constraint), insert its
chunks (embedding + tsvector), and append audit rows. Connection params come
from the environment, never hardcoded.

Env vars (mirror Supabase connection string):
  PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD   (or DATABASE_URL)
"""
from __future__ import annotations

import os

from platform_b.indexer import to_pgvector_literal, INSERT_CHUNK_SQL


class DuplicateDocument(Exception):
    """Raised when the dedup gate (UNIQUE customer/system/signature) fires."""


def connect():
    """Open a psycopg2 connection from environment configuration.

    Works with both Supabase direct (5432) and pooler (6543) hosts. sslmode is
    forced to 'require' when not already specified, since Supabase requires TLS.
    """
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if url:
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
        sslmode=os.environ.get("PGSSLMODE", "require"),
    )


_INSERT_DOC = """
insert into documents
  (filename, environment, customer_id, system_id, sap_sid, artifact_type,
   object_name, priority, interface_id, integration_flow_id, package_name,
   version, error_signature, quality_score, quality_status, status, raw_text)
values
  (%(filename)s, %(environment)s, %(customer_id)s, %(system_id)s, %(sap_sid)s,
   %(artifact_type)s, %(object_name)s, %(priority)s, %(interface_id)s,
   %(integration_flow_id)s, %(package_name)s, %(version)s, %(error_signature)s,
   %(quality_score)s, %(quality_status)s, %(status)s, %(raw_text)s)
returning id
"""

_INSERT_AUDIT = """
insert into audit_trail (document_id, action, actor, detail)
values (%s, %s, %s, %s)
"""


def insert_document(conn, *, meta: dict, filename: str, signature: str,
                    quality: dict, raw_text: str, status: str = "draft") -> int:
    """Insert a document row. Raises DuplicateDocument on the dedup constraint."""
    import psycopg2
    params = {
        "filename": filename,
        "environment": meta.get("environment"),
        "customer_id": meta.get("customer_id"),
        "system_id": meta.get("system_id"),
        "sap_sid": meta.get("sap_sid"),
        "artifact_type": meta.get("artifact_type"),
        "object_name": meta.get("object_name"),
        "priority": meta.get("priority"),
        "interface_id": meta.get("interface_id"),
        "integration_flow_id": meta.get("integration_flow_id"),
        "package_name": meta.get("package_name"),
        "version": meta.get("version", "v1"),
        "error_signature": signature,
        "quality_score": quality.get("composite"),
        "quality_status": quality.get("status"),
        "status": status,
        "raw_text": raw_text,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_DOC, params)
            doc_id = cur.fetchone()[0]
            cur.execute(_INSERT_AUDIT, (doc_id, "uploaded", "admin", filename))
            cur.execute(_INSERT_AUDIT, (doc_id, "scored", "admin",
                                        f"{quality.get('composite')} {quality.get('status')}"))
        conn.commit()
        return doc_id
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise DuplicateDocument(signature)


def index_chunks(conn, doc_id: int, index_rows) -> int:
    """Insert chunk rows (embedding + tsvector) and mark the document indexed."""
    with conn.cursor() as cur:
        for r in index_rows:
            cur.execute(INSERT_CHUNK_SQL, (
                doc_id, r.chunk_index, r.section_type, r.content,
                to_pgvector_literal(r.embedding), r.content,
            ))
        cur.execute("update documents set status='indexed' where id=%s", (doc_id,))
        cur.execute(_INSERT_AUDIT, (doc_id, "indexed", "admin",
                                    f"{len(index_rows)} chunks"))
    conn.commit()
    return len(index_rows)


# Collection-level index statistics, grouped by artifact_type. "Collections" here
# are logical (one per artifact_type), mirroring the PROD Vector Indexes view.
# points          = chunks belonging to indexed documents of that artifact_type
# indexed_vectors = chunks whose embedding is non-null (vector actually stored)
# documents       = distinct indexed documents
_INDEX_STATS_SQL = """
select
    d.artifact_type                                   as collection,
    count(c.id)                                        as points,
    count(c.embedding)                                 as indexed_vectors,
    count(distinct d.id)                               as documents
from documents d
left join chunks c on c.document_id = d.id
where d.status = 'indexed'
group by d.artifact_type
order by points desc
"""


def index_stats(conn) -> list[dict]:
    """Return per-collection (artifact_type) index health rows."""
    with conn.cursor() as cur:
        cur.execute(_INDEX_STATS_SQL)
        rows = cur.fetchall()
    out = []
    for collection, points, vectors, docs in rows:
        out.append({
            "collection": f"qdkep_{collection}",
            "artifact_type": collection,
            "documents": int(docs),
            "points": int(points),
            "indexed_vectors": int(vectors),
            "status": "green" if points > 0 else "empty",
        })
    return out


_DASHBOARD_SQL = """
select
    count(*)                                                   as total,
    count(*) filter (where status = 'draft')                   as draft,
    count(*) filter (where status = 'approved')                as approved,
    count(*) filter (where status = 'indexed')                 as indexed,
    count(*) filter (where artifact_type = 'kedb')             as kedb_total,
    count(*) filter (where artifact_type = 'kedb'
                     and status = 'indexed')                   as kedb_indexed,
    count(*) filter (where artifact_type = 'runbook')          as rb_total,
    count(*) filter (where artifact_type = 'runbook'
                     and status = 'indexed')                   as rb_indexed,
    coalesce(round(avg(quality_score), 1), 0)                  as avg_quality
from documents
"""


def dashboard_stats(conn) -> dict:
    """Platform overview counters for the Dashboard page."""
    with conn.cursor() as cur:
        cur.execute(_DASHBOARD_SQL)
        r = cur.fetchone()
    keys = ("total", "draft", "approved", "indexed", "kedb_total",
            "kedb_indexed", "rb_total", "rb_indexed", "avg_quality")
    return {k: (float(v) if k == "avg_quality" else int(v)) for k, v in zip(keys, r)}


_LIST_DOCS_SQL = """
select id, filename, artifact_type, customer_id, system_id, priority,
       version, quality_score, quality_status, status, created_at
from documents
order by created_at desc
limit %s offset %s
"""


def list_documents(conn, limit: int = 50, offset: int = 0) -> list[dict]:
    """List documents (most recent first) for the Documents page."""
    with conn.cursor() as cur:
        cur.execute(_LIST_DOCS_SQL, (limit, offset))
        rows = cur.fetchall()
    cols = ("id", "filename", "artifact_type", "customer_id", "system_id",
            "priority", "version", "quality_score", "quality_status",
            "status", "created_at")
    out = []
    for row in rows:
        d = dict(zip(cols, row))
        d["quality_score"] = float(d["quality_score"]) if d["quality_score"] is not None else None
        d["created_at"] = str(d["created_at"])
        out.append(d)
    return out


def document_detail(conn, doc_id: int) -> dict | None:
    """Full metadata + chunk count + audit trail for one document."""
    with conn.cursor() as cur:
        cur.execute("""
            select id, filename, environment, customer_id, system_id, sap_sid,
                   artifact_type, object_name, priority, interface_id,
                   integration_flow_id, package_name, version, error_signature,
                   quality_score, quality_status, status, created_at
            from documents where id = %s
        """, (doc_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = ("id", "filename", "environment", "customer_id", "system_id",
                "sap_sid", "artifact_type", "object_name", "priority",
                "interface_id", "integration_flow_id", "package_name", "version",
                "error_signature", "quality_score", "quality_status", "status",
                "created_at")
        doc = dict(zip(cols, row))
        doc["quality_score"] = float(doc["quality_score"]) if doc["quality_score"] is not None else None
        doc["created_at"] = str(doc["created_at"])

        cur.execute("select count(*) from chunks where document_id = %s", (doc_id,))
        doc["chunks"] = int(cur.fetchone()[0])

        cur.execute("""
            select action, actor, detail, created_at
            from audit_trail where document_id = %s order by created_at asc
        """, (doc_id,))
        doc["audit"] = [
            {"action": a, "actor": ac, "detail": d, "at": str(ts)}
            for a, ac, d, ts in cur.fetchall()
        ]
    return doc


def delete_document(conn, doc_id: int) -> bool:
    """Delete a document and its chunks (cascade) and audit rows. Returns True
    if a row was removed."""
    with conn.cursor() as cur:
        cur.execute("delete from documents where id = %s", (doc_id,))
        removed = cur.rowcount
    conn.commit()
    return removed > 0

"""QDKEP hybrid retrieval + 8-signal reranking (Faza 3).

Combines two retrieval channels over the `chunks` table in Supabase:
  - semantic: pgvector cosine distance on the query embedding
  - lexical : tsvector ts_rank (BM25-like keyword overlap)

Candidates are fused, then reranked by an 8-signal scorer that boosts chunks
whose section_type carries high weight (error_signature, diagnosis_step,
recovery) and rewards literal evidence overlap with the query.

Hard filters (customer_id, system_id, interface_id) are applied in SQL so they
never leak across customers. artifact_type ordering follows the retrieval policy:
kedb -> runbook -> architecture/interface_inventory -> global.

Pure stdlib + psycopg2 at the call site; the SQL is parameterized.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .indexer import embed_texts, to_pgvector_literal

# Reranking weights per section_type (AI4BIZ Ingestion Guide, 8-signal engine).
SECTION_WEIGHTS = {
    "error_signature": 1.0,
    "diagnosis_step": 0.9,
    "recovery": 0.9,
    "escalation": 0.6,
    "context": 0.3,
    "generic": 0.1,
}

# Retrieval order by artifact_type (lower rank = preferred).
ARTIFACT_ORDER = {
    "kedb": 0,
    "runbook": 1,
    "architecture": 2,
    "interface_inventory": 2,
}
_DEFAULT_ORDER = 3  # global / everything else

# Fusion weights for the two channels before reranking.
W_SEMANTIC = 0.6
W_LEXICAL = 0.4


@dataclass
class Candidate:
    document_id: int
    chunk_index: int
    section_type: str
    artifact_type: str
    content: str
    semantic_score: float = 0.0
    lexical_score: float = 0.0
    fused_score: float = 0.0
    final_score: float = 0.0


# ---------------------------------------------------------------------------
# SQL. Two CTEs (semantic + lexical), fused with a full outer join so a chunk
# that ranks in either channel survives. Hard filters injected via params.
# ---------------------------------------------------------------------------
HYBRID_SQL = """
with sem as (
    select c.id, c.document_id, c.chunk_index, c.section_type, c.content,
           d.artifact_type,
           1 - (c.embedding <=> %(qvec)s::vector) as semantic_score
    from chunks c
    join documents d on d.id = c.document_id
    where d.status = 'indexed'
      and (%(customer_id)s is null or d.customer_id = %(customer_id)s)
      and (%(system_id)s   is null or d.system_id   = %(system_id)s)
      and (%(interface_id)s is null or d.interface_id = %(interface_id)s)
      and (%(artifact_type)s is null or d.artifact_type = %(artifact_type)s)
    order by c.embedding <=> %(qvec)s::vector
    limit %(pool)s
),
lex as (
    select c.id, c.document_id, c.chunk_index, c.section_type, c.content,
           d.artifact_type,
           ts_rank(c.tsv, plainto_tsquery('english', %(qtext)s)) as lexical_score
    from chunks c
    join documents d on d.id = c.document_id
    where d.status = 'indexed'
      and c.tsv @@ plainto_tsquery('english', %(qtext)s)
      and (%(customer_id)s is null or d.customer_id = %(customer_id)s)
      and (%(system_id)s   is null or d.system_id   = %(system_id)s)
      and (%(interface_id)s is null or d.interface_id = %(interface_id)s)
      and (%(artifact_type)s is null or d.artifact_type = %(artifact_type)s)
    order by lexical_score desc
    limit %(pool)s
)
select
    coalesce(sem.document_id, lex.document_id)   as document_id,
    coalesce(sem.chunk_index, lex.chunk_index)   as chunk_index,
    coalesce(sem.section_type, lex.section_type) as section_type,
    coalesce(sem.artifact_type, lex.artifact_type) as artifact_type,
    coalesce(sem.content, lex.content)           as content,
    coalesce(sem.semantic_score, 0)              as semantic_score,
    coalesce(lex.lexical_score, 0)               as lexical_score
from sem
full outer join lex on sem.id = lex.id
"""


def build_query_params(query_text: str, *, query_embedding: list[float],
                       customer_id=None, system_id=None, interface_id=None,
                       artifact_type=None, pool: int = 50) -> dict:
    """Assemble the parameter dict for HYBRID_SQL."""
    return {
        "qtext": query_text,
        "qvec": to_pgvector_literal(query_embedding),
        "customer_id": customer_id,
        "system_id": system_id,
        "interface_id": interface_id,
        "artifact_type": artifact_type,
        "pool": pool,
    }


def _normalize(rows, key):
    """Min-max normalize a score column to 0..1 (stable when all equal)."""
    vals = [getattr(r, key) for r in rows]
    lo, hi = min(vals, default=0.0), max(vals, default=0.0)
    if hi - lo < 1e-9:
        for r in rows:
            setattr(r, key, 1.0 if hi > 0 else 0.0)
        return
    for r in rows:
        setattr(r, key, (getattr(r, key) - lo) / (hi - lo))


def _evidence_overlap(query_text: str, content: str) -> float:
    """Fraction of distinctive query tokens (codes, CamelCase, UPPER) present
    literally in the chunk — rewards copied log text matches."""
    toks = set(re.findall(r'[A-Z][A-Za-z0-9_]{2,}|\b\d{3,}\b|[A-Z0-9_]{4,}', query_text))
    if not toks:
        return 0.0
    hit = sum(1 for t in toks if t in content)
    return hit / len(toks)


def rerank(query_text: str, rows: list[Candidate], top_k: int = 5) -> list[Candidate]:
    """Fuse the two channels, then apply the 8-signal rerank."""
    if not rows:
        return []
    _normalize(rows, "semantic_score")
    _normalize(rows, "lexical_score")

    for r in rows:
        r.fused_score = W_SEMANTIC * r.semantic_score + W_LEXICAL * r.lexical_score
        section_w = SECTION_WEIGHTS.get(r.section_type, 0.1)
        artifact_boost = 1.0 - 0.05 * ARTIFACT_ORDER.get(r.artifact_type, _DEFAULT_ORDER)
        evidence = _evidence_overlap(query_text, r.content)
        # Final = fused relevance, scaled by section weight + artifact preference,
        # plus a literal-evidence bonus (the 8th signal).
        r.final_score = round(
            r.fused_score * section_w * artifact_boost + 0.15 * evidence, 6
        )

    rows.sort(key=lambda r: r.final_score, reverse=True)
    return rows[:top_k]


def retrieve(cursor, query_text: str, *, embedder=None, top_k: int = 5,
             customer_id=None, system_id=None, interface_id=None,
             artifact_type=None, pool: int = 50) -> list[Candidate]:
    """Run the full hybrid retrieval against an open DB cursor.

    `cursor` is a psycopg2 cursor. `embedder` may be injected for tests.
    """
    qvec = embed_texts([query_text], embedder=embedder)[0]
    params = build_query_params(
        query_text, query_embedding=qvec, customer_id=customer_id,
        system_id=system_id, interface_id=interface_id,
        artifact_type=artifact_type, pool=pool,
    )
    cursor.execute(HYBRID_SQL, params)
    rows = [
        Candidate(
            document_id=row[0], chunk_index=row[1], section_type=row[2],
            artifact_type=row[3], content=row[4],
            semantic_score=float(row[5]), lexical_score=float(row[6]),
        )
        for row in cursor.fetchall()
    ]
    return rerank(query_text, rows, top_k=top_k)

"""QDKEP endpoint handlers (orchestrator).

Pure functions that take parsed input + dependencies and return (status, dict).
Kept separate from the HTTP layer so they unit-test without a live server.
The full ingestion path runs here: parse -> signature -> quality gate ->
dedup insert -> chunk -> embed -> index.
"""
from __future__ import annotations

from platform_a.parser import parse_metadata, canonical_signature, extract_error_pattern
from platform_a.chunker import chunk_document
from platform_b.quality import score_document
from platform_b.indexer import build_index_rows
from platform_b.answerer import search as do_search, answer as do_answer

MIN_INDEX_SCORE = 60.0  # below this the document is rejected, not indexed.


def handle_upload(conn, *, filename: str, raw_text: str, embedder=None,
                  db=None, chunk_mode: str = "bm25_typed",
                  chunk_size=None, chunk_overlap=None) -> tuple[int, dict]:
    """Full ingestion: score, dedup-insert, chunk, embed, index."""
    from orchestrator import db as dbmod
    db = db or dbmod

    meta = parse_metadata(raw_text)
    signature = canonical_signature(
        meta.get("system_id", ""), meta.get("object_name", ""),
        extract_error_pattern(raw_text),
    )
    quality = score_document(raw_text)

    if quality["composite"] < MIN_INDEX_SCORE:
        return 422, {
            "status": "rejected",
            "reason": "quality below threshold",
            "quality": quality["composite"],
            "quality_status": quality["status"],
            "issues": quality["issues"],
        }

    try:
        doc_id = db.insert_document(
            conn, meta=meta, filename=filename, signature=signature,
            quality=quality, raw_text=raw_text, status="draft",
        )
    except db.DuplicateDocument:
        return 409, {"status": "duplicate", "signature": signature}

    rows = build_index_rows(
        chunk_document(raw_text, mode=chunk_mode, chunk_size=chunk_size,
                       overlap=chunk_overlap),
        embedder=embedder,
    )
    n = db.index_chunks(conn, doc_id, rows)

    return 201, {
        "status": "indexed",
        "document_id": doc_id,
        "signature": signature,
        "quality": quality["composite"],
        "quality_status": quality["status"],
        "categories": quality["categories"],
        "issues": quality["issues"],
        "metadata": quality["metadata"],
        "chunk_mode": chunk_mode,
        "chunks": n,
    }


def handle_search(conn, *, query: str, top_k: int = 5, embedder=None,
                  **filters) -> tuple[int, dict]:
    if not query:
        return 400, {"error": "query required"}
    with conn.cursor() as cur:
        hits = do_search(cur, query, embedder=embedder, top_k=top_k, **filters)
    return 200, {"query": query, "hits": [h.__dict__ for h in hits]}


def handle_answer(conn, *, query: str, generate, top_k: int = 5, embedder=None,
                  **filters) -> tuple[int, dict]:
    if not query:
        return 400, {"error": "query required"}
    with conn.cursor() as cur:
        result = do_answer(cur, query, generate=generate, embedder=embedder,
                           top_k=top_k, **filters)
    return 200, {
        "query": query,
        "answer": result.answer,
        "citations": result.citations,
        "used_chunks": result.used_chunks,
    }


def handle_indexes(conn, *, db=None) -> tuple[int, dict]:
    """Collection-level index health, grouped by artifact_type."""
    from orchestrator import db as dbmod
    db = db or dbmod
    stats = db.index_stats(conn)
    totals = {
        "documents": sum(s["documents"] for s in stats),
        "points": sum(s["points"] for s in stats),
        "indexed_vectors": sum(s["indexed_vectors"] for s in stats),
    }
    return 200, {"collections": stats, "totals": totals}


def handle_dashboard(conn, *, db=None) -> tuple[int, dict]:
    """Platform overview counters for the Dashboard page."""
    from orchestrator import db as dbmod
    db = db or dbmod
    return 200, db.dashboard_stats(conn)


def handle_documents(conn, *, limit: int = 50, offset: int = 0, db=None) -> tuple[int, dict]:
    """List documents for the Documents page."""
    from orchestrator import db as dbmod
    db = db or dbmod
    docs = db.list_documents(conn, limit=limit, offset=offset)
    return 200, {"documents": docs, "count": len(docs)}


def handle_document_detail(conn, doc_id: int, *, db=None) -> tuple[int, dict]:
    """Full detail (metadata + audit + chunks) for one document."""
    from orchestrator import db as dbmod
    db = db or dbmod
    doc = db.document_detail(conn, doc_id)
    if doc is None:
        return 404, {"error": "document not found", "id": doc_id}
    return 200, doc


def handle_delete_document(conn, doc_id: int, *, db=None) -> tuple[int, dict]:
    """Delete a document by id."""
    from orchestrator import db as dbmod
    db = db or dbmod
    ok = db.delete_document(conn, doc_id)
    if not ok:
        return 404, {"error": "document not found", "id": doc_id}
    return 200, {"status": "deleted", "id": doc_id}


def handle_benchmark(conn, *, query_set, generate, embedder=None,
                     static_categories=None) -> tuple[int, dict]:
    """Run the dynamic benchmark over a query-set; optionally fold in static
    categories to produce the full 7-category composite."""
    from platform_b.benchmark import evaluate_dynamic, full_benchmark
    if not query_set:
        return 400, {"error": "query_set required"}
    with conn.cursor() as cur:
        dyn = evaluate_dynamic(cur, query_set, generate=generate, embedder=embedder)
    payload = {
        "dynamic": {
            "retrieval_precision": dyn.retrieval_precision,
            "retrieval_recall": dyn.retrieval_recall,
            "answer_faithfulness": dyn.answer_faithfulness,
            "citation_accuracy": dyn.citation_accuracy,
        },
        "per_query": dyn.per_query,
    }
    if static_categories:
        payload["full"] = full_benchmark(static_categories, dyn)
    return 200, payload

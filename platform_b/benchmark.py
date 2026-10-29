"""QDKEP dynamic quality scoring (Faza 4) — completes the 7-category model.

Static categories (Faza 1) score one document in isolation. Dynamic categories
require a query-set with ground-truth relevant documents, a live index, the
retrieved contexts, and the generated answers:

  Retrieval Precision        20%  — of retrieved, how many are relevant
  Retrieval Recall           15%  — of relevant, how many were retrieved
  Answer Faithfulness        10%  — answer claims grounded in retrieved context
  Citation / Evidence Acc.    5%  — cited refs point to truly relevant chunks

Combined with the three static categories (0.50) these sum to 1.00, so the full
benchmark composite reaches the 0-100 enterprise-readiness scale.

A query-set entry:
  {"query": "...", "filters": {...}, "relevant_doc_ids": [1, 7], "top_k": 5}
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .retriever import retrieve
from .answerer import answer

FULL_WEIGHTS = {
    "ingestion_policy_compliance": 0.20,
    "bm25_authoring_readiness": 0.15,
    "chunking_quality": 0.15,
    "retrieval_precision": 0.20,
    "retrieval_recall": 0.15,
    "answer_faithfulness": 0.10,
    "citation_accuracy": 0.05,
}

_CITE = re.compile(r'\[(\d+)\]')


def precision_recall(retrieved_doc_ids: list[int], relevant: set[int]) -> tuple[float, float]:
    """Precision and recall (0..1) for one query's retrieved doc ids."""
    if not retrieved_doc_ids:
        return 0.0, 0.0
    retrieved = list(dict.fromkeys(retrieved_doc_ids))  # de-dup, keep order
    hits = sum(1 for d in retrieved if d in relevant)
    precision = hits / len(retrieved)
    recall = hits / len(relevant) if relevant else 0.0
    return precision, recall


def faithfulness(answer_text: str, contexts: list[str]) -> float:
    """Fraction of distinctive answer tokens that appear in the retrieved context.

    A lightweight, deterministic proxy for grounding: codes, CamelCase, and
    UPPER_SNAKE tokens in the answer should be traceable to the context.
    """
    toks = set(re.findall(r'[A-Z][A-Za-z0-9_]{2,}|\b\d{3,}\b|[A-Z0-9_]{4,}', answer_text))
    # Drop pure citation markers like "1" captured as digits.
    toks = {t for t in toks if not t.isdigit() or len(t) >= 3}
    if not toks:
        return 1.0  # no checkable claims -> not penalized
    blob = " ".join(contexts)
    grounded = sum(1 for t in toks if t in blob)
    return grounded / len(toks)


def citation_accuracy(answer_text: str, citations: list[dict],
                      relevant: set[int]) -> float:
    """Of the refs actually cited in the answer, fraction pointing to a relevant doc."""
    cited_refs = {int(m) for m in _CITE.findall(answer_text)}
    if not cited_refs:
        return 0.0
    by_ref = {c["ref"]: c["document_id"] for c in citations}
    good = sum(1 for r in cited_refs if by_ref.get(r) in relevant)
    return good / len(cited_refs)


@dataclass
class DynamicReport:
    retrieval_precision: float
    retrieval_recall: float
    answer_faithfulness: float
    citation_accuracy: float
    per_query: list[dict]


def evaluate_dynamic(cursor, query_set: list[dict], *, generate, embedder=None) -> DynamicReport:
    """Run the query-set through retrieval + answering and average the metrics."""
    p_acc = r_acc = f_acc = c_acc = 0.0
    per_query = []
    n = max(len(query_set), 1)

    for q in query_set:
        relevant = set(q.get("relevant_doc_ids", []))
        filters = q.get("filters", {})
        top_k = q.get("top_k", 5)

        cands = retrieve(cursor, q["query"], embedder=embedder, top_k=top_k, **filters)
        retrieved_ids = [c.document_id for c in cands]
        contexts = [c.content for c in cands]

        p, r = precision_recall(retrieved_ids, relevant)
        res = answer(cursor, q["query"], generate=generate, embedder=embedder,
                     top_k=top_k, **filters)
        f = faithfulness(res.answer, contexts)
        c = citation_accuracy(res.answer, res.citations, relevant)

        p_acc += p; r_acc += r; f_acc += f; c_acc += c
        per_query.append({
            "query": q["query"], "precision": round(p, 3), "recall": round(r, 3),
            "faithfulness": round(f, 3), "citation_accuracy": round(c, 3),
            "retrieved": retrieved_ids,
        })

    return DynamicReport(
        retrieval_precision=round(p_acc / n * 100, 2),
        retrieval_recall=round(r_acc / n * 100, 2),
        answer_faithfulness=round(f_acc / n * 100, 2),
        citation_accuracy=round(c_acc / n * 100, 2),
        per_query=per_query,
    )


def _status(score: float) -> str:
    if score >= 90: return "enterprise_ready"
    if score >= 80: return "ready_minor"
    if score >= 70: return "conditional"
    if score >= 60: return "needs_remediation"
    return "not_ready"


def full_benchmark(static_categories: dict, dynamic: DynamicReport) -> dict:
    """Combine static (Faza 1) + dynamic (Faza 4) into the 7-category composite."""
    cats = {
        "ingestion_policy_compliance": static_categories["ingestion_policy_compliance"],
        "bm25_authoring_readiness": static_categories["bm25_authoring_readiness"],
        "chunking_quality": static_categories["chunking_quality"],
        "retrieval_precision": dynamic.retrieval_precision,
        "retrieval_recall": dynamic.retrieval_recall,
        "answer_faithfulness": dynamic.answer_faithfulness,
        "citation_accuracy": dynamic.citation_accuracy,
    }
    composite = round(sum(cats[k] * FULL_WEIGHTS[k] for k in cats), 2)
    return {"categories": cats, "composite": composite, "status": _status(composite)}

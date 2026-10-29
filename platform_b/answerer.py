"""QDKEP search + AMS-oriented answering (Faza 3).

Wraps hybrid retrieval into two operator-facing surfaces:
  - search(): returns reranked chunks with provenance for the Search page.
  - answer(): builds a grounded AMS answer from the top-K chunks, with inline
    citations back to (document_id, chunk_index, section_type).

The answer generator is provider-agnostic: pass a `generate` callable
(prompt -> str). In production wire it to the Anthropic API; in tests inject a
stub. The retrieved context is never bypassed — answers must cite retrieved
chunks, matching the closed-loop "answer from indexed knowledge" model.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .retriever import retrieve, Candidate


@dataclass
class SearchHit:
    rank: int
    document_id: int
    chunk_index: int
    section_type: str
    artifact_type: str
    score: float
    snippet: str


def search(cursor, query_text: str, *, embedder=None, top_k: int = 5, **filters) -> list[SearchHit]:
    """Run retrieval and return ranked hits for the Search surface."""
    cands = retrieve(cursor, query_text, embedder=embedder, top_k=top_k, **filters)
    hits = []
    for i, c in enumerate(cands, start=1):
        snippet = c.content.strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "..."
        hits.append(SearchHit(
            rank=i, document_id=c.document_id, chunk_index=c.chunk_index,
            section_type=c.section_type, artifact_type=c.artifact_type,
            score=round(c.final_score, 4), snippet=snippet,
        ))
    return hits


def _build_prompt(query_text: str, cands: list[Candidate]) -> str:
    """Compose a grounded AMS prompt with numbered, citable context blocks."""
    blocks = []
    for i, c in enumerate(cands, start=1):
        blocks.append(
            f"[{i}] (doc {c.document_id}, chunk {c.chunk_index}, "
            f"{c.section_type}/{c.artifact_type})\n{c.content.strip()}"
        )
    context = "\n\n".join(blocks)
    return (
        "You are an SAP AMS knowledge assistant. Answer the operator's question "
        "using ONLY the numbered context below. Cite sources inline as [n]. If the "
        "context does not contain the answer, say so plainly. Keep it operational "
        "and action-led.\n\n"
        f"QUESTION:\n{query_text}\n\nCONTEXT:\n{context}\n\nANSWER:"
    )


@dataclass
class AnswerResult:
    answer: str
    citations: list[dict]
    used_chunks: int


def answer(cursor, query_text: str, *, generate, embedder=None, top_k: int = 5,
           **filters) -> AnswerResult:
    """Retrieve top-K, build a grounded prompt, and generate an AMS answer.

    `generate` is a callable (prompt: str) -> str. No retrieval -> no answer:
    if nothing is indexed/matched, return an explicit not-found result.
    """
    cands = retrieve(cursor, query_text, embedder=embedder, top_k=top_k, **filters)
    if not cands:
        return AnswerResult(
            answer="No indexed knowledge matches this question for the given filters.",
            citations=[], used_chunks=0,
        )
    prompt = _build_prompt(query_text, cands)
    text = generate(prompt)
    citations = [
        {"ref": i, "document_id": c.document_id, "chunk_index": c.chunk_index,
         "section_type": c.section_type, "artifact_type": c.artifact_type}
        for i, c in enumerate(cands, start=1)
    ]
    return AnswerResult(answer=text, citations=citations, used_chunks=len(cands))

"""QDKEP indexing (Faza 2): local embeddings + pgvector/tsvector rows.

Embeds each chunk with a local sentence-transformers model (no API, no cost).
Produces insert-ready rows for the `chunks` table: embedding (vector(384)) and
the lexical content that Postgres turns into tsvector on insert.

The embedder is loaded lazily and cached, so importing this module is cheap and
unit tests can inject a fake embedder.
"""
from __future__ import annotations

from dataclasses import dataclass

from platform_a.chunker import ChunkSet

MODEL_NAME = "all-MiniLM-L6-v2"   # 384-dim, fast, fully local
EMBED_DIM = 384

_model = None


def _get_model():
    """Lazily load and cache the sentence-transformers model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_texts(texts: list[str], embedder=None) -> list[list[float]]:
    """Embed a list of texts to 384-dim float vectors.

    `embedder` may be injected (a callable taking list[str] -> list[list[float]])
    to avoid loading the real model in tests.
    """
    if not texts:
        return []
    if embedder is not None:
        return embedder(texts)
    model = _get_model()
    vecs = model.encode(texts, normalize_embeddings=True)
    return [v.tolist() for v in vecs]


@dataclass
class IndexRow:
    chunk_index: int
    section_type: str
    content: str
    embedding: list[float]


def build_index_rows(chunkset: ChunkSet, embedder=None) -> list[IndexRow]:
    """Embed all chunks and return insert-ready rows for the `chunks` table."""
    texts = [c.content for c in chunkset.chunks]
    vecs = embed_texts(texts, embedder=embedder)
    rows: list[IndexRow] = []
    for chunk, vec in zip(chunkset.chunks, vecs):
        rows.append(IndexRow(
            chunk_index=chunk.chunk_index,
            section_type=chunk.section_type,
            content=chunk.content,
            embedding=vec,
        ))
    return rows


def to_pgvector_literal(vec: list[float]) -> str:
    """Format a vector as a pgvector literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# SQL template: tsv is computed server-side from content so the lexical (BM25-like)
# layer and the vector layer stay in one INSERT. %s order:
# (document_id, chunk_index, section_type, content, embedding_literal)
INSERT_CHUNK_SQL = """
insert into chunks (document_id, chunk_index, section_type, content, embedding, tsv)
values (%s, %s, %s, %s, %s::vector, to_tsvector('english', %s))
"""

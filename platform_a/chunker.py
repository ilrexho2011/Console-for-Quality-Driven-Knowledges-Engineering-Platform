"""QDKEP heading-aware chunking (Faza 2).

Splits an atomic KB/RB/DOC document along its section headings, then assigns a
section_type to each chunk. section_type drives the 8-signal BM25 reranking
(error_signature, diagnosis_step, recovery, escalation, context, generic).

Heading-aware (not fixed-size) is mandatory for normative operational docs:
the AI4BIZ Knowledge Ingestion Guide maps section names to reranking weights.
Pure stdlib.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parser import parse_metadata

_HEADING = re.compile(r'^\s*#{1,6}\s+(.*?)\s*$')

# Map a (normalized, upper) heading to its BM25 section_type. Covers RB, KB, DOC
# heading vocabularies. Matching is substring-based so "5. ERROR SIGNATURE" and
# "ERROR PATTERN" both resolve correctly.
_SECTION_MAP = (
    ("ERROR SIGNATURE", "error_signature"),
    ("ERROR PATTERN", "error_signature"),
    ("SYMPTOM", "error_signature"),
    ("DIAGNOSIS STEPS", "diagnosis_step"),
    ("DIAGNOSIS SIGNALS", "diagnosis_step"),
    ("DIAGNOSIS", "diagnosis_step"),
    ("RECOVERY STEPS", "recovery"),
    ("RECOVERY", "recovery"),
    ("RESOLUTION", "recovery"),
    ("WORKAROUND", "recovery"),
    ("POST-FIX VALIDATION", "recovery"),
    ("VALIDATION", "recovery"),
    ("ESCALATION CRITERIA", "escalation"),
    ("ESCALATION", "escalation"),
    ("ROOT CAUSE", "context"),
    ("KNOWN ROOT CAUSES", "context"),
    ("BUSINESS CONTEXT", "context"),
    ("TECHNICAL LANDSCAPE", "context"),
    ("MONITORING AND MPL EVIDENCE", "context"),
    ("CORE EXPLANATION", "context"),
    ("OPERATIONAL RELEVANCE", "context"),
    ("SCOPE", "context"),
    ("KEY TERMS", "context"),
    ("PURPOSE", "context"),
    ("PREREQUISITES", "context"),
)


def classify_section(heading: str) -> str:
    """Map a heading string to its BM25 section_type."""
    h = re.sub(r'^\d+\.\s*', '', heading.strip().upper())
    for needle, stype in _SECTION_MAP:
        if needle in h:
            return stype
    return "generic"


@dataclass
class Chunk:
    chunk_index: int
    section_type: str
    heading: str
    content: str


@dataclass
class ChunkSet:
    metadata: dict = field(default_factory=dict)
    chunks: list[Chunk] = field(default_factory=list)


# Default (size, overlap) per chunk mode — mirrors the PROD upload profiles.
CHUNK_PROFILES = {
    "bm25_typed": (400, 80),
    "procedure":  (400, 80),
    "paragraph":  (800, 120),
    "fixed":      (1000, 100),
}


def profile_defaults(mode: str) -> tuple[int, int]:
    return CHUNK_PROFILES.get(mode, CHUNK_PROFILES["bm25_typed"])


def chunk_document(text: str, max_chars: int = 1200, *,
                   mode: str = "bm25_typed", chunk_size: int | None = None,
                   overlap: int | None = None) -> ChunkSet:
    """Split a document into chunks.

    mode:
      bm25_typed / procedure  -> heading-aware, section_type preserved (recommended
                                  for KB/RB; honours the sacred-heading structure)
      paragraph               -> heading-aware, split on paragraph boundaries
      fixed                   -> ignore headings, fixed-size windows with overlap

    chunk_size / overlap default to the profile for the mode when not given.
    """
    size, ov = profile_defaults(mode)
    if chunk_size is not None:
        size = chunk_size
    if overlap is not None:
        ov = overlap

    if mode == "fixed":
        return _chunk_fixed(text, size, ov)

    meta = parse_metadata(text)
    lines = text.splitlines()

    # Group lines into (heading, body_lines) sections.
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_body: list[str] = []
    for line in lines:
        m = _HEADING.match(line)
        if m:
            if current_heading or current_body:
                sections.append((current_heading, current_body))
            current_heading = m.group(1).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_heading or current_body:
        sections.append((current_heading, current_body))

    chunks: list[Chunk] = []
    idx = 0
    for heading, body in sections:
        body_text = "\n".join(body).strip()
        if not body_text:
            continue
        stype = classify_section(heading) if heading else "generic"
        for piece in _split_oversize(body_text, size):
            chunks.append(Chunk(idx, stype, heading, piece))
            idx += 1

    return ChunkSet(metadata=meta, chunks=chunks)


def _chunk_fixed(text: str, size: int, overlap: int) -> ChunkSet:
    """Fixed-size sliding-window chunks (no heading awareness)."""
    meta = parse_metadata(text)
    body = text.strip()
    step = max(size - overlap, 1)
    chunks: list[Chunk] = []
    idx = 0
    for start in range(0, max(len(body), 1), step):
        piece = body[start:start + size].strip()
        if piece:
            chunks.append(Chunk(idx, "generic", "", piece))
            idx += 1
        if start + size >= len(body):
            break
    return ChunkSet(metadata=meta, chunks=chunks)


def _split_oversize(body: str, max_chars: int) -> list[str]:
    """Split a section body on blank lines / bullets to respect max_chars."""
    if len(body) <= max_chars:
        return [body]
    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    for para in re.split(r'\n\s*\n', body):
        if size + len(para) > max_chars and buf:
            pieces.append("\n\n".join(buf).strip())
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 2
    if buf:
        pieces.append("\n\n".join(buf).strip())
    return [p for p in pieces if p]

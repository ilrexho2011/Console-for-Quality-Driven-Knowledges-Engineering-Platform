"""QDKEP static quality scoring (Faza 1).

Three static categories, computed at upload-time without an index or LLM:
  - Ingestion Policy Compliance  (weight 0.20 / 0.50 of static)
  - BM25 Authoring Readiness     (weight 0.15 / 0.375 of static)
  - Chunking Quality             (weight 0.15 / 0.375 of static)

Each category returns 0-100. The static composite renormalizes the three
weights (0.20+0.15+0.15=0.50) to a 0-100 scale, so a perfect static document
can reach 100 even before dynamic (retrieval) categories exist.

Status thresholds (applied to the static composite in Faza 1):
  90-100 enterprise_ready | 80-89 ready_minor | 70-79 conditional
  60-69  needs_remediation | <60 not_ready
"""
from __future__ import annotations

from platform_a.parser import (
    MANDATORY_FIELDS, ALLOWED_ARTIFACT_TYPES,
    parse_metadata, parse_headings, profile_for,
)

STATIC_WEIGHTS = {"policy": 0.20, "bm25": 0.15, "chunking": 0.15}
_STATIC_TOTAL = sum(STATIC_WEIGHTS.values())  # 0.50

_VAGUE = ("something went wrong", "system issue", "may occur", "sometimes",
          "various", "issues may", "related components")
_ACTION_VERBS = ("check", "verify", "confirm", "compare", "replay",
                 "restart", "restore", "escalate", "inspect", "validate")


def score_policy_compliance(text: str, meta: dict) -> tuple[float, list[str]]:
    """Ingestion-policy: mandatory metadata, allowed artifact_type, single-artifact."""
    issues: list[str] = []
    present = sum(1 for f in MANDATORY_FIELDS if meta.get(f))
    meta_score = present / len(MANDATORY_FIELDS) * 60
    for f in MANDATORY_FIELDS:
        if not meta.get(f):
            issues.append(f"missing mandatory metadata: {f}")

    at = meta.get("artifact_type", "").lower()
    type_score = 20 if at in ALLOWED_ARTIFACT_TYPES else 0
    if type_score == 0:
        issues.append(f"artifact_type '{at}' not in allowed set")

    # One file = one artifact: penalize duplicate ID markers for THIS type.
    # Count only true ID-lines (marker at line start), not inline references
    # like "* Source KEDB ID: ...".
    prof = profile_for(meta.get("artifact_type", ""))
    atomic_score = 20
    marker = prof["id_marker"]
    if marker:
        marker_lines = sum(
            1 for ln in text.splitlines()
            if ln.strip().upper().startswith(marker) and ":" not in ln
        )
        if marker_lines > 1:
            atomic_score = 0
            issues.append("multiple artifacts detected in one file (violates one-file-one-artifact)")
    return round(meta_score + type_score + atomic_score, 2), issues


def score_bm25_readiness(text: str, meta: dict) -> tuple[float, list[str]]:
    """BM25 authoring: canonical term repetition, literal evidence, type-aware headings."""
    issues: list[str] = []
    headings = parse_headings(text)
    heading_set = {h for h in headings}
    obj = meta.get("object_name", "")
    prof = profile_for(meta.get("artifact_type", ""))
    sacred = prof["sacred"]

    # Canonical term (object_name) repeated across title + sections.
    reps = text.upper().count(obj.upper()) if obj else 0
    canon_score = min(reps, 4) / 4 * 30
    if reps < 2:
        issues.append("canonical object_name appears <2 times (BM25 rewards repetition)")

    # Sacred headings for THIS artifact type, present and not renamed.
    sacred_present = sum(1 for h in sacred if h in heading_set)
    sacred_score = sacred_present / len(sacred) * 40
    for h in sacred:
        if h not in heading_set:
            issues.append(f"sacred heading missing or renamed: {h}")

    # Literal evidence: status codes, exception classes, taxonomy bars.
    # Taxonomy line (DOMAIN | CATEGORY | ...) is expected for kedb/runbook,
    # not for documentation — DOC redistributes that weight to status codes.
    at = (meta.get("artifact_type", "") or "").lower()
    has_taxonomy = "|" in text
    has_code = any(c.isdigit() for c in text)
    if at == "documentation":
        ev_score = 30 if has_code else 0
    else:
        ev_score = (15 if has_taxonomy else 0) + (15 if has_code else 0)
        if not has_taxonomy:
            issues.append("no normalized taxonomy (DOMAIN | CATEGORY | ... ) found")
    return round(canon_score + sacred_score + ev_score, 2), issues


def score_chunking_quality(text: str, meta: dict) -> tuple[float, list[str]]:
    """Chunking: short action-led steps, low vague-language, structured bullets."""
    issues: list[str] = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    bullets = [l for l in lines if l.startswith(("*", "-"))]

    bullet_ratio = len(bullets) / max(len(lines), 1)
    bullet_score = min(bullet_ratio / 0.4, 1.0) * 40
    if bullet_ratio < 0.2:
        issues.append("few bullet points — prefer structured steps over text blocks")

    action_led = sum(1 for b in bullets
                     if b.lstrip("*- ").lower().startswith(_ACTION_VERBS))
    action_score = (action_led / len(bullets) if bullets else 0) * 30

    low = text.lower()
    vague_hits = sum(low.count(v) for v in _VAGUE)
    vague_score = max(0, 30 - vague_hits * 10)
    if vague_hits:
        issues.append(f"{vague_hits} vague phrase(s) detected (e.g. 'may occur', 'system issue')")
    return round(bullet_score + action_score + vague_score, 2), issues


def _status(score: float) -> str:
    if score >= 90: return "enterprise_ready"
    if score >= 80: return "ready_minor"
    if score >= 70: return "conditional"
    if score >= 60: return "needs_remediation"
    return "not_ready"


def score_document(text: str) -> dict:
    """Full static quality report for one document."""
    meta = parse_metadata(text)
    p, p_iss = score_policy_compliance(text, meta)
    b, b_iss = score_bm25_readiness(text, meta)
    c, c_iss = score_chunking_quality(text, meta)

    composite = (
        p * STATIC_WEIGHTS["policy"]
        + b * STATIC_WEIGHTS["bm25"]
        + c * STATIC_WEIGHTS["chunking"]
    ) / _STATIC_TOTAL
    composite = round(composite, 2)

    return {
        "categories": {
            "ingestion_policy_compliance": p,
            "bm25_authoring_readiness": b,
            "chunking_quality": c,
        },
        "composite": composite,
        "status": _status(composite),
        "issues": p_iss + b_iss + c_iss,
        "metadata": meta,
    }

"""QDKEP document parsing + canonical signature.

Parses the AI4BIZ atomic KB/RB/DOC format: an inline metadata block (key: "value")
at the top, followed by numbered/named sections. Pure stdlib.
"""
from __future__ import annotations

import hashlib
import re

# Mandatory metadata fields per AI4BIZ Knowledge Schema Standard + QDKEP contract.
MANDATORY_FIELDS = (
    "environment", "customer_id", "system_id",
    "sap_sid", "artifact_type", "object_name", "priority",
)
RECOMMENDED_FIELDS = (
    "version", "interface_id", "integration_flow_id",
    "integration_flow_name", "package_name", "review_cycle",
)

# Sacred headings — renaming silently degrades BM25 retrieval.
SACRED_HEADINGS = ("ERROR SIGNATURE", "DIAGNOSIS STEPS", "RECOVERY STEPS", "ESCALATION CRITERIA")

# Artifact-type profiles: each type is scored against its own required headings
# and its own atomic-unit marker (the ID line that should appear exactly once).
ARTIFACT_PROFILES = {
    "runbook": {
        "sacred": ("ERROR SIGNATURE", "DIAGNOSIS STEPS", "RECOVERY STEPS", "ESCALATION CRITERIA"),
        "expected": ("PURPOSE", "BUSINESS CONTEXT", "TECHNICAL LANDSCAPE",
                     "ERROR SIGNATURE", "DIAGNOSIS STEPS", "RECOVERY STEPS",
                     "ESCALATION CRITERIA", "POST-FIX VALIDATION"),
        "id_marker": "RUNBOOK ID",
    },
    "kedb": {
        "sacred": ("SYMPTOM", "ERROR PATTERN", "ROOT CAUSE", "RESOLUTION"),
        "expected": ("SYMPTOM", "ERROR PATTERN", "ROOT CAUSE", "DIAGNOSIS SIGNALS",
                     "RESOLUTION", "VALIDATION"),
        "id_marker": "KEDB ID",
    },
    "documentation": {
        "sacred": ("PURPOSE", "SCOPE", "CORE EXPLANATION"),
        "expected": ("PURPOSE", "SCOPE", "KEY TERMS", "CORE EXPLANATION",
                     "OPERATIONAL RELEVANCE"),
        "id_marker": None,  # DOC has no single ID-line marker
    },
}


def profile_for(artifact_type: str) -> dict:
    """Return the scoring profile for an artifact_type, defaulting to runbook."""
    return ARTIFACT_PROFILES.get((artifact_type or "").lower(), ARTIFACT_PROFILES["runbook"])

ALLOWED_ARTIFACT_TYPES = (
    "kedb", "runbook", "architecture", "interface_inventory",
    "monitoring_pattern", "fix", "ticket_template", "troubleshooting", "documentation",
)

_META_LINE = re.compile(r'^\s*([a-z_]+)\s*:\s*"?(.*?)"?\s*$')
_HEADING = re.compile(r'^\s*#{1,6}\s+(.*?)\s*$|^\s*\d+\.\s+([A-Z][A-Z \-/]+)\s*$')

# Volatile fragments stripped before hashing so the same incident hashes stably.
_VOLATILE = [
    re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'),  # GUID
    re.compile(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\b'),  # timestamp
    re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'),                 # IPv4
    re.compile(r':\d{2,5}\b'),                                  # port
    re.compile(r'\s+'),
]


def parse_metadata(text: str) -> dict:
    """Extract the inline metadata block from the top of the document."""
    meta: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = _META_LINE.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key in MANDATORY_FIELDS or key in RECOMMENDED_FIELDS:
                meta[key] = val
        elif meta and not line.strip():
            break  # blank line after metadata block ends it
    return meta


def parse_headings(text: str) -> list[str]:
    """Return the list of section headings (normalized uppercase)."""
    headings = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            h = (m.group(1) or m.group(2) or "").strip().upper()
            # Strip leading "N. " from markdown headings like "## 5. ERROR SIGNATURE"
            h = re.sub(r'^\d+\.\s*', '', h)
            if h:
                headings.append(h)
    return headings


def extract_error_pattern(text: str) -> str:
    """Pull the canonical error pattern (taxonomy line) from the ERROR SIGNATURE
    or ERROR PATTERN section, e.g. 'GROOVY | XML | PARSE | MALFORMED_XML'."""
    lines = text.splitlines()
    capture = False
    for line in lines:
        u = line.strip().upper()
        if "ERROR SIGNATURE" in u or "ERROR PATTERN" in u:
            capture = True
            continue
        if capture and line.strip():
            return line.strip().lstrip("* ").strip()
    return ""


def _normalize(s: str) -> str:
    s = s.upper()
    for pat in _VOLATILE:
        s = pat.sub(" ", s)
    return s.strip()


def canonical_signature(system_id: str, object_name: str, error_pattern: str) -> str:
    """SHA-256 over normalized (system_id + object_name + error_pattern)."""
    basis = "|".join(_normalize(p) for p in (system_id, object_name, error_pattern))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()

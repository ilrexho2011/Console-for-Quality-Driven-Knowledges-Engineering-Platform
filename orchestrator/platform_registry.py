"""Platform registry — the single source of enabled platforms.

`--platform all` resolves through enabled_platforms() so new platforms appear
without touching the orchestrator or CLI.
"""

from __future__ import annotations

# Order is the canonical processing order; membership is the enablement set.
_ENABLED = ["CPI", "PIPO", "APIM", "AEM", "S4H"]


def enabled_platforms() -> list[str]:
    return list(_ENABLED)


def is_enabled(platform: str) -> bool:
    return platform.upper() in _ENABLED


def resolve(spec: str) -> list[str]:
    """Resolve a --platform spec into a concrete platform list.

    'all' -> enabled_platforms(); 'cpi,pipo' -> ['CPI','PIPO']; 'cpi' -> ['CPI'].
    Unknown names are dropped (never defaulted to CPI).
    """
    spec = (spec or "").strip().lower()
    if spec in ("", "all"):
        return enabled_platforms()
    out = []
    for part in spec.split(","):
        p = part.strip().upper()
        if p in _ENABLED and p not in out:
            out.append(p)
    return out

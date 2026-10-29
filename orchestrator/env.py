"""Minimal .env loader (stdlib only).

Reads KEY=VALUE lines from a .env file in the project root and sets them into
os.environ without overwriting variables that are already set in the shell.
Supports quoted values, inline comments after a value, and blank/comment lines.

No external dependency (python-dotenv is not required).
"""
from __future__ import annotations

import os


def load_dotenv(path: str | None = None, *, override: bool = False) -> dict:
    """Load environment variables from a .env file.

    path     : explicit path; defaults to <project_root>/.env
    override : if True, values in .env replace existing os.environ entries.
    Returns the dict of keys it set.
    """
    if path is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, ".env")

    loaded: dict[str, str] = {}
    if not os.path.isfile(path):
        return loaded

    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            # Strip surrounding quotes; only strip inline comments when unquoted.
            if val and val[0] in ("'", '"') and val[-1:] == val[0]:
                val = val[1:-1]
            else:
                hash_at = val.find(" #")
                if hash_at != -1:
                    val = val[:hash_at].rstrip()
            if not key:
                continue
            if override or key not in os.environ:
                os.environ[key] = val
            loaded[key] = val
    return loaded

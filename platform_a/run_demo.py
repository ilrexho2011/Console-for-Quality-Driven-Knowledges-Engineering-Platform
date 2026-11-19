"""Demo: generate RB + KB from a CSV of error records and validate the output.

Run:
    python -m platform_a.run_demo
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

from platform_a.generators.error_artifacts import (
    ErrorRecord,
    generate_kedb,
    generate_runbook,
)

OUT = Path("generated_package/knowledge")


def load_records(csv_path: Path) -> list[ErrorRecord]:
    records: list[ErrorRecord] = []
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            records.append(ErrorRecord(
                customer_id=row["customer_id"],
                system_id=row["system_id"],
                sap_sid=row.get("sap_sid", ""),
                object_name=row["object_name"],
                title=row["title"],
                priority=row["priority"],
                error_pattern=row["error_pattern"],
                symptom=row["symptom"],
                root_cause=row["root_cause"],
                fix_steps=row["fix_steps"],
                workaround=row.get("workaround", ""),
                interface_name=row.get("interface_name", ""),
                integration_flow_id=row.get("integration_flow_id", ""),
                package_name=row.get("package_name", ""),
                kedb_id=row.get("kedb_id", ""),
                evidence=[e for e in row.get("evidence", "").split("||") if e.strip()],
            ))
    return records


def main() -> int:
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample_errors.csv")
    records = load_records(csv_path)

    (OUT / "RB").mkdir(parents=True, exist_ok=True)
    (OUT / "KB").mkdir(parents=True, exist_ok=True)
    (OUT / "DOC").mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for i, rec in enumerate(records, start=1):
        rb = generate_runbook(rec, runbook_id=f"RB-{rec.priority}-{i:04d}")
        kb = generate_kedb(rec, kedb_id=rec.kedb_id or f"KEDB-{rec.priority}-{i:04d}")

        meta_problems = rb.metadata.validate() + kb.metadata.validate()
        if meta_problems:
            print("metadata problems:", meta_problems)

        rb_path = OUT / "RB" / rb.filename
        kb_path = OUT / "KB" / kb.filename
        rb_path.write_text(rb.render(), encoding="utf-8")
        kb_path.write_text(kb.render(), encoding="utf-8")
        written.extend([rb_path, kb_path])

    print(f"generated {len(written)} files in {OUT}")
    for p in written:
        print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

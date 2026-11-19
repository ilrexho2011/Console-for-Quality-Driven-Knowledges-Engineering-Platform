"""Build a complete QDKEP knowledge package (RB + KB + DOC).

Reads an error CSV and an interface CSV, generates artifacts for all three
document types into the knowledge/ layout the AI4BIZ runtime expects, then
prints a manifest.

Run:
    python -m platform_a.build_package sample_errors.csv sample_interfaces.csv
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from platform_a.generators.error_artifacts import (
    ErrorRecord, generate_kedb, generate_runbook,
)
from platform_a.generators.catalog_artifacts import (
    InterfaceRecord, generate_catalog_doc, generate_interface_inventory,
)

OUT = Path("generated_package/knowledge")


def load_errors(path: Path) -> list[ErrorRecord]:
    rows: list[ErrorRecord] = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(ErrorRecord(
                customer_id=r["customer_id"], system_id=r["system_id"],
                sap_sid=r.get("sap_sid", ""), object_name=r["object_name"],
                title=r["title"], priority=r["priority"],
                error_pattern=r["error_pattern"], symptom=r["symptom"],
                root_cause=r["root_cause"], fix_steps=r["fix_steps"],
                workaround=r.get("workaround", ""),
                interface_name=r.get("interface_name", ""),
                integration_flow_id=r.get("integration_flow_id", ""),
                package_name=r.get("package_name", ""),
                kedb_id=r.get("kedb_id", ""),
                evidence=[e for e in r.get("evidence", "").split("||") if e.strip()],
            ))
    return rows


def load_interfaces(path: Path) -> list[InterfaceRecord]:
    rows: list[InterfaceRecord] = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(InterfaceRecord(
                customer_id=r["customer_id"], system_id=r["system_id"],
                sap_sid=r.get("sap_sid", ""), object_name=r["object_name"],
                interface_name=r.get("interface_name", r["object_name"]),
                integration_flow_id=r.get("integration_flow_id", ""),
                package_name=r.get("package_name", ""),
                package_id=r.get("package_id", ""),
                priority=r.get("priority", "P3"),
                sender_system=r.get("sender_system", ""),
                receiver_system=r.get("receiver_system", ""),
                technology=r.get("technology", "SAP CPI"),
                interface_type=r.get("interface_type", "asynchronous"),
                status=r.get("status", "active"),
                sibling_interfaces=[s for s in r.get("sibling_interfaces", "").split("|") if s],
                kedb_family_seeds=[s for s in r.get("kedb_family_seeds", "").split("|") if s],
            ))
    return rows


def main() -> int:
    errors_csv = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample_errors.csv")
    interfaces_csv = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sample_interfaces.csv")

    for sub in ("RB", "KB", "DOC"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)

    manifest: list[str] = []

    for i, rec in enumerate(load_errors(errors_csv), start=1):
        rb = generate_runbook(rec, runbook_id=f"RB-{rec.priority}-{i:04d}")
        kb = generate_kedb(rec, kedb_id=rec.kedb_id or f"KEDB-{rec.priority}-{i:04d}")
        (OUT / "RB" / rb.filename).write_text(rb.render(), encoding="utf-8")
        (OUT / "KB" / kb.filename).write_text(kb.render(), encoding="utf-8")
        manifest += [f"RB/{rb.filename}", f"KB/{kb.filename}"]

    if interfaces_csv.exists():
        for rec in load_interfaces(interfaces_csv):
            doc = generate_catalog_doc(rec)
            (OUT / "DOC" / doc.filename).write_text(doc.render(), encoding="utf-8")
            manifest.append(f"DOC/{doc.filename}")

    print(f"built {len(manifest)} artifacts into {OUT}")
    for m in manifest:
        print("  ", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

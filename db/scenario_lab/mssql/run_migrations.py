"""Apply / roll back Scenario Lab migrations against MS SQL Server.

Splits each .sql file on GO batch separators and executes batches in order.
Usage:
    python run_migrations.py up        # apply all pending V-scripts
    python run_migrations.py down 1    # roll back the last N migrations
Connection comes from env: MSSQL_CONN (full ODBC string) OR the discrete
MSSQL_SERVER / MSSQL_DATABASE / MSSQL_USER / MSSQL_PASSWORD vars.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pyodbc

HERE = Path(__file__).parent
UP_DIR = HERE / "migration" / "up"
DOWN_DIR = HERE / "migration" / "rollback"
GO = re.compile(r"(?im)^\s*GO\s*$")


def conn_string() -> str:
    if os.getenv("MSSQL_CONN"):
        return os.environ["MSSQL_CONN"]
    server = os.environ["MSSQL_SERVER"]
    database = os.environ["MSSQL_DATABASE"]
    driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}",
             "Encrypt=yes",
             f"TrustServerCertificate={os.getenv('MSSQL_TRUST_CERT', 'no')}"]
    if os.getenv("MSSQL_TRUSTED", "").lower() in {"1", "true", "yes"}:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={os.environ['MSSQL_USER']}")
        parts.append(f"PWD={os.environ['MSSQL_PASSWORD']}")
    return ";".join(parts) + ";"


def run_file(conn: pyodbc.Connection, path: Path) -> None:
    batches = [b.strip() for b in GO.split(path.read_text(encoding="utf-8")) if b.strip()]
    cur = conn.cursor()
    for batch in batches:
        cur.execute(batch)
        while cur.nextset():  # consume PRINT / result sets
            pass
    conn.commit()
    print(f"  applied {path.name}")


def applied_versions(conn: pyodbc.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute(
        "IF OBJECT_ID('scenario_lab.schema_migration','U') IS NOT NULL "
        "SELECT version FROM scenario_lab.schema_migration"
    )
    try:
        return {r[0] for r in cur.fetchall()}
    except pyodbc.ProgrammingError:
        return set()


def cmd_up() -> None:
    with pyodbc.connect(conn_string(), autocommit=False) as conn:
        done = applied_versions(conn)
        for path in sorted(UP_DIR.glob("V*.sql")):
            version = path.name.split("__")[0]
            if version in done:
                print(f"  skip {version} (already applied)")
                continue
            print(f"applying {version} ...")
            run_file(conn, path)


def cmd_down(n: int) -> None:
    with pyodbc.connect(conn_string(), autocommit=False) as conn:
        done = sorted(applied_versions(conn), reverse=True)
        for version in done[:n]:
            num = version[1:]  # V003 -> 003
            matches = list(DOWN_DIR.glob(f"U{num}__*.sql"))
            if not matches:
                print(f"  no rollback file for {version}")
                continue
            print(f"rolling back {version} ...")
            run_file(conn, matches[0])


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "up"
    if action == "up":
        cmd_up()
    elif action == "down":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        cmd_down(count)
    else:
        print(__doc__)
        sys.exit(1)

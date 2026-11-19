"""PipelineRun persistence and state machine for the isolated CLI commands.

pipeline_run.json is the single source of run state. Each command reads it,
checks the current state, runs its service, writes its output atomically, and
advances the state. A no-candidate generation is a SUCCESS, not a failure.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

SCHEMA_VERSION = "1.0"
RUN_FILE = "pipeline_run.json"


class ExitCode(IntEnum):
    OK = 0
    INVALID_ARGS = 2
    MISSING_INPUT = 10
    INVALID_SCHEMA = 11
    ILLEGAL_STATE = 20
    MONITOR_FAILED = 30
    GENERATION_FAILED = 40
    BENCHMARK_FAILED = 50
    PROMOTION_BLOCKED = 60
    PERSISTENCE_ERROR = 70
    UNEXPECTED = 99


# Controlled state machine
STATES = [
    "CREATED", "MONITORED", "GENERATED", "BENCHMARKED", "PROMOTED",
    # alternative branches
    "GENERATED_NO_CANDIDATES", "BENCHMARK_FAILED", "PROMOTION_BLOCKED", "FAILED",
]

# allowed transitions: stage command -> required current states
STAGE_PRECONDITIONS = {
    "monitor": {"CREATED", "MONITORED"},  # idempotent re-run allowed
    "generate": {"MONITORED", "GENERATED", "GENERATED_NO_CANDIDATES"},
    "benchmark": {"GENERATED", "BENCHMARKED", "BENCHMARK_FAILED"},
    "promote": {"BENCHMARKED", "PROMOTED", "PROMOTION_BLOCKED"},
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def atomic_write_json(path: Path, obj) -> None:
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


@dataclass
class PipelineRun:
    run_id: str
    run_dir: Path
    schema_version: str = SCHEMA_VERSION
    platforms: list[str] = field(default_factory=list)
    status: str = "CREATED"
    current_stage: str = "created"
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    stages: dict = field(default_factory=dict)

    @classmethod
    def create(cls, run_id: str, runs_root: str, platforms: list[str] | None = None) -> "PipelineRun":
        run_dir = Path(runs_root) / run_id
        run = cls(run_id=run_id, run_dir=run_dir, platforms=platforms or [])
        run.stages = {s: {"status": "PENDING"} for s in ("monitor", "generate", "benchmark", "promote")}
        run.save()
        return run

    @classmethod
    def load(cls, run_id: str, runs_root: str) -> "PipelineRun":
        run_dir = Path(runs_root) / run_id
        path = run_dir / RUN_FILE
        if not path.exists():
            raise FileNotFoundError(f"no pipeline_run.json for {run_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        run = cls(
            run_id=data["run_id"], run_dir=run_dir,
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            platforms=data.get("platforms", []),
            status=data.get("status", "CREATED"),
            current_stage=data.get("current_stage", "created"),
            created_at=data.get("created_at", utcnow()),
            updated_at=data.get("updated_at", utcnow()),
            stages=data.get("stages", {}),
        )
        return run

    @classmethod
    def load_or_adopt(cls, run_id: str, runs_root: str) -> "PipelineRun":
        """Load a run; if it has no pipeline_run.json but does have a populated
        knowledge/ directory, adopt the folder as a benchmark-only run.

        This lets users drop a hand-built archive (only knowledge/{KB,RB}/*.md)
        into runs/ and benchmark it directly, without any pipeline metadata.
        """
        run_dir = Path(runs_root) / run_id
        if (run_dir / RUN_FILE).exists():
            return cls.load(run_id, runs_root)
        knowledge = run_dir / "knowledge"
        if run_dir.exists() and knowledge.exists() and any(knowledge.rglob("*.md")):
            run = cls(run_id=run_id, run_dir=run_dir, platforms=[],
                      status="ADOPTED", current_stage="generated")
            run.stages = {s: {"status": "PENDING"} for s in
                          ("monitor", "generate", "benchmark", "promote")}
            run.stages["generate"] = {"status": "COMPLETED", "detail": {"adopted": True}}
            return run
        raise FileNotFoundError(f"no pipeline_run.json or knowledge for {run_id}")

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "platforms": self.platforms,
            "status": self.status,
            "current_stage": self.current_stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stages": self.stages,
        }

    def save(self) -> None:
        self.updated_at = utcnow()
        atomic_write_json(self.run_dir / RUN_FILE, self.to_dict())

    def can_run(self, stage: str) -> bool:
        return self.status in STAGE_PRECONDITIONS.get(stage, set())

    def mark_stage(self, stage: str, status: str, output: str | None = None,
                   run_status: str | None = None, current_stage: str | None = None) -> None:
        entry = {"status": status}
        if output:
            entry["output"] = output
        self.stages[stage] = entry
        if run_status:
            self.status = run_status
        if current_stage:
            self.current_stage = current_stage
        self.save()

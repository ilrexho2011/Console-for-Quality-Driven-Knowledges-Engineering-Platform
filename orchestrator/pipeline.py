"""QDKEP pipeline orchestrator with run_id stage tracking.

One run_id flows through every stage: monitor -> generate -> benchmark ->
promote -> package. Each stage records PASSED/FAILED/WAITING so the dashboard,
CLI and tests share one source of truth.

This is the real (small) implementation the e2e test asserts against. The full
CLI (`qdkep pipeline --platform all`) wraps this once the e2e test is green.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from platform_a.connectors.local_mpl import LocalMPLSource
from platform_a.connectors.base import MPLQuery
from platform_a.generators.manager import GenerationManager, DuplicatePolicy
from platform_a.generators.pair_builder import build_pairs_from_source
from platform_b.benchmark.orchestrator import run_benchmark
from platform_b.feedback.corrective_feedback import build_feedback


def make_run_id(when: datetime | None = None, seq: int | None = None,
                runs_root: str | Path | None = None) -> str:
    """Build a unique run id in the RUN-NNN-YYYYMMDD model.

    Example: RUN-001-20260621 (progressive NNN + the current date).

    When `seq` is given it is used verbatim (tests pin it). Otherwise, if
    `runs_root` is provided, NNN is the next free GLOBAL sequence across all
    existing runs (not reset per day), so every new run gets a strictly higher
    number. Falls back to 001 when no runs_root is available.
    """
    when = when or datetime.now(timezone.utc)
    day = when.strftime("%Y%m%d")
    if seq is not None:
        return f"RUN-{seq:03d}-{day}"
    next_seq = 1
    if runs_root is not None:
        root = Path(runs_root)
        if root.exists():
            used = []
            for p in root.glob("RUN-*"):
                # accept both RUN-NNN-YYYYMMDD (new) and RUN-YYYYMMDD-NNN (legacy)
                parts = p.name.split("-")
                if len(parts) == 3:
                    a, b = parts[1], parts[2]
                    if a.isdigit() and len(a) <= 3:        # RUN-NNN-YYYYMMDD
                        used.append(int(a))
                    elif b.isdigit() and len(a) == 8:       # RUN-YYYYMMDD-NNN
                        used.append(int(b))
            next_seq = (max(used) + 1) if used else 1
    return f"RUN-{next_seq:03d}-{day}"


@dataclass
class StageResult:
    name: str
    status: str = "WAITING"   # PASSED | FAILED | WAITING | NOT_CREATED
    detail: dict = field(default_factory=dict)


@dataclass
class MonitorResult:
    failed: int = 0
    patterns: int = 0
    status: str = "WAITING"


@dataclass
class GenerationResult:
    kedb_count: int = 0
    runbook_count: int = 0
    status: str = "WAITING"


@dataclass
class BenchmarkResult:
    overall: int = 0
    passed: bool = False
    status: str = "WAITING"


@dataclass
class PromotionResult:
    promoted_count: int = 0
    decision: str = "WAITING"
    status: str = "WAITING"


@dataclass
class PipelineResult:
    run_id: str
    monitor: MonitorResult = field(default_factory=MonitorResult)
    generation: GenerationResult = field(default_factory=GenerationResult)
    benchmark: BenchmarkResult = field(default_factory=BenchmarkResult)
    promotion: PromotionResult = field(default_factory=PromotionResult)
    package_created: bool = False


@dataclass
class S4HStageView:
    """Per-event view used by S4H/platform e2e tests."""
    source_platform: str = ""
    source_type: str = ""


@dataclass
class PatternView:
    event_family: str = ""
    signature: str = ""


@dataclass
class GenView:
    eligible: bool = False
    reason_code: str = ""


@dataclass
class DocView:
    pair_id: str
    object_name: str
    document_type: str


@dataclass
class EventPipelineResult:
    run_id: str
    normalized: S4HStageView
    pattern: PatternView
    generation: GenView
    documents: list = field(default_factory=list)
    benchmark: BenchmarkResult = field(default_factory=BenchmarkResult)


def run_pipeline(
    platform: str,
    source: str,
    out_root: str,
    promote: bool = False,
    threshold: int = 80,
    stop_on_benchmark_failure: bool = True,
    customer_id: str = "RCA",
    system_id: str = "CPI-RCA",
    sap_sid: str = "CPI",
    run_id: str | None = None,
) -> PipelineResult:
    rid = run_id or make_run_id()
    result = PipelineResult(run_id=rid)

    # --- Monitor ---
    src = LocalMPLSource(source)
    failed = list(src.fetch_mpl(MPLQuery(status="FAILED")))
    result.monitor.failed = len(failed)
    result.monitor.status = "PASSED"

    # --- Generate (atomic KB+RB pairs) ---
    mgr = GenerationManager(
        out_root=Path(out_root),
        duplicate_policy_by_type={
            "runbook": DuplicatePolicy.BLOCK,
            "kedb": DuplicatePolicy.BLOCK,
            "documentation": DuplicatePolicy.BLOCK,
        },
    )
    gen = build_pairs_from_source(src, customer_id, system_id, sap_sid, mgr, version="v1")
    result.monitor.patterns = gen["pairs"]
    result.generation.kedb_count = gen["kedb"]["written"]
    result.generation.runbook_count = gen["runbook"]["written"]
    result.generation.status = "PASSED" if gen["pairs"] > 0 else "FAILED"

    # --- Benchmark ---
    knowledge_dir = str(Path(out_root) / "knowledge")
    bench = run_benchmark(knowledge_dir, package_id=rid)
    result.benchmark.overall = bench["overall_score"]
    result.benchmark.passed = bench["overall_score"] >= threshold
    result.benchmark.status = "PASSED" if result.benchmark.passed else "FAILED"

    if stop_on_benchmark_failure and not result.benchmark.passed:
        result.promotion.status = "WAITING"
        result.package_created = False
        return result

    # --- Promote (quality gate) ---
    fb = build_feedback(bench, threshold=threshold)
    result.promotion.decision = fb["promotion_decision"]
    if promote and fb["promotion_decision"] == "promote":
        result.promotion.promoted_count = result.generation.kedb_count + result.generation.runbook_count
        result.promotion.status = "PASSED"
        result.package_created = True
    else:
        result.promotion.status = "WAITING"
        result.package_created = False

    return result


def run_event_pipeline(
    source: str,
    platform: str,
    out_root: str,
    customer_id: str = "RCA",
    system_id: str = "S4H-RCA",
    sap_sid: str = "S4D",
    threshold: int = 80,
    run_id: str | None = None,
) -> EventPipelineResult:
    """Single-source pipeline over canonical events (any platform, incl. S4H).

    normalize -> classify -> eligibility -> generate pair(s) -> benchmark.
    Used by the platform e2e tests; mirrors the symmetric production flow.
    """
    import json
    from pathlib import Path
    from platform_a.connectors.platform_detect import normalize_record
    from platform_a.generators.eligibility import evaluate_candidate
    from platform_a.generators.event_bridge import (
        build_records_from_raw, _evidence_from_raw,
    )
    from platform_a.generators.error_artifacts import generate_pair
    from platform_a.generators.manager import GenerationManager, DuplicatePolicy
    from platform_a.generators.pair_builder import stable_pair_number

    rid = run_id or make_run_id()
    raw_records = _load_raw(source)

    # first record drives the normalized/pattern/generation single-event view
    first = raw_records[0] if raw_records else {}
    ev = normalize_record(first, platform=platform)
    cand = evaluate_candidate(ev, evidence=_evidence_from_raw(first), raw=first)

    result = EventPipelineResult(
        run_id=rid,
        normalized=S4HStageView(ev.source_platform, ev.source_type),
        pattern=PatternView(cand.event_family, cand.signature),
        generation=GenView(cand.eligible, cand.reason_code),
    )

    records, _ = build_records_from_raw(
        raw_records, customer_id=customer_id, system_id=system_id,
        sap_sid=sap_sid, platform=platform)

    if not records:
        return result  # not eligible -> no documents

    mgr = GenerationManager(out_root=Path(out_root),
        duplicate_policy_by_type={"runbook": DuplicatePolicy.BLOCK, "kedb": DuplicatePolicy.BLOCK})
    docs = []
    for rec in records:
        num = stable_pair_number(rec)
        kb, rb = generate_pair(rec, number=num)
        mgr.write(kb, "v1"); mgr.write(rb, "v1")
        pair_num = kb.id_value.split("-")[-1]
        docs.append(DocView(pair_num, kb.metadata.object_name, "kedb"))
        docs.append(DocView(pair_num, rb.metadata.object_name, "runbook"))
    result.documents = docs

    bench = run_benchmark(str(Path(out_root) / "knowledge"), package_id=rid)
    result.benchmark.overall = bench["overall_score"]
    result.benchmark.passed = bench["overall_score"] >= threshold
    result.benchmark.status = "PASSED" if result.benchmark.passed else "FAILED"
    return result


def _load_raw(source: str) -> list[dict]:
    import json
    from pathlib import Path
    p = Path(source)
    files = sorted(p.glob("*.json")) if p.is_dir() else [p]
    out: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            out.extend(data.get("results", data.get("d", {}).get("results", [])) or [data])
        elif isinstance(data, list):
            out.extend(data)
    return out

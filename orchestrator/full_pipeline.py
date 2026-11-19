"""qdkep pipeline orchestrator.

Joins the four stabilized stages over ONE run_id using the shared handlers
(no subprocess, no stdout parsing). Honors --stop-on-benchmark-failure,
--promote (promote only when benchmark passed), --platform all (from registry),
and --resume (skip completed stages). Writes pipeline-summary.json and returns
a single mapped exit code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from orchestrator.run_state import ExitCode, PipelineRun, atomic_write_json
from orchestrator.handlers import (
    StageResult, monitor_handler, generate_handler, benchmark_handler, promote_handler,
)
from orchestrator.platform_registry import resolve
from orchestrator.pipeline import make_run_id


# stages considered "completed" for --resume skipping, by run status
COMPLETED_FOR = {
    "monitor": {"MONITORED", "GENERATED", "GENERATED_NO_CANDIDATES",
                "BENCHMARKED", "BENCHMARK_FAILED", "PROMOTED", "PROMOTION_BLOCKED"},
    "generate": {"GENERATED", "GENERATED_NO_CANDIDATES",
                 "BENCHMARKED", "BENCHMARK_FAILED", "PROMOTED", "PROMOTION_BLOCKED"},
    "benchmark": {"BENCHMARKED", "BENCHMARK_FAILED", "PROMOTED", "PROMOTION_BLOCKED"},
    "promote": {"PROMOTED", "PROMOTION_BLOCKED"},
}


@dataclass
class PipelineOptions:
    platform: str = "all"
    source: str = ""
    threshold: int = 80
    stop_on_benchmark_failure: bool = True
    promote: bool = False
    resume: bool = False
    version: str = "v1"
    duplicate_block: bool = True
    ai_enrich: bool = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_summary(run: PipelineRun, options: PipelineOptions, platforms: list[str],
                   stages: dict, status: str, exit_code: int, started_at: str) -> None:
    atomic_write_json(run.run_dir / "pipeline-summary.json", {
        "run_id": run.run_id,
        "status": status,
        "started_at": started_at,
        "completed_at": _utcnow(),
        "platforms": platforms,
        "options": {
            "stop_on_benchmark_failure": options.stop_on_benchmark_failure,
            "promote": options.promote,
            "resume": options.resume,
        },
        "stages": stages,
        "exit_code": int(exit_code),
    })


def _stage_entry(result: StageResult) -> dict:
    entry = {"status": result.status, "output": result.output}
    entry.update(result.metrics)
    return entry


def run_full_pipeline(options: PipelineOptions, runs_root: str,
                      run_id: str | None = None) -> tuple[int, dict]:
    """Execute the pipeline end to end. Returns (exit_code, summary_stages)."""
    started_at = _utcnow()
    platforms = resolve(options.platform)

    # create or resume the run
    if options.resume and run_id:
        try:
            run = PipelineRun.load(run_id, runs_root)
        except FileNotFoundError:
            return ExitCode.MISSING_INPUT, {}
    else:
        rid = run_id or make_run_id(runs_root=runs_root)
        run = PipelineRun.create(rid, runs_root, platforms=platforms)

    stages: dict = {}

    def skip(stage: str) -> bool:
        return options.resume and run.status in COMPLETED_FOR.get(stage, set())

    # --- monitor ---
    if skip("monitor"):
        stages["monitor"] = {"status": "SKIPPED_RESUME"}
    else:
        res = monitor_handler(run, options.source, platforms_to_arg(platforms, options.platform))
        stages["monitor"] = _stage_entry(res)
        if not res.ok:
            _write_summary(run, options, platforms, stages, "FAILED", res.exit_code, started_at)
            return res.exit_code, stages

    # --- generate ---
    if skip("generate"):
        stages["generate"] = {"status": "SKIPPED_RESUME"}
        gen_status = run.status
    else:
        res = generate_handler(run, version=options.version,
                               duplicate_block=options.duplicate_block,
                               runs_root=runs_root,
                               ai_enrich=options.ai_enrich)
        stages["generate"] = _stage_entry(res)
        gen_status = run.status
        if not res.ok:
            _write_summary(run, options, platforms, stages, "FAILED", res.exit_code, started_at)
            return res.exit_code, stages

    # no candidates -> benchmark + promote SKIPPED, success
    if run.status == "GENERATED_NO_CANDIDATES":
        stages["benchmark"] = {"status": "SKIPPED"}
        stages["promote"] = {"status": "SKIPPED"}
        _write_summary(run, options, platforms, stages, "COMPLETED", ExitCode.OK, started_at)
        return ExitCode.OK, stages

    # --- benchmark ---
    if skip("benchmark"):
        stages["benchmark"] = {"status": "SKIPPED_RESUME"}
        bench_passed = run.status in ("BENCHMARKED", "PROMOTED", "PROMOTION_BLOCKED")
    else:
        res = benchmark_handler(run, threshold=options.threshold)
        stages["benchmark"] = _stage_entry(res)
        bench_passed = res.status == "PASSED"
        if not bench_passed:
            if options.stop_on_benchmark_failure:
                stages["promote"] = {"status": "NOT_ATTEMPTED"}
                _write_summary(run, options, platforms, stages,
                               "STOPPED_BY_POLICY", ExitCode.BENCHMARK_FAILED, started_at)
                return ExitCode.BENCHMARK_FAILED, stages
            # continue without promoting a failed benchmark
            stages["promote"] = {"status": "BLOCKED"}
            _write_summary(run, options, platforms, stages,
                           "BENCHMARK_FAILED", ExitCode.BENCHMARK_FAILED, started_at)
            return ExitCode.BENCHMARK_FAILED, stages

    # --- promote (only when benchmark passed and --promote) ---
    if skip("promote"):
        stages["promote"] = {"status": "SKIPPED_RESUME"}
        _write_summary(run, options, platforms, stages, "COMPLETED", ExitCode.OK, started_at)
        return ExitCode.OK, stages

    if options.promote and bench_passed:
        res = promote_handler(run, threshold=options.threshold)
        stages["promote"] = _stage_entry(res)
        if res.exit_code == ExitCode.PROMOTION_BLOCKED:
            _write_summary(run, options, platforms, stages,
                           "PROMOTION_BLOCKED", ExitCode.PROMOTION_BLOCKED, started_at)
            return ExitCode.PROMOTION_BLOCKED, stages
        _write_summary(run, options, platforms, stages, "COMPLETED", ExitCode.OK, started_at)
        return ExitCode.OK, stages

    # benchmark passed but promotion not requested
    stages["promote"] = {"status": "NOT_REQUESTED"}
    _write_summary(run, options, platforms, stages, "COMPLETED", ExitCode.OK, started_at)
    return ExitCode.OK, stages


def platforms_to_arg(platforms: list[str], original: str) -> str:
    """Pass a single platform name to monitor when unambiguous, else 'all'."""
    if len(platforms) == 1:
        return platforms[0].lower()
    return "all"

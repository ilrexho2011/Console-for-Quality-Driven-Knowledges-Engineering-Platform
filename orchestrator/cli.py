"""qdkep CLI — four isolated, idempotent stage commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.run_state import ExitCode, PipelineRun, atomic_write_json
from orchestrator.pipeline import make_run_id


def _runs_root(args):
    return args.runs_root


def cmd_monitor(args) -> int:
    from orchestrator.handlers import monitor_handler
    run_id = args.run_id or make_run_id()
    try:
        run = PipelineRun.load(run_id, _runs_root(args))
    except FileNotFoundError:
        run = PipelineRun.create(run_id, _runs_root(args), platforms=[args.platform.upper()])
    if not run.can_run("monitor"):
        print(f"illegal state {run.status} for monitor", file=sys.stderr)
        return ExitCode.ILLEGAL_STATE
    res = monitor_handler(run, args.source, args.platform)
    if res.ok:
        print(f"monitor: {res.metrics.get('events_total', 0)} events ({run_id})")
    else:
        print(f"monitor failed: {res.status}", file=sys.stderr)
    return res.exit_code


def cmd_generate(args) -> int:
    from orchestrator.handlers import generate_handler
    try:
        run = PipelineRun.load(args.run_id, _runs_root(args))
    except FileNotFoundError:
        return ExitCode.MISSING_INPUT
    if not run.can_run("generate"):
        print(f"illegal state {run.status} for generate", file=sys.stderr)
        return ExitCode.ILLEGAL_STATE
    res = generate_handler(run)
    if res.status == "COMPLETED_NO_CANDIDATES":
        print(f"generate: COMPLETED_NO_CANDIDATES ({res.metrics.get('ineligible_count', 0)} ineligible)")
    elif res.ok:
        print(f"generate: {res.metrics.get('pairs', 0)} pair(s)")
    else:
        print(f"generate failed: {res.status}", file=sys.stderr)
    return res.exit_code


def cmd_benchmark(args) -> int:
    from orchestrator.handlers import benchmark_handler
    try:
        run = PipelineRun.load(args.run_id, _runs_root(args))
    except FileNotFoundError:
        return ExitCode.MISSING_INPUT
    if not run.can_run("benchmark"):
        print(f"illegal state {run.status} for benchmark", file=sys.stderr)
        return ExitCode.ILLEGAL_STATE
    res = benchmark_handler(run, threshold=args.threshold)
    score = res.metrics.get("overall_score", 0)
    if res.ok:
        print(f"benchmark: {score} PASSED")
    else:
        print(f"benchmark: {score} below {args.threshold}", file=sys.stderr)
    return res.exit_code


def cmd_promote(args) -> int:
    from orchestrator.handlers import promote_handler
    try:
        run = PipelineRun.load(args.run_id, _runs_root(args))
    except FileNotFoundError:
        return ExitCode.MISSING_INPUT
    if not run.can_run("promote"):
        print(f"illegal state {run.status} for promote", file=sys.stderr)
        return ExitCode.ILLEGAL_STATE
    res = promote_handler(run, threshold=args.threshold)
    if res.ok:
        print("promote: PROMOTED")
    else:
        print("promote: BLOCKED", file=sys.stderr)
    return res.exit_code


def cmd_pipeline(args) -> int:
    from orchestrator.full_pipeline import run_full_pipeline, PipelineOptions
    options = PipelineOptions(
        platform=args.platform, source=args.source, threshold=args.threshold,
        stop_on_benchmark_failure=args.stop_on_benchmark_failure,
        promote=args.promote, resume=args.resume,
    )
    rid = args.run_id or None
    if args.resume and not rid:
        print("--resume requires --run-id", file=sys.stderr)
        return ExitCode.INVALID_ARGS
    exit_code, stages = run_full_pipeline(options, _runs_root(args), run_id=rid)
    for name in ("monitor", "generate", "benchmark", "promote"):
        st = stages.get(name, {})
        print(f"  {name:10} {st.get('status', '-')}")
    print(f"pipeline exit: {int(exit_code)}")
    return exit_code


def cmd_inventory_refresh(args) -> int:
    """Generate a fresh independent INV-run (for scheduled cron/systemd refresh).

    interface_inventory is the foundational knowledge layer; this rebuilds it from
    CPIView design + MPL into inventory_runs/INV-NNN-YYYYMMDD, independent of any
    error-driven RUN. Optionally benchmarks (adapted profile) + promotes.
    """
    from platform_a.generators.inventory_run import generate_inventory_run
    inv_root = args.inv_root or str(Path(_runs_root(args)).resolve().parent / "inventory_runs")
    summary = generate_inventory_run(
        inv_root, sap_sid=args.sap_sid, customer_id=args.customer,
        version=args.version, duplicate_block=(args.duplicate == "block"),
        ai_enrich=args.ai_enrich)
    inv_id = summary["inv_run_id"]
    line = (f"inventory-refresh: {inv_id} · {summary['documents_created']} doc(s) · "
            f"MPL {summary.get('mpl_source', 'design_only')}")
    if (summary.get("ai_enrichment") or {}).get("active"):
        line += f" · AI {summary['ai_enrichment']['provider']}"
    print(line)
    if args.benchmark and summary["documents_created"]:
        from orchestrator.handlers import benchmark_handler, promote_handler
        run = PipelineRun.load_or_adopt(inv_id, inv_root)
        b = benchmark_handler(run, threshold=args.threshold)
        p = promote_handler(run, threshold=args.threshold)
        print(f"  benchmark: {b.metrics.get('overall_score', '?')} {b.status} · "
              f"promotion {p.status}")
    return ExitCode.OK


def _reason_histogram(candidates):
    hist = {}
    for c in candidates:
        if not c.eligible:
            hist[c.reason_code] = hist.get(c.reason_code, 0) + 1
    return hist


def _load_raw(source: Path):
    files = sorted(source.rglob("*.json")) if source.is_dir() else [source]
    out = []
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


def build_parser():
    p = argparse.ArgumentParser(prog="qdkep")
    p.add_argument("--runs-root", default="runs")
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("monitor"); m.add_argument("--run-id", default=""); m.add_argument("--platform", default="cpi"); m.add_argument("--source", required=True); m.set_defaults(func=cmd_monitor)
    g = sub.add_parser("generate"); g.add_argument("--run-id", required=True); g.set_defaults(func=cmd_generate)
    b = sub.add_parser("benchmark"); b.add_argument("--run-id", required=True); b.add_argument("--threshold", type=int, default=80); b.set_defaults(func=cmd_benchmark)
    pr = sub.add_parser("promote"); pr.add_argument("--run-id", required=True); pr.add_argument("--threshold", type=int, default=80); pr.set_defaults(func=cmd_promote)
    pl = sub.add_parser("pipeline")
    pl.add_argument("--run-id", default="")
    pl.add_argument("--platform", default="all")
    pl.add_argument("--source", default="")
    pl.add_argument("--threshold", type=int, default=80)
    pl.add_argument("--promote", action="store_true")
    pl.add_argument("--no-stop-on-benchmark-failure", dest="stop_on_benchmark_failure",
                    action="store_false", default=True)
    pl.add_argument("--stop-on-benchmark-failure", dest="stop_on_benchmark_failure",
                    action="store_true")
    pl.add_argument("--resume", action="store_true")
    pl.set_defaults(func=cmd_pipeline)
    ir = sub.add_parser("inventory-refresh",
                        help="Rebuild the foundational INV inventory run (cron/systemd-friendly)")
    ir.add_argument("--inv-root", default="",
                    help="inventory-runs root (default: <runs-root>/../inventory_runs)")
    ir.add_argument("--sap-sid", default="CPI")
    ir.add_argument("--customer", default="RCA")
    ir.add_argument("--version", default="v1")
    ir.add_argument("--duplicate", choices=["block", "allow"], default="block")
    ir.add_argument("--ai-enrich", action="store_true",
                    help="AI-enrich the operational analysis (needs AI_PROVIDER/AI_MODEL in .env)")
    ir.add_argument("--benchmark", action="store_true",
                    help="also benchmark + promote the new INV run (adapted profile)")
    ir.add_argument("--threshold", type=int, default=80)
    ir.set_defaults(func=cmd_inventory_refresh)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        return ExitCode.UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())

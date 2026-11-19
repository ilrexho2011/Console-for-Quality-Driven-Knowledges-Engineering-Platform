"""QDKEP local server — one command to start the Control Room in the browser.

Pure standard library (http.server); no new dependencies. Serves:
  GET  /                          -> the live five-section dashboard HTML
  GET  /api/full-state            -> everything the dashboard needs, from runs/
  GET  /api/state                 -> run cards
  GET  /api/monitoring            -> aggregated monitoring metrics
  GET  /api/connections           -> read-only system handshake (CPI/PIPO/S4H)
  GET  /api/scan?source=...        -> scan a source without generating
  GET  /api/platforms             -> enabled platforms
  GET  /api/runs/<id>             -> a single run card
  GET  /api/runs/<id>/download     -> the run's knowledge package as a .zip
  GET  /api/inventory/<sys>/download -> exported inventory of a system as .zip
  POST /api/inventory/<sys>/export   -> export a system's design inventory (JSON)
  GET  /api/runs/<id>/doc?file=... -> one document's content (for viewing)
  GET  /api/runs/<id>/category-diagnostics -> weak-category cause & action (read-only)
  POST /api/pipeline               -> run a fresh pipeline (version, dup, promote)
  POST /api/runs/<id>/resume       -> resume an existing run
  POST /api/runs/<id>/benchmark    -> benchmark an existing/adopted run (+feedback)
  POST /api/runs/<id>/regenerate   -> regenerate one doc from feedback (new version)
  POST /api/runs/<id>/apply-feedback        -> correct one doc + re-run benchmark
  POST /api/runs/<id>/apply-feedback-group  -> correct all failing docs + re-run
  GET  /api/scenario-lab/sources    -> selectable source systems (Scenario Lab)
  GET  /api/scenario-lab/errors      -> first-seen errors for a system
  GET  /api/scenario-lab/candidates  -> atomic KB-RB candidates (status=new)
  POST /api/scenario-lab/harvest     -> pull preliminary material into staging
  POST /api/scenario-lab/candidate-status -> mark a candidate queued/generated

Routing is split into pure functions (route_get / route_post) that return a
Response object, so every branch is testable in-process without a socket. The
HTTP Handler is a thin adapter over them. Pipelines run in-process via the
shared orchestrator (no subprocess, no platform branching), reading/writing the
same runs/ directory the CLI uses.

Usage:
    python -m orchestrator.server --runs-root runs --port 8765
    # then open http://127.0.0.1:8765
"""

from __future__ import annotations

import io
import json
import threading
import webbrowser
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from orchestrator.full_pipeline import run_full_pipeline, PipelineOptions
from orchestrator.dashboard_state import (
    build_dashboard_state, build_run_card, build_full_dashboard,
    build_monitoring_metrics, scan_source,
)
from orchestrator.run_state import PipelineRun
from orchestrator.platform_registry import enabled_platforms


@dataclass
class Response:
    code: int
    body: bytes
    ctype: str = "application/json"
    headers: dict | None = None

    @classmethod
    def jsonr(cls, code: int, obj) -> "Response":
        return cls(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    @classmethod
    def htmlr(cls, code: int, text: str) -> "Response":
        return cls(code, text.encode("utf-8"), "text/html")


def _render_page(runs_root: str) -> str:
    """Serve the five-section dashboard. It fetches live data via /api/full-state."""
    here = Path(__file__).resolve().parent.parent
    # Prefer the canonical copy that lives next to this module; fall back to the
    # project-root copy for backward compatibility. (The two must stay in sync;
    # the local one is the maintained source of truth.)
    local_dash = Path(__file__).resolve().parent / "platform_c_dashboard.html"
    dash = local_dash if local_dash.exists() else here / "platform_c_dashboard.html"
    if dash.exists():
        return dash.read_text(encoding="utf-8")
    template = (here / "control_room_template.html").read_text(encoding="utf-8")
    state = build_dashboard_state(runs_root)
    state["_live"] = True
    return template.replace("__STATE__", json.dumps(state, ensure_ascii=False))


def _zip_knowledge(run_id: str, runs_root: str) -> bytes | None:
    """Zip a run's knowledge/ tree; returns None when the package is absent."""
    knowledge = Path(runs_root) / run_id / "knowledge"
    if not knowledge.exists():
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(knowledge.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=str(Path(run_id) / f.relative_to(knowledge.parent)))
    return buf.getvalue()


def _read_json_safe(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _inv_root(runs_root: str) -> Path:
    """The independent inventory-runs root (sibling of runs/)."""
    return Path(runs_root).resolve().parent / "inventory_runs"


def _inv_benchmark_state(inv_dir: Path, inv_id: str) -> dict:
    """Shape an INV-run's saved benchmark/promotion into the dashboard state
    the Benchmark/Feedback/Packages tabs already render (adapted profile)."""
    bench = _read_json_safe(inv_dir / "benchmark.json")
    promo = _read_json_safe(inv_dir / "promotion.json")
    rm = bench.get("retrieval_metrics", {}) if bench else {}
    docs = []
    for d in (bench.get("documents", []) if bench else []):
        cats = d.get("category_scores", {})
        pol, bm, ch = (cats.get("policy_compliance", 0), cats.get("bm25_readiness", 0),
                       cats.get("chunking_quality", 0))
        docs.append({
            "id": d.get("document_id", ""), "type": "doc",
            "artifact_type": d.get("document_type", "interface_inventory"),
            "policy": pol, "bm25": bm, "chunking": ch,
            "file": d.get("filename", ""), "version": "v1",
            "overall": d.get("overall_score") or round((pol + bm + ch) / 3),
        })
    return {
        "run_id": inv_id,
        "benchmark": {
            "overall": bench.get("overall_score", 0) if bench else 0,
            "status": bench.get("status", "—") if bench else "—",
            "profile": bench.get("profile", "interface_inventory") if bench else "interface_inventory",
            "faithfulness_status": bench.get("faithfulness_status", "") if bench else "",
            "categories": bench.get("category_scores", {}) if bench else {},
            "metrics": {
                "hit_at_3": rm.get("hit_at_3", 0), "mrr": rm.get("mrr", 0),
                "context_recall": rm.get("context_recall", 0),
                "noise_ratio": rm.get("noise_ratio", 0),
                "evaluated_queries": bench.get("query_count", rm.get("evaluated_queries", 0)) if bench else 0,
            },
            "documents": docs,
        },
        "feedback": {
            "decision": promo.get("promotion_decision", "—") if promo else "—",
            "items": promo.get("feedback", []) if promo else [],
            "failing_count": (promo.get("failing_document_count", len(promo.get("feedback", [])))
                              if promo else 0),
        },
        "packages": docs,
    }


def _hints_for_doc(run_dir: Path, fname: str) -> dict:
    """Find the regeneration hints for one document from promotion.json."""
    promo = _read_json_safe(run_dir / "promotion.json")
    doc_id = Path(fname).name
    for fb in (promo.get("feedback", []) if promo else []):
        fb_file = fb.get("filename") or ""
        did = fb.get("document_id") or ""
        if fb_file and Path(fb_file).name == doc_id:
            return fb.get("regeneration_hints", {})
        if did and (did in doc_id or doc_id in did or
                    did.replace("-", "_").upper() in doc_id.upper()):
            return fb.get("regeneration_hints", {})
    return {}


def _doc_content(run_id: str, fname: str, runs_root: str) -> dict | None:
    """Read one document by basename under the run's knowledge/ (traversal-safe)."""
    safe = Path(fname).name  # drop any path components -> no traversal
    knowledge = Path(runs_root) / run_id / "knowledge"
    if not knowledge.exists():
        return None
    hit = next((p for p in knowledge.rglob("*.md") if p.name == safe), None)
    if not hit:
        return None
    return {"file": safe, "content": hit.read_text(encoding="utf-8"),
            "subdir": hit.parent.name}


_INVENTORY_SYSTEMS = ("cpi", "apim", "aem", "pipo", "s4h")


def _zip_inventory(system: str, inventory_root: str = "inventory") -> bytes | None:
    """Zip ./inventory/<system>/ ; returns None when nothing has been exported."""
    sys = Path(system).name.lower()
    root = Path(inventory_root) / sys
    if not root.exists() or not any(root.iterdir()):
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=str(Path(sys) / f.relative_to(root)))
    return buf.getvalue()


def route_get(path: str, query: dict, runs_root: str) -> Response:
    """Pure GET routing. Returns a Response; no socket involved."""
    if path in ("/", "/index.html"):
        try:
            return Response.htmlr(200, _render_page(runs_root))
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/state":
        return Response.jsonr(200, build_dashboard_state(runs_root))
    if path == "/api/monitoring":
        return Response.jsonr(200, build_monitoring_metrics(runs_root))
    if path == "/api/connections":
        try:
            from platform_a.connectors.connections import probe_all
            return Response.jsonr(200, probe_all())
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/full-state":
        focus = (query.get("focus", [""])[0]).strip() or None
        months_raw = (query.get("months", [""])[0]).strip()
        months = [m for m in months_raw.split(",") if m] or None
        return Response.jsonr(200, build_full_dashboard(
            runs_root, focus_run=focus, archive_months=months))
    if path == "/api/mpl-archive/months":
        # available MPL archive months (mpl_MM_YYYY.json) for the loader selector
        try:
            from platform_a.connectors.error_corpus import (
                default_store_root, list_archive_months)
            return Response.jsonr(200, {
                "months": list_archive_months(default_store_root(runs_root))})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/runs":
        # list runs that already contain generated documents (newest first),
        # for the Benchmark RUN selector
        root = Path(runs_root)
        runs = []
        if root.exists():
            for d in sorted(root.glob("RUN-*"), reverse=True):
                knowledge = d / "knowledge"
                docs = list(knowledge.rglob("*.md")) if knowledge.exists() else []
                if not docs:
                    continue
                card = build_run_card(d) or {}
                runs.append({
                    "run_id": d.name,
                    "documents": len(docs),
                    "completed_at": card.get("completed_at") or card.get("started_at") or "",
                    "has_benchmark": (d / "benchmark.json").exists(),
                })
        return Response.jsonr(200, {"runs": runs})
    if path == "/api/inventory/runs":
        # list independent INV-runs (newest first) for the inventory benchmark picker
        root = _inv_root(runs_root)
        inv_runs = []
        if root.exists():
            for d in sorted(root.glob("INV-*"), reverse=True):
                knowledge = d / "knowledge"
                docs = list(knowledge.rglob("*.md")) if knowledge.exists() else []
                if not docs:
                    continue
                manifest = _read_json_safe(d / "inventory_manifest.json")
                inv_runs.append({
                    "inv_run_id": d.name,
                    "documents": len(docs),
                    "generated_at": manifest.get("generated_at", ""),
                    "mpl_source": manifest.get("mpl_source", ""),
                    "has_benchmark": (d / "benchmark.json").exists(),
                })
        return Response.jsonr(200, {"inv_runs": inv_runs})
    if path.startswith("/api/inventory/") and path.endswith("/download") \
            and path.split("/")[3].startswith("INV-"):
        inv_id = path.split("/")[3]
        data = _zip_knowledge(inv_id, str(_inv_root(runs_root)))
        if data is None:
            return Response.jsonr(404, {"error": "no knowledge package for this INV run"})
        return Response(200, data, "application/zip",
                        {"Content-Disposition": f'attachment; filename="{inv_id}_knowledge.zip"'})
    if path.startswith("/api/inventory/") and path.endswith("/doc") \
            and path.split("/")[3].startswith("INV-"):
        inv_id = path.split("/")[3]
        fname = (query.get("file", [""])[0]).strip()
        doc = _doc_content(inv_id, fname, str(_inv_root(runs_root)))
        if doc is None:
            return Response.jsonr(404, {"error": "document not found"})
        return Response.jsonr(200, doc)
    if path == "/api/scan":
        source = (query.get("source", [""])[0]).strip()
        platform = (query.get("platform", [""])[0]).strip() or None
        if not source:
            return Response.jsonr(400, {"error": "source query param required"})
        try:
            return Response.jsonr(200, scan_source(source, platform))
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/platforms":
        return Response.jsonr(200, {"platforms": enabled_platforms()})
    if path.startswith("/api/runs/") and path.endswith("/download"):
        run_id = path.split("/")[3]
        data = _zip_knowledge(run_id, runs_root)
        if data is None:
            return Response.jsonr(404, {"error": "no knowledge package for this run"})
        return Response(200, data, "application/zip",
                        {"Content-Disposition": f'attachment; filename="{run_id}_knowledge.zip"'})
    if path.startswith("/api/runs/") and path.endswith("/doc"):
        run_id = path.split("/")[3]
        fname = (query.get("file", [""])[0]).strip()
        doc = _doc_content(run_id, fname, runs_root)
        if doc is None:
            return Response.jsonr(404, {"error": "document not found"})
        return Response.jsonr(200, doc)
    if path.startswith("/api/runs/") and path.endswith("/results"):
        # READ-ONLY: return the already-saved benchmark/feedback/packages for a
        # run without recomputing anything. Powers the "Display existing"
        # choice when a run was already benchmarked on a prior platform version.
        run_id = path.split("/")[3]
        run_dir = Path(runs_root) / run_id
        if not run_dir.exists():
            return Response.jsonr(404, {"error": "run not found"})
        if not (run_dir / "benchmark.json").exists():
            return Response.jsonr(409, {"error": "no saved benchmark",
                                        "message": "this run has not been benchmarked yet — run a benchmark first"})
        return Response.jsonr(200, {
            "run_id": run_id,
            "source": "existing",
            "full_state": build_full_dashboard(runs_root, focus_run=run_id),
        })
    if path.startswith("/api/runs/") and path.endswith("/category-diagnostics"):
        # READ-ONLY (v15.3, Proposal 2): explain the weak weighted categories of
        # an already-saved benchmark (cause & action), without recomputing or
        # rewriting anything. Powers the Benchmark "weak categories" panel.
        run_id = path.split("/")[3]
        run_dir = Path(runs_root) / run_id
        if not run_dir.exists():
            return Response.jsonr(404, {"error": "run not found"})
        bench = _read_json_safe(run_dir / "benchmark.json")
        if not bench:
            return Response.jsonr(409, {"error": "no saved benchmark",
                                        "message": "this run has not been benchmarked yet — run a benchmark first"})
        from platform_b.feedback.corrective_feedback import build_category_diagnostics
        bench.setdefault("run_id", run_id)
        threshold = int((query.get("threshold", ["80"]) or ["80"])[0])
        return Response.jsonr(200, build_category_diagnostics(bench, threshold=threshold))
    if path.startswith("/api/inventory/") and path.endswith("/download"):
        system = path.split("/")[3].lower()
        if system not in _INVENTORY_SYSTEMS:
            return Response.jsonr(404, {"error": f"unknown system '{system}'"})
        data = _zip_inventory(system)
        if data is None:
            return Response.jsonr(404, {"error": "system unavailable",
                                        "message": "no inventory exported for this system yet"})
        return Response(200, data, "application/zip",
                        {"Content-Disposition": f'attachment; filename="{system}_inventory.zip"'})
    if path == "/api/scenario-lab/sources":
        try:
            store = _scenario_store(runs_root)
            return Response.jsonr(200, {"sources": store.list_sources()})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/scenario-lab/errors":
        system_id = (query.get("system_id", [""])[0]).strip()
        if not system_id:
            return Response.jsonr(400, {"error": "system_id query param required"})
        store = _scenario_store(runs_root)
        return Response.jsonr(200, {"errors": store.first_seen_errors(system_id)})
    if path == "/api/scenario-lab/candidates":
        status = (query.get("status", ["new"])[0]).strip() or None
        store = _scenario_store(runs_root)
        return Response.jsonr(200, {"candidates": store.candidate_pairs(status)})
    if path == "/api/scenario-lab/evidence":
        # Signals Evidence drill-down (F1): System > Package > iFlow > Cases.
        # Optional ?system_id= narrows to one system; else all systems.
        system_id = (query.get("system_id", [""])[0]).strip() or None
        try:
            store = _scenario_store(runs_root)
            return Response.jsonr(200, {"evidence": store.evidence_tree(system_id)})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/scenario-lab/predictions":
        # F3: predicted test matrices for a system (Case Prediction).
        system_id = (query.get("system_id", [""])[0]).strip()
        if not system_id:
            return Response.jsonr(400, {"error": "system_id query param required"})
        try:
            store = _scenario_store(runs_root)
            return Response.jsonr(200, {"predictions": store.list_predictions(system_id)})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/scenario-lab/recent-paths":
        return Response.jsonr(200, {"paths": _recent_paths(runs_root)})
    if path.startswith("/api/runs/"):
        run_id = path.rsplit("/", 1)[-1]
        card = build_run_card(Path(runs_root) / run_id)
        if card:
            return Response.jsonr(200, card)
        return Response.jsonr(404, {"error": "run not found"})
    return Response.jsonr(404, {"error": "not found"})


def route_post(path: str, body: dict, runs_root: str) -> Response:
    """Pure POST routing. Returns a Response; no socket involved."""
    if path == "/api/mpl-archive/sync":
        # Controlled action: pull FAILED MPL from the live tenant(s) into the
        # monthly archive store, then return the refreshed full-state so the
        # corpus tile + archive selector reflect the newly persisted records.
        try:
            from platform_a.connectors.archive_sync import sync_live_mpl_to_store
            hours = int(body.get("hours", 24))
            summary = sync_live_mpl_to_store(runs_root, hours=hours)
            return Response.jsonr(200, {
                "sync": summary,
                "full_state": build_full_dashboard(runs_root),
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})
    if path == "/api/pipeline":
        opts = PipelineOptions(
            platform=body.get("platform", "all"),
            source=body.get("source", ""),
            threshold=int(body.get("threshold", 80)),
            stop_on_benchmark_failure=bool(body.get("stop_on_benchmark_failure", True)),
            # promotion runs automatically when the benchmark clears the
            # threshold (the pipeline gates promote on bench_passed itself)
            promote=bool(body.get("promote", True)),
            version=str(body.get("version", "v1")),
            duplicate_block=bool(body.get("duplicate_block", True)),
            ai_enrich=bool(body.get("ai_enrich", False)),
        )
        # always a fresh run_id unless the caller explicitly pins one
        run_id = body.get("run_id") or None
        try:
            exit_code, _ = run_full_pipeline(opts, runs_root, run_id=run_id)
            return Response.jsonr(200, {
                "exit_code": int(exit_code),
                "full_state": build_full_dashboard(runs_root),
                "state": build_dashboard_state(runs_root),
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/runs/") and path.endswith("/resume"):
        run_id = path.split("/")[3]
        try:
            run = PipelineRun.load(run_id, runs_root)
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "run not found"})
        opts = PipelineOptions(
            platform=",".join(run.platforms) or "all",
            source=body.get("source", ""),
            promote=bool(body.get("promote", True)),
            resume=True,
        )
        exit_code, _ = run_full_pipeline(opts, runs_root, run_id=run_id)
        return Response.jsonr(200, {"exit_code": int(exit_code),
                                    "full_state": build_full_dashboard(runs_root),
                                    "state": build_dashboard_state(runs_root)})

    if path.startswith("/api/runs/") and path.endswith("/generate-inventory"):
        # Generation screen: "Generate interface_inventory_docs" button.
        # Produces one atomic interface_inventory doc per iFlow observed in the
        # run (Section 4 from CPIView, Section 5 from MPL), P3 constant, written
        # to knowledge/DOC and recorded in generation.json / generation_inventory.json.
        run_id = path.split("/")[3]
        try:
            run = PipelineRun.load_or_adopt(run_id, runs_root)
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "run not found"})
        version = str(body.get("version", "v1"))
        duplicate_block = bool(body.get("duplicate_block", True))
        customer_id = str(body.get("customer_id", "RCA"))
        try:
            from orchestrator.handlers import inventory_handler
            res = inventory_handler(run, version=version,
                                    duplicate_block=duplicate_block,
                                    runs_root=runs_root, customer_id=customer_id)
            if not res.ok:
                return Response.jsonr(409, {
                    "error": res.status,
                    "message": "the run has no monitor.json to derive interfaces from — "
                               "generate pairs (or scan a source) first"})
            return Response.jsonr(200, {
                "run_id": run_id,
                "status": res.status,
                "metrics": res.metrics,
                "full_state": build_full_dashboard(runs_root, focus_run=run_id),
                "state": build_dashboard_state(runs_root),
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/runs/") and path.endswith("/benchmark"):
        run_id = path.split("/")[3]
        try:
            run = PipelineRun.load_or_adopt(run_id, runs_root)
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "run not found",
                                        "message": "the run has no knowledge documents to benchmark"})
        knowledge = run.run_dir / "knowledge"
        if not knowledge.exists() or not any(knowledge.rglob("*.md")):
            return Response.jsonr(409, {"error": "no generated documents in this run",
                                        "message": "select a RUN that already has generated documents"})
        try:
            from orchestrator.handlers import benchmark_handler, promote_handler
            threshold = int(body.get("threshold", 80))
            bench = benchmark_handler(run, threshold)
            # chain into promotion (feedback + decision) when the benchmark ran
            promo = promote_handler(run, threshold)
            run.save()
            return Response.jsonr(200, {
                "run_id": run_id,
                "benchmark_status": bench.status,
                "promotion_status": promo.status,
                "full_state": build_full_dashboard(runs_root, focus_run=run_id),
                "state": build_dashboard_state(runs_root),
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/runs/") and path.endswith("/regenerate"):
        run_id = path.split("/")[3]
        try:
            run = PipelineRun.load_or_adopt(run_id, runs_root)
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "run not found"})
        fname = (body.get("file") or "").strip()
        if not fname:
            return Response.jsonr(400, {"error": "file is required"})
        # locate this document's corrective feedback (regeneration hints)
        promo = _read_json_safe(run.run_dir / "promotion.json")
        hints = {}
        doc_id = Path(fname).name
        for fb in (promo.get("feedback", []) if promo else []):
            fb_file = (fb.get("filename") or "")
            did = (fb.get("document_id") or "")
            if fb_file and Path(fb_file).name == doc_id:
                hints = fb.get("regeneration_hints", {})
                break
            if did and (did in doc_id or doc_id in did or
                        did.replace("-", "_").upper() in doc_id.upper()):
                hints = fb.get("regeneration_hints", {})
                break
        try:
            from platform_a.generators.regenerate import regenerate_document
            mode = (body.get("mode") or "new").lower()
            result = regenerate_document(run.run_dir, fname, hints, run_id=run_id, mode=mode)
            return Response.jsonr(200, {
                "run_id": run_id, **result,
                "applied_hints": hints,
                "full_state": build_full_dashboard(runs_root, focus_run=run_id),
            })
        except FileNotFoundError as exc:
            return Response.jsonr(404, {"error": str(exc)})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/runs/") and path.endswith("/apply-feedback-group"):
        run_id = path.split("/")[3]
        try:
            run = PipelineRun.load_or_adopt(run_id, runs_root)
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "run not found"})
        promo = _read_json_safe(run.run_dir / "promotion.json")
        feedback = promo.get("feedback", []) if promo else []
        if not feedback:
            return Response.jsonr(409, {"error": "no corrective feedback",
                                        "message": "run a benchmark first to produce feedback"})
        use_ai = bool(body.get("use_ai", True))
        threshold = int(body.get("threshold", 80))
        mode = (body.get("mode") or "new").lower()
        try:
            from platform_a.generators.corrective_apply import apply_corrective_feedback
            from orchestrator.handlers import benchmark_handler, promote_handler
            corrected, failed, labels = [], [], []
            for fb in feedback:
                fname = fb.get("filename") or ""
                if not fname:
                    continue
                try:
                    res = apply_corrective_feedback(
                        run.run_dir, fname, fb.get("regeneration_hints", {}),
                        run_id=run_id, use_ai=use_ai, mode=mode)
                    corrected.append(res["new_file"])
                    labels.append(res.get("version_label", ""))
                except FileNotFoundError:
                    failed.append(fname)
            # re-run the cycle: Benchmark -> Feedback/Promote on the corrected set
            bench = benchmark_handler(run, threshold)
            promo2 = promote_handler(run, threshold)
            run.save()
            group_label = labels[0] if labels else ("Remediated" if mode == "same" else "Update")
            return Response.jsonr(200, {
                "run_id": run_id,
                "corrected_count": len(corrected),
                "corrected": corrected,
                "skipped": failed,
                "mode": mode,
                "version_label": group_label,
                "benchmark_status": bench.status,
                "promotion_status": promo2.status,
                "full_state": build_full_dashboard(runs_root, focus_run=run_id),
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/runs/") and path.endswith("/apply-feedback"):
        run_id = path.split("/")[3]
        try:
            run = PipelineRun.load_or_adopt(run_id, runs_root)
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "run not found"})
        fname = (body.get("file") or "").strip()
        if not fname:
            return Response.jsonr(400, {"error": "file is required"})
        hints = _hints_for_doc(run.run_dir, fname)
        use_ai = bool(body.get("use_ai", True))
        threshold = int(body.get("threshold", 80))
        mode = (body.get("mode") or "new").lower()
        try:
            from platform_a.generators.corrective_apply import apply_corrective_feedback
            from orchestrator.handlers import benchmark_handler, promote_handler
            result = apply_corrective_feedback(run.run_dir, fname, hints,
                                               run_id=run_id, use_ai=use_ai, mode=mode)
            # re-run the cycle so the corrected document is re-scored
            bench = benchmark_handler(run, threshold)
            promo2 = promote_handler(run, threshold)
            run.save()
            return Response.jsonr(200, {
                "run_id": run_id, **result,
                "benchmark_status": bench.status,
                "promotion_status": promo2.status,
                "full_state": build_full_dashboard(runs_root, focus_run=run_id),
            })
        except FileNotFoundError as exc:
            return Response.jsonr(404, {"error": str(exc)})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/inventory/") and path.split("/")[3].startswith("INV-") \
            and path.endswith("/apply-feedback"):
        # Correct ONE inventory doc (inventory-aware rebuild from CPIView+MPL),
        # then re-benchmark. Mirrors /api/runs/<id>/apply-feedback for INV runs.
        inv_id = path.split("/")[3]
        inv_root = _inv_root(runs_root)
        try:
            run = PipelineRun.load_or_adopt(inv_id, str(inv_root))
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "INV run not found"})
        fname = (body.get("file") or "").strip()
        if not fname:
            return Response.jsonr(400, {"error": "file is required"})
        hints = _hints_for_doc(run.run_dir, fname)
        use_ai = bool(body.get("use_ai", False))
        mode = (body.get("mode") or "new").lower()
        threshold = int(body.get("threshold", 80))
        try:
            from platform_a.generators.corrective_apply import apply_corrective_feedback
            from orchestrator.handlers import benchmark_handler, promote_handler
            result = apply_corrective_feedback(run.run_dir, fname, hints,
                                               run_id=inv_id, use_ai=use_ai, mode=mode)
            benchmark_handler(run, threshold)
            promote_handler(run, threshold)
            run.save()
            return Response.jsonr(200, {
                "inv_run_id": inv_id, **result,
                "full_state": _inv_benchmark_state(run.run_dir, inv_id),
            })
        except FileNotFoundError as exc:
            return Response.jsonr(404, {"error": str(exc)})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/inventory/") and path.split("/")[3].startswith("INV-") \
            and path.endswith("/regenerate"):
        # Regenerate ONE inventory doc from its feedback hints (inventory-aware).
        inv_id = path.split("/")[3]
        inv_root = _inv_root(runs_root)
        try:
            run = PipelineRun.load_or_adopt(inv_id, str(inv_root))
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "INV run not found"})
        fname = (body.get("file") or "").strip()
        if not fname:
            return Response.jsonr(400, {"error": "file is required"})
        hints = _hints_for_doc(run.run_dir, fname)
        mode = (body.get("mode") or "new").lower()
        try:
            from platform_a.generators.regenerate import regenerate_document
            result = regenerate_document(run.run_dir, fname, hints, run_id=inv_id, mode=mode)
            return Response.jsonr(200, {
                "inv_run_id": inv_id, **result,
                "full_state": _inv_benchmark_state(run.run_dir, inv_id),
            })
        except FileNotFoundError as exc:
            return Response.jsonr(404, {"error": str(exc)})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path == "/api/inventory/generate":
        # Independent interface_inventory generation (foundational layer).
        # Does NOT depend on any prior RUN/monitor.json — sources CPIView design
        # + MPL and writes to inventory_runs/INV-NNN-YYYYMMDD/.
        inv_root = Path(runs_root).resolve().parent / "inventory_runs"
        sap_sid = str(body.get("sap_sid") or body.get("source") or "CPI")
        if sap_sid.upper() in ("ALL",):
            sap_sid = "CPI"
        version = str(body.get("version", "v1"))
        duplicate_block = bool(body.get("duplicate_block", True))
        customer_id = str(body.get("customer_id", "RCA"))
        ai_enrich = bool(body.get("ai_enrich", False))
        try:
            from platform_a.generators.inventory_run import generate_inventory_run
            summary = generate_inventory_run(
                str(inv_root), sap_sid=sap_sid, customer_id=customer_id,
                version=version, duplicate_block=duplicate_block, ai_enrich=ai_enrich)
            return Response.jsonr(200, {
                "inv_run_id": summary["inv_run_id"],
                "status": summary["status"],
                "mpl_source": summary.get("mpl_source", ""),
                "ai_enrichment": summary.get("ai_enrichment", {}),
                "metrics": summary,
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/inventory/") and path.split("/")[3].startswith("INV-") \
            and path.endswith("/benchmark"):
        # Benchmark an independent INV-run (adapted interface_inventory profile is
        # auto-detected because every doc is interface_inventory), then feedback.
        inv_id = path.split("/")[3]
        inv_root = _inv_root(runs_root)
        try:
            run = PipelineRun.load_or_adopt(inv_id, str(inv_root))
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "INV run not found or has no documents"})
        try:
            from orchestrator.handlers import benchmark_handler, promote_handler
            threshold = int(body.get("threshold", 80))
            bench = benchmark_handler(run, threshold)
            promo = promote_handler(run, threshold)
            run.save()
            return Response.jsonr(200, {
                "inv_run_id": inv_id,
                "benchmark_status": bench.status,
                "promotion_status": promo.status,
                "full_state": _inv_benchmark_state(run.run_dir, inv_id),
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/inventory/") and path.split("/")[3].startswith("INV-") \
            and path.endswith("/apply-feedback-group"):
        # Correct every failing inventory doc (rebuilt from CPIView+MPL via the
        # inventory-aware regenerate path), then re-run Benchmark -> Feedback.
        inv_id = path.split("/")[3]
        inv_root = _inv_root(runs_root)
        try:
            run = PipelineRun.load_or_adopt(inv_id, str(inv_root))
        except FileNotFoundError:
            return Response.jsonr(404, {"error": "INV run not found"})
        promo = _read_json_safe(run.run_dir / "promotion.json")
        feedback = promo.get("feedback", []) if promo else []
        if not feedback:
            return Response.jsonr(409, {"error": "no corrective feedback",
                                        "message": "benchmark the INV run first to produce feedback"})
        mode = (body.get("mode") or "new").lower()
        threshold = int(body.get("threshold", 80))
        try:
            from platform_a.generators.corrective_apply import apply_corrective_feedback
            from orchestrator.handlers import benchmark_handler, promote_handler
            corrected, failed = [], []
            for fb in feedback:
                fname = fb.get("filename") or ""
                if not fname:
                    continue
                try:
                    res = apply_corrective_feedback(
                        run.run_dir, fname, fb.get("regeneration_hints", {}),
                        run_id=inv_id, use_ai=bool(body.get("use_ai", False)), mode=mode)
                    corrected.append(res["new_file"])
                except FileNotFoundError:
                    failed.append(fname)
            bench = benchmark_handler(run, threshold)
            promo2 = promote_handler(run, threshold)
            run.save()
            return Response.jsonr(200, {
                "inv_run_id": inv_id,
                "corrected_count": len(corrected),
                "corrected": corrected,
                "skipped": failed,
                "mode": mode,
                "benchmark_status": bench.status,
                "promotion_status": promo2.status,
                "full_state": _inv_benchmark_state(run.run_dir, inv_id),
            })
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path.startswith("/api/inventory/") and path.endswith("/export"):
        system = path.split("/")[3].lower()
        if system not in _INVENTORY_SYSTEMS:
            return Response.jsonr(404, {"error": f"unknown system '{system}'"})
        try:
            from platform_a.connectors.inventory_export import export_inventory
            result = export_inventory(system, inventory_root="inventory")
            code = 200 if result.get("status") == "ok" else 409
            return Response.jsonr(code, result)
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path == "/api/scenario-lab/harvest":
        system_id = (body.get("system_id") or "").strip()
        if not system_id:
            return Response.jsonr(400, {"error": "system_id is required"})
        mode = (body.get("mode") or "full").strip()
        local_source = (body.get("local_source") or "").strip() or None
        cpiview_db = (body.get("cpiview_db") or "").strip() or None
        triggered_by = (body.get("triggered_by") or "operator").strip()
        try:
            from platform_a.scenario_lab.service import harvest_system
            store = _scenario_store(runs_root)
            summary = harvest_system(store, system_id, mode,
                                     local_source=local_source,
                                     cpiview_db=cpiview_db,
                                     triggered_by=triggered_by)
            _remember_path(runs_root, local_source, cpiview_db)
            return Response.jsonr(200, {
                "summary": summary,
                "errors": store.first_seen_errors(system_id),
                "candidates": store.candidate_pairs("new"),
            })
        except ValueError as exc:
            return Response.jsonr(404, {"error": str(exc)})
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path == "/api/scenario-lab/harvest-corpus":
        # Stream C: pipe the unified Error Corpus (runs + MPL archive) into
        # Scenario Lab candidates, joined to interface_inventory and flagged
        # against existing KEDB/RB.
        triggered_by = (body.get("triggered_by") or "operator").strip()
        months = body.get("months") or None
        try:
            from platform_a.scenario_lab.service import (
                harvest_from_corpus, enrich_evidence_from_corpus)
            store = _scenario_store(runs_root)
            result = harvest_from_corpus(store, runs_root,
                                         months=months, triggered_by=triggered_by)
            result["candidates"] = store.candidate_pairs("new")
            # F2: forward the same corpus into the Signals Evidence index and
            # attach per-iFlow priors + priority (best-effort, never blocks).
            try:
                result["evidence_enrichment"] = enrich_evidence_from_corpus(
                    store, runs_root, months=months)
            except Exception as _e:
                result["evidence_enrichment"] = {"error": str(_e)}
            return Response.jsonr(200, result)
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path == "/api/scenario-lab/predict":
        # F3: predict & persist the test matrix (negative >=5 when design allows
        # + positive) for a system's iFlows. Produces material for MANUAL SAP CI
        # execution — nothing is executed here.
        system_id = (body.get("system_id") or "").strip()
        if not system_id:
            return Response.jsonr(400, {"error": "system_id required"})
        cpiview_db = (body.get("cpiview_db") or "").strip() or None
        try:
            from platform_a.scenario_lab.service import predict_for_system
            store = _scenario_store(runs_root)
            result = predict_for_system(store, system_id, cpiview_db=cpiview_db)
            result["predictions"] = store.list_predictions(system_id)
            return Response.jsonr(200, result)
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path == "/api/scenario-lab/generate-group":
        triggered_by = (body.get("triggered_by") or "operator").strip()
        try:
            from platform_a.scenario_lab.service import generate_group
            store = _scenario_store(runs_root)
            project_root = Path(runs_root).resolve().parent
            result = generate_group(store, project_root, triggered_by=triggered_by)
            return Response.jsonr(200, result)
        except Exception as exc:
            return Response.jsonr(500, {"error": str(exc)})

    if path == "/api/scenario-lab/candidate-status":
        system_id = (body.get("system_id") or "").strip()
        signature = (body.get("error_signature") or "").strip()
        status = (body.get("status") or "queued_for_generation").strip()
        if not (system_id and signature):
            return Response.jsonr(400, {"error": "system_id and error_signature required"})
        store = _scenario_store(runs_root)
        ok = store.mark_candidate(system_id, signature, status,
                                  kb_id=body.get("kb_document_id", ""),
                                  rb_id=body.get("rb_document_id", ""))
        if not ok:
            return Response.jsonr(404, {"error": "candidate not found"})
        return Response.jsonr(200, {"candidates": store.candidate_pairs("new")})

    return Response.jsonr(404, {"error": "not found"})


def _scenario_store(runs_root: str):
    """Scenario Lab store sibling to runs/ (shares the project data root)."""
    from platform_a.scenario_lab.service import default_store
    project_root = Path(runs_root).resolve().parent
    return default_store(project_root)


def _recent_paths(runs_root: str) -> list:
    """Recently used local export / CPIView paths (for the datalist)."""
    import json
    p = Path(runs_root).resolve().parent / "scenario_lab" / "recent_paths.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("paths", [])
    except Exception:
        return []


def _remember_path(runs_root: str, *paths) -> None:
    """Persist non-empty paths to the recent-paths list (most recent first)."""
    import json
    vals = [p for p in paths if p]
    if not vals:
        return
    base = Path(runs_root).resolve().parent / "scenario_lab"
    base.mkdir(parents=True, exist_ok=True)
    f = base / "recent_paths.json"
    existing = _recent_paths(runs_root)
    for v in vals:
        if v in existing:
            existing.remove(v)
        existing.insert(0, v)
    f.write_text(json.dumps({"paths": existing[:15]}, indent=2) + "\n", encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    runs_root = "runs"

    def log_message(self, *args):  # quiet
        pass

    def _emit(self, resp: Response):
        self.send_response(resp.code)
        ctype = resp.ctype
        if ctype.startswith("text/") or ctype == "application/json":
            ctype = ctype + "; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(resp.body)))
        for k, v in (resp.headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp.body)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        self._emit(route_get(parsed.path, parse_qs(parsed.query), self.runs_root))

    def do_POST(self):
        path = urlparse(self.path).path
        self._emit(route_post(path, self._json_body(), self.runs_root))


def serve(runs_root: str = "runs", port: int = 8765, open_browser: bool = True) -> None:
    Handler.runs_root = runs_root
    Path(runs_root).mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"QDKEP Control Room running at {url}")
    print(f"  runs directory: {Path(runs_root).resolve()}")
    print("  press Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping QDKEP server")
        server.shutdown()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="qdkep-server")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    serve(args.runs_root, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

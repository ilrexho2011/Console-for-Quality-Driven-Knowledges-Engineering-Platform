"""Shared stage handlers.

Both the isolated CLI commands and the pipeline orchestrator call these
handlers. They return typed StageResult objects (status + exit_code + metrics);
no stdout parsing, no subprocess. CLI wraps them and renders; the orchestrator
consumes the result directly.

Handlers contain no platform-specific branching: they operate on the canonical
contracts (normalized events, candidates, documents, benchmark result).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.run_state import ExitCode, PipelineRun, atomic_write_json


@dataclass
class StageResult:
    stage: str
    status: str
    exit_code: int
    output: str = ""
    metrics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == ExitCode.OK


def _load_raw(source: Path) -> list[dict]:
    files = sorted(source.rglob("*.json")) if source.is_dir() else [source]
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


def monitor_handler(run: PipelineRun, source: str, platform: str) -> StageResult:
    from platform_a.connectors.platform_detect import normalize_record
    src = Path(source)
    if not src.exists():
        return StageResult("monitor", "FAILED", ExitCode.MISSING_INPUT)
    raw = _load_raw(src)
    events, failed = [], 0
    for r in raw:
        try:
            ev = normalize_record(r, platform=platform if platform != "all" else None)
        except Exception:
            continue  # malformed never stops the batch
        if (r.get("status", r.get("Status", "")) or "").upper() == "FAILED":
            failed += 1
        events.append({"raw": r, "source_platform": ev.source_platform,
                       "object_name": ev.object_name, "taxonomy": ev.taxonomy})
    atomic_write_json(run.run_dir / "monitor.json",
                      {"run_id": run.run_id, "platform": platform,
                       "event_count": len(events), "failed_events": failed, "events": events})
    run.mark_stage("monitor", "COMPLETED", output="monitor.json",
                   run_status="MONITORED", current_stage="monitor")
    return StageResult("monitor", "COMPLETED", ExitCode.OK, "monitor.json",
                       {"events_total": len(events), "failed_events": failed})


_REG_EMPTY = {"", "n/a", "na", "none", "-", "—", "–"}


def _reg_clean(value) -> str:
    v = (str(value) if value is not None else "").strip()
    return "" if v.lower() in _REG_EMPTY else v


def _documented_error_from(rec, raw, kb, rb, number, version, run_id):
    """Build a registry DocumentedError from a generated pair + its raw MPL.

    Uses the real MPL record for guid/timestamps/sender/receiver/error (fields a
    document alone cannot always carry) and the pair's ids/filenames. Returns
    None if the registry package is unavailable.
    """
    try:
        from platform_a.registry import DocumentedError
        from platform_a.registry.backfill import _system_kind, _exception_of, _http_of
    except Exception:
        return None
    raw = raw if isinstance(raw, dict) else {}
    err = _reg_clean(raw.get("ErrorText", raw.get("error_text", ""))) or _reg_clean(getattr(rec, "symptom", ""))
    log_start = _reg_clean(raw.get("LogStart", raw.get("log_start", "")))
    obj = getattr(rec, "object_name", "")
    sap_sid = getattr(rec, "sap_sid", "")
    return DocumentedError(
        source_of_record="mpl_live",
        run_id=run_id,
        system_kind=_system_kind(sap_sid, obj),
        customer_id=getattr(rec, "customer_id", ""),
        system_id=getattr(rec, "system_id", ""),
        sap_sid=sap_sid,
        integration_flow_name=getattr(rec, "integration_flow_id", "") or getattr(rec, "interface_name", ""),
        integration_flow_id=getattr(rec, "integration_flow_id", ""),
        interface_id=getattr(rec, "interface_name", ""),
        package_name=getattr(rec, "package_name", ""),
        message_guid=_reg_clean(raw.get("MessageGuid", raw.get("dump_id", raw.get("request_id", "")))),
        correlation_id=_reg_clean(raw.get("CorrelationId", "")),
        log_start=log_start,
        log_end=_reg_clean(raw.get("LogEnd", raw.get("log_end", ""))),
        sender=_reg_clean(raw.get("Sender", raw.get("sender", ""))),
        receiver=_reg_clean(raw.get("Receiver", raw.get("receiver", ""))),
        status=_reg_clean(raw.get("Status", raw.get("status", ""))) or "FAILED",
        error_message=err,
        error_phrase=(obj or "").replace("_", " ").strip(),
        exception_class=_exception_of(err),
        http_status_code=_http_of(err),
        object_name=obj,
        taxonomy=getattr(rec, "error_pattern", ""),
        priority=getattr(rec, "priority", ""),
        kedb_id=getattr(kb, "id_value", ""),
        runbook_id=getattr(rb, "id_value", ""),
        pair_number=number,
        kb_filename=getattr(kb, "filename", ""),
        rb_filename=getattr(rb, "filename", ""),
        document_version=version,
        occurred_at=log_start,
        raw_payload=dict(raw),
        design_payload=dict(getattr(rec, "design_context", None)
                            or getattr(rec, "source_metadata", {}) or {}),
    )


def _forward_register(docs, runs_root) -> dict:
    """Best-effort registration of newly documented errors. Never raises."""
    info = {"enabled": True, "attempted": len(docs), "registered": 0,
            "skipped_duplicates": 0, "error": ""}
    docs = [d for d in docs if d is not None]
    if not docs:
        info["enabled"] = False
        return info
    try:
        from platform_a.registry import open_repository
        db = str(Path(runs_root) / "knowledge_registry.sqlite") if runs_root else None
        repo = open_repository(db)
    except Exception as exc:
        info["enabled"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"[:160]
        return info
    try:
        res = repo.upsert_many(docs)
        info["registered"] = res["inserted"]
        info["skipped_duplicates"] = res["skipped_duplicates"]
        info["registry_total"] = repo.count()
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"[:160]
    finally:
        try:
            repo.close()
        except Exception:
            pass
    return info


def generate_handler(run: PipelineRun, version: str = "v1",
                     duplicate_block: bool = True,
                     runs_root: str | None = None,
                     ai_enrich: bool = False) -> StageResult:
    from platform_a.generators.event_bridge import build_records_from_raw
    from platform_a.generators.manager import GenerationManager, DuplicatePolicy
    from platform_a.generators.pair_builder import IncrementalPairAllocator
    from platform_a.generators.error_artifacts import generate_pair

    monitor_path = run.run_dir / "monitor.json"
    if not monitor_path.exists():
        return StageResult("generate", "FAILED", ExitCode.MISSING_INPUT)
    try:
        monitor = json.loads(monitor_path.read_text(encoding="utf-8"))
    except Exception:
        return StageResult("generate", "FAILED", ExitCode.INVALID_SCHEMA)

    raw_records = [e["raw"] for e in monitor.get("events", [])]

    # Faza 6.3: cross-run dedup (opt-in via REGISTRY_DEDUPE_ENABLED). Load the set
    # of fine signatures already in the registry so a re-run does not re-document
    # the same patterns — while distinct iFlows sharing an exception class stay
    # documentable. Best-effort; empty when disabled/unavailable.
    already_documented = set()
    _dedupe_root = runs_root if runs_root is not None else str(run.run_dir.parent)
    if os.environ.get("REGISTRY_DEDUPE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from platform_a.registry import open_repository
            _repo = open_repository(str(Path(_dedupe_root) / "knowledge_registry.sqlite"))
            already_documented = _repo.documented_signatures()
            _repo.close()
        except Exception:
            already_documented = set()

    # Foundational inventory: enrich KB-RB with the latest INV-run context so
    # design/operational data lifts retrieval. Best-effort; {} when no INV-run.
    inventory_index = {}
    try:
        from platform_a.generators.inventory_index import load_inventory_index
        inventory_index = load_inventory_index(runs_root=_dedupe_root)
    except Exception:
        inventory_index = {}

    # F4: evidence-first generation. Source 2 FAILED + 1 SUCCESS from the Signals
    # Evidence index (F1/F2) and, when EVIDENCE_GATE_ENABLED, GATE OUT iFlows the
    # index knows about but cannot yet prove (< 2 FAILED or < 1 SUCCESS). Both are
    # best-effort: no index -> unchanged behaviour.
    evidence_provider = None
    try:
        from platform_a.scenario_lab.evidence_provider import (
            evidence_provider_from_runs_root)
        evidence_provider = evidence_provider_from_runs_root(_dedupe_root)
    except Exception:
        evidence_provider = None
    gate_not_ready = os.environ.get("EVIDENCE_GATE_ENABLED", "").strip().lower() \
        in ("1", "true", "yes", "on")

    try:
        records, candidates = build_records_from_raw(
            raw_records, already_documented=already_documented,
            inventory_index=inventory_index,
            evidence_provider=evidence_provider, gate_not_ready=gate_not_ready)
    except Exception:
        return StageResult("generate", "FAILED", ExitCode.GENERATION_FAILED)

    policy = DuplicatePolicy.BLOCK if duplicate_block else DuplicatePolicy.ALLOW
    mgr = GenerationManager(out_root=run.run_dir,
        duplicate_policy_by_type={"runbook": policy, "kedb": policy})

    # Optional AI section enrichment. Default OFF. When ON we build the enricher
    # from .env (AI_PROVIDER/AI_MODEL); if no provider/key is present it is a
    # no-op and generation stays fully deterministic and non-blocking.
    enricher = None
    enrich_provider = "none"
    enrich_enabled = False
    if ai_enrich:
        try:
            from platform_a.connectors.env_loader import load_env
            load_env()  # populate os.environ from .env without overriding real env
        except Exception:
            pass
        try:
            from platform_a.ai.section_enrich import SectionEnricher
            enricher = SectionEnricher.from_env()
            enrich_enabled = enricher.enabled()
            enrich_provider = enricher.provider.value
        except Exception:
            enricher = None

    # GLOBAL incremental numbering, continuing the sequence already on disk.
    # The allocator scans runs_root (default: the parent of this run dir), so a
    # new run picks up after the highest number ever generated.
    scan_root = runs_root if runs_root is not None else str(run.run_dir.parent)
    allocator = IncrementalPairAllocator(scan_root)

    # object_name -> a representative raw MPL record, for forward-registration
    _obj_to_raw: dict = {}
    for _e in monitor.get("events", []):
        _on = _e.get("object_name")
        if _on and _on not in _obj_to_raw:
            _obj_to_raw[_on] = _e.get("raw", {}) or {}

    pairs = 0
    pair_detail = []
    enriched_docs = 0
    _to_register = []
    for rec in records:
        number = allocator.next()
        kb, rb = generate_pair(rec, number=number, enricher=enricher)
        mgr.write(kb, version); mgr.write(rb, version)
        pairs += 1
        if (kb.enrichment_trace or {}).get("source") == "ai":
            enriched_docs += 1
        if (rb.enrichment_trace or {}).get("source") == "ai":
            enriched_docs += 1
        pair_detail.append({"object_name": rec.object_name,
                            "pair_number": number,
                            "kedb_id": kb.id_value, "runbook_id": rb.id_value,
                            "kedb_enrichment": kb.enrichment_trace,
                            "runbook_enrichment": rb.enrichment_trace})
        # forward-register this newly documented error (best-effort, below)
        _to_register.append(_documented_error_from(
            rec, _obj_to_raw.get(rec.object_name, {}), kb, rb, number, version, run.run_id))

    # register into the knowledge registry (source_of_record='mpl_live')
    registry_info = _forward_register(_to_register, scan_root)

    eligible = len(records)
    ineligible = len([c for c in candidates if not c.eligible])
    reasons: dict[str, int] = {}
    for c in candidates:
        if not c.eligible:
            reasons[c.reason_code] = reasons.get(c.reason_code, 0) + 1

    # Per-system eligibility breakdown (Eligible for KB+RB / Duplicate-in-batch
    # per platform), so the operator sees the KB+RB candidate signal for CPI,
    # APIM, AEM, PI/PO and S4H — not just the global total. Platform = the event's
    # REAL source_platform (reliable: APIM/AEM are CPI sub-sources but keep their
    # own source_platform), mapped to each candidate by its signature.
    try:
        from platform_a.connectors.platform_detect import normalize_record as _norm
        from platform_a.generators.eligibility import candidate_signature as _sig
        _sig_plat: dict[str, str] = {}
        for _r in raw_records:
            try:
                _ev = _norm(_r, platform=_r.get("source_platform"))
                _sig_plat[_sig(_ev)] = (_ev.source_platform or "CPI").upper()
            except Exception:
                continue
    except Exception:
        _sig_plat = {}

    eligibility_by_platform: dict[str, dict] = {}
    for c in candidates:
        plat = _sig_plat.get(getattr(c, "signature", ""), "CPI")
        b = eligibility_by_platform.setdefault(
            plat, {"eligible": 0, "ineligible": 0, "duplicate": 0})
        if c.eligible:
            b["eligible"] += 1
        else:
            b["ineligible"] += 1
            if c.reason_code == "DUPLICATE_PATTERN":
                b["duplicate"] += 1

    # Faza 6.4: coverage metrics — how much of the value-add landed on the docs.
    _design_covered = sum(1 for r in records if (getattr(r, "design_context", None) or {}).get("found"))
    _success_covered = sum(1 for r in records if getattr(r, "success_baseline", None))
    _two_failed = sum(1 for r in records if len(getattr(r, "failed_samples", None) or []) >= 2)
    coverage = {
        "design_context": f"{_design_covered}/{eligible}",
        "design_context_pct": round(100 * _design_covered / eligible) if eligible else 0,
        "success_baseline": f"{_success_covered}/{eligible}",
        "two_failed_samples": f"{_two_failed}/{eligible}",
        "evidence_gate_enabled": gate_not_ready,
        "evidence_index_iflows": (len(getattr(evidence_provider, "_flows", {}))
                                  if evidence_provider else 0),
        "held_insufficient_evidence": (reasons.get("INSUFFICIENT_EVIDENCE", 0)
                                       if gate_not_ready else 0),
    }

    summary = {"run_id": run.run_id, "version": version,
               "eligible_count": eligible,
               "ineligible_count": ineligible, "pairs": pairs,
               "documents_created": pairs * 2, "ineligibility_reasons": reasons,
               "eligibility_by_platform": eligibility_by_platform,
               "pairs_detail": pair_detail,
               "highest_pair_number": allocator.last,
               "ai_enrichment": {
                   "requested": bool(ai_enrich),
                   "active": bool(enrich_enabled),
                   "provider": enrich_provider,
                   "documents_enriched": enriched_docs,
               },
               "coverage": coverage,
               "registry": registry_info}

    # Faza 6.4: a standalone, machine-readable metrics artifact per run.
    try:
        import datetime as _dt
        atomic_write_json(run.run_dir / "pipeline-metrics.json", {
            "run_id": run.run_id,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "events_total": len(raw_records),
            "eligible": eligible, "ineligible": ineligible,
            "pairs": pairs, "documents_created": pairs * 2,
            "coverage": coverage, "registry": registry_info,
        })
    except Exception:
        pass

    if eligible == 0:
        summary["status"] = "COMPLETED_NO_CANDIDATES"
        atomic_write_json(run.run_dir / "generation.json", summary)
        run.mark_stage("generate", "COMPLETED", output="generation.json",
                       run_status="GENERATED_NO_CANDIDATES", current_stage="generate")
        return StageResult("generate", "COMPLETED_NO_CANDIDATES", ExitCode.OK,
                           "generation.json", summary)

    summary["status"] = "GENERATED"
    atomic_write_json(run.run_dir / "generation.json", summary)
    run.mark_stage("generate", "COMPLETED", output="documents/",
                   run_status="GENERATED", current_stage="generate")
    return StageResult("generate", "GENERATED", ExitCode.OK, "documents/", summary)


def _mpl_from_raw(raw: dict):
    """Map one raw MPL dict (as stored in monitor.json) to an MPLRecord.

    Tolerant of both CPI OData field names (MessageGuid/Status/LogEnd/...) and
    the lower_snake variants used by local exports. Never raises.
    """
    from platform_a.connectors.base import MPLRecord

    def g(*keys) -> str:
        for k in keys:
            v = raw.get(k)
            if v not in (None, ""):
                return str(v)
        return ""

    return MPLRecord(
        message_guid=g("MessageGuid", "message_guid", "dump_id", "request_id"),
        correlation_id=g("CorrelationId", "correlation_id"),
        integration_flow_name=g("IntegrationFlowName", "integration_flow_name",
                                "sap_object", "integration_artifact"),
        status=(g("Status", "status") or "").upper(),
        log_start=g("LogStart", "log_start", "timestamp"),
        log_end=g("LogEnd", "log_end"),
        sender=g("Sender", "sender"),
        receiver=g("Receiver", "receiver"),
        error_text=g("ErrorText", "error_text", "fault", "status_text"),
    )


def inventory_handler(run: PipelineRun, version: str = "v1",
                      duplicate_block: bool = True,
                      runs_root: str | None = None,
                      customer_id: str = "RCA") -> StageResult:
    """Generate interface_inventory docs for every interface observed in the run.

    Companion to generate_handler: one atomic interface_inventory doc per distinct
    integration_flow observed in monitor.json, with Section 4 filled from the
    CPIView design model and Section 5 from that flow's MPL evidence. Written to
    knowledge/DOC via the same GenerationManager (documentation -> DOC), and
    recorded in generation_inventory.json plus an interface_inventory block on
    generation.json. Priority is a constant P3 for the whole category.
    """
    from platform_a.connectors.platform_detect import normalize_record
    from platform_a.generators.event_bridge import _sap_sid_for_event
    from platform_a.generators.manager import GenerationManager, DuplicatePolicy
    from platform_a.generators.catalog_artifacts import (
        build_interface_inventory_record, generate_interface_inventory, _norm_name,
    )

    monitor_path = run.run_dir / "monitor.json"
    if not monitor_path.exists():
        return StageResult("inventory", "FAILED", ExitCode.MISSING_INPUT)
    try:
        monitor = json.loads(monitor_path.read_text(encoding="utf-8"))
    except Exception:
        return StageResult("inventory", "FAILED", ExitCode.INVALID_SCHEMA)

    # CPIView design provider (best-effort; None when disabled/absent).
    design_provider = None
    try:
        from platform_a.design.cpiview_provider import provider_from_env
        design_provider = provider_from_env()
    except Exception:
        design_provider = None

    # Group observed events by integration_flow, keeping identity + raw records.
    flows: dict[str, dict] = {}
    for e in monitor.get("events", []):
        raw = e.get("raw", {}) or {}
        try:
            ev = normalize_record(raw, platform=None)
        except Exception:
            continue
        flow = (ev.integration_flow_name or "").strip()
        if not flow:
            continue
        sap_sid = _sap_sid_for_event(ev, "CPI")
        bucket = flows.setdefault(flow, {
            "sap_sid": sap_sid,
            "source_platform": ev.source_platform,
            "raws": [],
        })
        bucket["raws"].append(raw)

    if not flows:
        summary = {"run_id": run.run_id, "version": version,
                   "interfaces": 0, "documents_created": 0, "docs": [],
                   "status": "COMPLETED_NO_INTERFACES"}
        atomic_write_json(run.run_dir / "generation_inventory.json", summary)
        run.mark_stage("inventory", "COMPLETED", output="generation_inventory.json",
                       run_status=run.status, current_stage="inventory")
        return StageResult("inventory", "COMPLETED_NO_INTERFACES", ExitCode.OK,
                           "generation_inventory.json", summary)

    policy = DuplicatePolicy.BLOCK if duplicate_block else DuplicatePolicy.ALLOW
    mgr = GenerationManager(out_root=run.run_dir,
                            duplicate_policy_by_type={"documentation": policy})

    docs_detail: list[dict] = []
    written = 0
    for flow, info in flows.items():
        sap_sid = info["sap_sid"]
        technology = "SAP CPI" if info["source_platform"] in ("CPI", "APIM", "AEM") else "SAP PI"
        object_name = _norm_name(flow)  # no sap_sid prefix (filename carries SYSTEM=sap_sid)
        design_ctx = {}
        if design_provider is not None:
            try:
                design_ctx = design_provider.for_flow(flow).as_payload()
            except Exception:
                design_ctx = {}
        pkg = design_ctx.get("package_id", "") if design_ctx else ""
        mpl_records = [_mpl_from_raw(r) for r in info["raws"]]

        rec = build_interface_inventory_record(
            customer_id=customer_id,
            system_id=f"{sap_sid}-{customer_id}",
            sap_sid=sap_sid,
            object_name=object_name,
            integration_flow_name=flow,
            package_name=pkg,
            package_id=pkg,
            design_context=design_ctx,
            mpl_records=mpl_records,
            source_mode="live_mpl",
            runtime_source_file=str((run.run_dir / "monitor.json").name),
            runtime_evidence_scope="exact_interface_match",
            runtime_evidence_modes="live_mpl",
            technology=technology,
        )
        art = generate_interface_inventory(rec)
        outcome, path = mgr.write(art, version)
        if outcome.value in ("written", "overwritten"):
            written += 1
        docs_detail.append({
            "object_name": object_name,
            "integration_flow_id": flow,
            "doc_id": art.id_value,
            "artifact_type": "interface_inventory",
            "priority": "P3",
            "filename": art.filename,
            "design_found": bool(design_ctx.get("found")),
            "mpl_events": len(mpl_records),
            "outcome": outcome.value,
        })

    summary = {"run_id": run.run_id, "version": version,
               "interfaces": len(flows), "documents_created": written,
               "docs": docs_detail, "status": "GENERATED"}
    atomic_write_json(run.run_dir / "generation_inventory.json", summary)

    # record an interface_inventory block on generation.json (best-effort merge)
    gen_path = run.run_dir / "generation.json"
    gen = _read_json_safe_local(gen_path)
    gen = gen if isinstance(gen, dict) else {}
    gen.setdefault("run_id", run.run_id)
    gen["interface_inventory"] = {
        "count": len(docs_detail),
        "documents_created": written,
        "docs": docs_detail,
    }
    atomic_write_json(gen_path, gen)

    run.mark_stage("inventory", "COMPLETED", output="generation_inventory.json",
                   run_status=run.status, current_stage="inventory")
    return StageResult("inventory", "GENERATED", ExitCode.OK,
                       "generation_inventory.json", summary)


def _read_json_safe_local(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def benchmark_handler(run: PipelineRun, threshold: int = 80) -> StageResult:
    from platform_b.benchmark.orchestrator import run_benchmark
    knowledge = run.run_dir / "knowledge"
    if not knowledge.exists():
        return StageResult("benchmark", "FAILED", ExitCode.MISSING_INPUT)
    result = run_benchmark(str(knowledge), package_id=run.run_id)
    atomic_write_json(run.run_dir / "benchmark.json", result)
    passed = result["overall_score"] >= threshold
    if passed:
        run.mark_stage("benchmark", "COMPLETED", output="benchmark.json",
                       run_status="BENCHMARKED", current_stage="benchmark")
        return StageResult("benchmark", "PASSED", ExitCode.OK, "benchmark.json",
                           {"overall_score": result["overall_score"]})
    run.mark_stage("benchmark", "FAILED", output="benchmark.json",
                   run_status="BENCHMARK_FAILED", current_stage="benchmark")
    return StageResult("benchmark", "FAILED", ExitCode.BENCHMARK_FAILED, "benchmark.json",
                       {"overall_score": result["overall_score"]})


def promote_handler(run: PipelineRun, threshold: int = 80) -> StageResult:
    from platform_b.feedback.corrective_feedback import build_feedback
    bench_path = run.run_dir / "benchmark.json"
    if not bench_path.exists():
        return StageResult("promote", "FAILED", ExitCode.MISSING_INPUT)
    bench = json.loads(bench_path.read_text(encoding="utf-8"))
    fb = build_feedback(bench, threshold=threshold)
    atomic_write_json(run.run_dir / "promotion.json", fb)
    if fb["promotion_decision"] == "promote":
        run.mark_stage("promote", "COMPLETED", output="promotion.json",
                       run_status="PROMOTED", current_stage="promote")
        return StageResult("promote", "COMPLETED", ExitCode.OK, "promotion.json",
                           {"promoted_documents": len(bench.get("documents", []))})
    run.mark_stage("promote", "BLOCKED", output="promotion.json",
                   run_status="PROMOTION_BLOCKED", current_stage="promote")
    return StageResult("promote", "BLOCKED", ExitCode.PROMOTION_BLOCKED, "promotion.json",
                       {"failing": fb["failing_document_count"]})

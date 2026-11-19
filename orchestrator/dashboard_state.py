"""Collect PipelineRuns into a single dashboard-consumable contract.

The dashboard (Platform C) consumes the output of build_dashboard_state(): the
current run plus previous runs, each with lifecycle, per-stage status and
duration, eligibility breakdown, documents, benchmark and promotion outcomes.

This reads the real run artifacts (pipeline_run.json, pipeline-summary.json,
generation.json, benchmark.json, promotion.json) — no data is invented.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _duration_seconds(start: str, end: str) -> float | None:
    a, b = _parse_iso(start), _parse_iso(end)
    if a and b:
        return round((b - a).total_seconds(), 2)
    return None


_ODATA_DATE_RE = re.compile(r"/Date\((-?\d+)")


def _fmt_mpl_ts(value: str) -> str:
    """Format an MPL log timestamp as 'dd-mm-yyyy HH-MM-SS'.

    Handles ISO strings and SAP OData v2 '/Date(epoch_ms)/' values (as returned
    by MessageProcessingLogs LogStart/LogEnd). Nothing is invented; when the
    value cannot be parsed, the original string is returned as-is.
    """
    if not value:
        return ""
    dt = None
    m = _ODATA_DATE_RE.search(str(value))
    if m:
        try:
            dt = datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
        except Exception:
            dt = None
    if dt is None:
        dt = _parse_iso(value)
    if dt is None:
        dt = _parse_dmy(value)
    if dt is not None:
        return dt.strftime("%d-%m-%Y %H:%M:%S")
    return value or ""


def _parse_dmy(value: str) -> datetime | None:
    """Parse 'dd-mm-YYYY HH:MM:SS' (format emitted by the S4H RFC collector)."""
    try:
        return datetime.strptime(str(value).strip(), "%d-%m-%Y %H:%M:%S")
    except Exception:
        return None


def _ts_sort_key(value: str) -> float:
    """Sortable epoch for any supported timestamp format; unknown/empty -> -inf."""
    if not value:
        return float("-inf")
    dt = None
    m = _ODATA_DATE_RE.search(str(value))
    if m:
        try:
            return int(m.group(1)) / 1000
        except Exception:
            dt = None
    dt = _parse_iso(value) or _parse_dmy(value)
    if dt is None:
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


_TS_KEYS = ("LogEnd", "log_end", "LogStart", "log_start",
            "ts", "timestamp", "event_time", "created_at")

# S4H: ST22 short dump -> DATUM/UZEIT, WE05 IDoc control record -> CREDAT/CRETIM
_S4H_DATE_TIME_KEYS = (("DATUM", "UZEIT"), ("dump_date", "dump_time"),
                       ("CREDAT", "CRETIM"), ("credat", "cretim"))


def _event_ts_raw(raw: dict, ev: dict) -> str:
    """Return the event timestamp for CPI MPL and S4H (ST22/WE05) events, or ''."""
    for k in _TS_KEYS:
        v = raw.get(k) or ev.get(k)
        if v:
            return str(v)
    for d_key, t_key in _S4H_DATE_TIME_KEYS:
        d = str(raw.get(d_key) or "").replace("-", "")
        if len(d) == 8 and d.isdigit():
            t = str(raw.get(t_key) or "000000").replace(":", "").ljust(6, "0")
            return f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:6]}"
    return ""


def _live_recent_failed(limit: int = 100, hours: int = 24) -> list[dict]:
    """All FAILED messages from the LIVE SAP CI (CPI) tenant in the last N hours,
    newest first (one row per message, deduped by MessageGuid).

    Feeds the scrollable "Recent Failed Messages (24-h Last Errors)" panel:
    queried live (Status eq 'FAILED' within the window, ordered by LogEnd desc).
    Returns [] when CPI is not live/reachable so the caller can fall back to the
    runs-based view. Never raises.
    """
    try:
        from datetime import timedelta
        from platform_a.connectors.env_loader import load_env
        from platform_a.connectors.connections import build_cpi_config, _placeholder
        load_env()
        cfg = build_cpi_config()
        if cfg is None or _placeholder(cfg.api_base_url):
            return []
        from platform_a.connectors.cpi_live import CPILiveSource
        src = CPILiveSource(cfg)
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        params = {
            "$filter": f"Status eq 'FAILED' and LogEnd gt datetime'{since}'",
            "$orderby": "LogEnd desc",
            "$top": str(max(limit, 100)),
            "$select": "MessageGuid,IntegrationFlowName,Status,LogStart,LogEnd",
            "$format": "json",
        }
        data = src._get("MessageProcessingLogs", params).json()
    except Exception:
        return []
    rows = (data.get("d", {}) or {}).get("results", []) if isinstance(data, dict) else []
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:  # already newest-first (LogEnd desc)
        iflow = (r.get("IntegrationFlowName") or "").strip()
        key = r.get("MessageGuid", "") or f"{iflow}|{r.get('LogEnd', '')}"
        if not key or key in seen:
            continue
        seen.add(key)
        guid = r.get("MessageGuid", "")
        err = ""
        try:
            err = (src.fetch_error_text(guid) or "").strip()
        except Exception:
            err = ""
        exc = ""
        m = re.search(r"([A-Za-z_][\w.]*(?:Exception|Error))", err)
        if m:
            exc = m.group(1).split(".")[-1]
        else:
            hm = re.search(r"\b([1-5][0-9]{2})\b", err)
            if hm:
                exc = f"HTTP{hm.group(1)}"
        ts_raw = r.get("LogEnd") or r.get("LogStart") or ""
        out.append({
            "guid": guid or "—",
            "iface": iflow,
            "exception": exc,
            "err": err[:220],
            "status": "FAILED",
            "sap_sid": "CPI",
            "ts": _fmt_mpl_ts(ts_raw),
        })
        if len(out) >= limit:
            break
    return out


def _stage_view(run_dir: Path, summary: dict, run: dict) -> list[dict]:
    """Per-stage view merging run state and summary metrics."""
    summary_stages = summary.get("stages", {}) if summary else {}
    run_stages = run.get("stages", {}) if run else {}
    out = []
    for name in ("monitor", "generate", "benchmark", "promote"):
        s = summary_stages.get(name, {})
        r = run_stages.get(name, {})
        out.append({
            "stage": name,
            "status": s.get("status") or r.get("status") or "PENDING",
            "metrics": {k: v for k, v in s.items() if k not in ("status", "output")},
            "output": s.get("output") or r.get("output", ""),
        })
    return out


def build_run_card(run_dir: Path) -> dict | None:
    """Build one dashboard card from a run directory."""
    run = _read_json(run_dir / "pipeline_run.json")
    if not run:
        return None
    summary = _read_json(run_dir / "pipeline-summary.json")
    generation = _read_json(run_dir / "generation.json") or {}
    benchmark = _read_json(run_dir / "benchmark.json") or {}
    promotion = _read_json(run_dir / "promotion.json") or {}

    started = (summary or {}).get("started_at", run.get("created_at", ""))
    completed = (summary or {}).get("completed_at", run.get("updated_at", ""))

    # eligibility breakdown
    eligible = generation.get("eligible_count", 0)
    ineligible = generation.get("ineligible_count", 0)
    reasons = generation.get("ineligibility_reasons", {})

    # documents
    documents = []
    for sub in ("RB", "KB"):
        d = run_dir / "knowledge" / sub
        if d.exists():
            for md in sorted(d.glob("*.md")):
                documents.append({"type": sub, "file": md.name})

    # benchmark failures (per-doc below threshold-ish issues)
    bench_docs = benchmark.get("documents", [])
    failing_docs = [
        {"document_id": fb.get("document_id"), "score": fb.get("overall_score")}
        for fb in promotion.get("feedback", [])
    ]

    return {
        "run_id": run.get("run_id"),
        "status": (summary or {}).get("status", run.get("status", "")),
        "current_stage": run.get("current_stage", ""),
        "platforms": run.get("platforms", []),
        "started_at": started,
        "completed_at": completed,
        "duration_seconds": _duration_seconds(started, completed),
        "stages": _stage_view(run_dir, summary or {}, run),
        "eligibility": {
            "eligible": eligible,
            "ineligible": ineligible,
            "reasons": reasons,
        },
        "documents": documents,
        "document_count": len(documents),
        "benchmark": {
            "overall_score": benchmark.get("overall_score"),
            "status": benchmark.get("status"),
            "document_count": len(bench_docs),
        },
        "promotion": {
            "decision": promotion.get("promotion_decision"),
            "failing_document_count": promotion.get("failing_document_count", 0),
            "failing_documents": failing_docs,
        },
        "options": (summary or {}).get("options", {}),
        # actionable next steps for the Control Room
        "can_resume": run.get("status") not in ("PROMOTED", "COMPLETED"),
        "can_retry_failed": run.get("status") in ("BENCHMARK_FAILED", "FAILED", "PROMOTION_BLOCKED"),
    }


def build_dashboard_state(runs_root: str) -> dict:
    """Collect all runs (most recent first) into the dashboard contract."""
    root = Path(runs_root)
    cards = []
    if root.exists():
        for run_dir in root.iterdir():
            if run_dir.is_dir():
                card = build_run_card(run_dir)
                if card:
                    cards.append(card)
    # most recent first by completed_at / started_at
    cards.sort(key=lambda c: c.get("completed_at") or c.get("started_at") or "", reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_count": len(cards),
        "current_run": cards[0] if cards else None,
        "previous_runs": cards[1:] if len(cards) > 1 else [],
        "runs": cards,
    }


def build_monitoring_metrics(runs_root: str) -> dict:
    """Aggregate monitoring indicators across all runs.

    Returns totals (failed events, eligible/ineligible patterns, documents),
    a breakdown by platform, the distribution of ineligibility reasons, and the
    set of distinct error patterns (object_names) observed.
    """
    root = Path(runs_root)
    totals = {"runs": 0, "events_total": 0, "failed_events": 0,
              "eligible": 0, "ineligible": 0, "documents": 0}
    by_platform: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    patterns: dict[str, int] = {}
    elig_by_plat: dict[str, dict] = {}   # per-system Eligible / Duplicate-in-batch

    if root.exists():
        for run_dir in root.iterdir():
            if not run_dir.is_dir():
                continue
            run = _read_json(run_dir / "pipeline_run.json")
            if not run:
                continue
            totals["runs"] += 1
            monitor = _read_json(run_dir / "monitor.json") or {}
            generation = _read_json(run_dir / "generation.json") or {}

            totals["events_total"] += monitor.get("event_count", 0)
            totals["failed_events"] += monitor.get("failed_events", 0)
            totals["eligible"] += generation.get("eligible_count", 0)
            totals["ineligible"] += generation.get("ineligible_count", 0)

            for r, c in (generation.get("ineligibility_reasons", {}) or {}).items():
                reasons[r] = reasons.get(r, 0) + c

            # per-system eligible / duplicate-in-batch, summed across runs
            for plat, b in (generation.get("eligibility_by_platform", {}) or {}).items():
                acc = elig_by_plat.setdefault(
                    plat, {"eligible": 0, "ineligible": 0, "duplicate": 0})
                for k in ("eligible", "ineligible", "duplicate"):
                    acc[k] += int(b.get(k, 0) or 0)

            # per-platform from monitored events
            for ev in monitor.get("events", []):
                plat = ev.get("source_platform", "UNKNOWN")
                p = by_platform.setdefault(plat, {"events": 0, "failed": 0, "patterns": set()})
                p["events"] += 1
                obj = ev.get("object_name", "")
                if obj:
                    p["patterns"].add(obj)
                    patterns[obj] = patterns.get(obj, 0) + 1
                raw = ev.get("raw", {})
                if (raw.get("status", raw.get("Status", "")) or "").upper() == "FAILED":
                    p["failed"] += 1

            # documents on disk
            for sub in ("RB", "KB"):
                d = run_dir / "knowledge" / sub
                if d.exists():
                    totals["documents"] += len(list(d.glob("*.md")))

    platforms = []
    for plat, v in sorted(by_platform.items()):
        platforms.append({"platform": plat, "events": v["events"],
                          "failed": v["failed"], "distinct_patterns": len(v["patterns"])})

    return {
        "totals": totals,
        "distinct_patterns": len(patterns),
        "by_platform": platforms,
        "eligibility_by_platform": elig_by_plat,
        "ineligibility_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "top_patterns": [{"object_name": k, "count": v}
                         for k, v in sorted(patterns.items(), key=lambda kv: -kv[1])[:10]],
    }


def scan_source(source: str, platform: str | None = None) -> dict:
    """Scan a single source path and return monitoring indicators WITHOUT generating.

    Normalizes every record, counts FAILED, and groups by derived pattern.
    """
    from .handlers import _load_raw
    from platform_a.connectors.platform_detect import normalize_record
    from pathlib import Path as _P

    raw_records = _load_raw(_P(source))
    patterns: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    failed = 0
    for r in raw_records:
        try:
            ev = normalize_record(r, platform=platform if platform not in ("", "all", None) else None)
        except Exception:
            continue
        by_platform[ev.source_platform] = by_platform.get(ev.source_platform, 0) + 1
        if ev.object_name:
            patterns[ev.object_name] = patterns.get(ev.object_name, 0) + 1
        if (r.get("status", r.get("Status", "")) or "").upper() == "FAILED":
            failed += 1
    return {
        "source": source,
        "records": len(raw_records),
        "failed": failed,
        "distinct_patterns": len(patterns),
        "by_platform": [{"platform": k, "events": v} for k, v in sorted(by_platform.items())],
        "patterns": [{"object_name": k, "count": v}
                     for k, v in sorted(patterns.items(), key=lambda kv: -kv[1])],
    }


def build_full_dashboard(runs_root: str, focus_run: str | None = None,
                         archive_months: list[str] | None = None,
                         store_root: str | None = None) -> dict:
    """Aggregate everything the five-section dashboard needs, from real runs.

    Sections: monitoring (failed/completed/interfaces/patterns + recent MPL),
    generation (eligible patterns -> pair IDs), benchmark (overall, categories,
    metrics, per-document scores), feedback (promotion decision + hints),
    packages (generated files with scores).

    When `focus_run` is given and that run exists, the benchmark / feedback /
    packages sections are scoped to that run instead of the most recent one.

    `archive_months` selects MPL Archive months (mpl_MM_YYYY.json) to fold into
    the unified Error Corpus so the "Error patterns" tile reflects the true
    diversity of signatures across runs + archive, not just the coarse
    object_name count of the latest harvest. Best-effort and additive: when the
    corpus cannot be built, the tile falls back to the object_name count.
    """
    root = Path(runs_root)
    monitoring = build_monitoring_metrics(runs_root)

    # Unified Error Corpus (Streams A/B): signature-keyed patterns across
    # runs + selected MPL archive months. Read-only, never fatal.
    corpus = None
    try:
        from platform_a.connectors.error_corpus import build_error_corpus
        corpus = build_error_corpus(runs_root, store_root=store_root,
                                    months=archive_months)
    except Exception:
        corpus = None
    state = build_dashboard_state(runs_root)
    current = state.get("current_run")
    if focus_run and (root / focus_run / "run.json").exists() or \
       (focus_run and (root / focus_run).exists()):
        focused_card = build_run_card(root / focus_run)
        if focused_card:
            current = focused_card

    # gather the most recent run's benchmark + feedback detail
    bench = {}
    feedback = []
    promotion_decision = None
    documents = []
    recent_mpl = []
    eligible_patterns = []
    rm = {}
    category_diagnostics = []  # v15.3: cause & action for weak weighted categories

    if current:
        run_dir = root / current["run_id"]
        bench = _read_json(run_dir / "benchmark.json") or {}
        promo = _read_json(run_dir / "promotion.json") or {}
        promotion_decision = promo.get("promotion_decision")
        feedback = promo.get("feedback", [])
        monitor = _read_json(run_dir / "monitor.json") or {}
        generation = _read_json(run_dir / "generation.json") or {}

        # v15.3 (Proposal 2): explain weak weighted categories under the bars.
        # Read-only/advisory — never rewrites governed documents.
        if bench:
            from platform_b.feedback.corrective_feedback import build_category_diagnostics
            _bench_for_diag = dict(bench)
            _bench_for_diag.setdefault("run_id", current["run_id"])
            category_diagnostics = build_category_diagnostics(
                _bench_for_diag, threshold=80
            ).get("weak_categories", [])

        # recent failed messages — the 5 most recent errors from 5 DIFFERENT
        # iFlows in SAP CI. v15.9: prefer the LIVE CPI tenant (latest per distinct
        # IntegrationFlowName, newest first). When CPI is not reachable, fall back
        # to the same distinct-iFlow view computed from runs/*/monitor.json — one
        # row per iFlow (the iFlow's latest failure), newest first, capped 5 per
        # system. Each row carries the real event date/time (LogEnd/LogStart).
        recent_mpl = _live_recent_failed()
        if not recent_mpl:
            _collected: list[dict] = []
            _seen: set[str] = set()
            _monitor_files = sorted(root.glob("*/monitor.json"),
                                    key=lambda p: p.stat().st_mtime, reverse=True)
            for _mf in _monitor_files:
                _mon = _read_json(_mf) or {}
                # Fallback for events that carry no own timestamp (e.g. S4H runs fed
                # from fixtures): the time the run captured them. Flagged via ts_source.
                _run_meta = _read_json(_mf.parent / "pipeline_run.json") or {}
                _captured_at = (_mon.get("captured_at") or _mon.get("created_at")
                                or _run_meta.get("created_at") or "")
                for ev in _mon.get("events", []):
                    raw = ev.get("raw", {})
                    status = (raw.get("status", raw.get("Status", "")) or "").upper()
                    if status != "FAILED":
                        continue
                    iflow = (raw.get("IntegrationFlowName")
                             or raw.get("integration_flow_name")
                             or ev.get("object_name", ""))
                    sid = ev.get("sap_sid", raw.get("sap_sid", ""))
                    ts_raw = _event_ts_raw(raw, ev)
                    ts_source = "event" if ts_raw else ""
                    if not ts_raw and _captured_at:
                        ts_raw, ts_source = _captured_at, "run_captured"
                    guid = raw.get("MessageGuid",
                                   raw.get("dump_id", raw.get("request_id", "")))
                    key = guid or f"{sid}|{iflow}|{ts_raw}"   # one row per message
                    if key in _seen:
                        continue
                    _seen.add(key)
                    _collected.append({
                        "guid": guid or "—",
                        "iface": iflow,
                        "exception": raw.get("exception", raw.get("Exception", "")),
                        "err": raw.get("error_text", raw.get("ErrorText", "")),
                        "status": status,
                        "sap_sid": sid,
                        "ts": _fmt_mpl_ts(ts_raw),
                        "ts_source": ts_source,
                        "_ts_sort": _ts_sort_key(ts_raw),
                    })
            _collected.sort(key=lambda r: r["_ts_sort"], reverse=True)
            _per_system: dict[str, int] = {}
            for row in _collected:
                sid = row.get("sap_sid") or "—"
                if _per_system.get(sid, 0) >= 100:   # last-24h view, scrollable
                    continue
                _per_system[sid] = _per_system.get(sid, 0) + 1
                row.pop("_ts_sort", None)
                recent_mpl.append(row)

        # per-document scores from benchmark
        for d in bench.get("documents", []):
            cats = d.get("category_scores", {})
            dtype = d.get("document_type", "")
            doc_id = d.get("document_id", "")
            pol = cats.get("policy_compliance", 0)
            bm = cats.get("bm25_readiness", 0)
            ch = cats.get("chunking_quality", 0)
            # per-doc readiness = mean of the three static categories the
            # benchmark scores per document (retrieval is package-level)
            overall = d.get("overall_score") or round((pol + bm + ch) / 3)
            fname = d.get("filename", doc_id + ".md")
            vmatch = re.search(r"_v(\d+)\.md$", fname)
            if dtype == "runbook" or str(doc_id).startswith("RB"):
                t = "runbook"
            elif dtype == "interface_inventory" or str(doc_id).startswith(("CAT", "DOC")):
                t = "doc"
            else:
                t = "kedb"
            documents.append({
                "id": doc_id,
                "type": t,
                "artifact_type": dtype or ("interface_inventory" if t == "doc" else t),
                "policy": pol,
                "bm25": bm,
                "chunking": ch,
                "file": fname,
                "version": f"v{vmatch.group(1)}" if vmatch else "v1",
                "overall": overall,
            })

        rm = bench.get("retrieval_metrics", {})

        # eligible patterns -> what would be produced
        for card in [current]:
            for doc in card.get("documents", []):
                eligible_patterns.append(doc)

    # interfaces watched = distinct patterns across platforms
    interfaces_by_platform = {p["platform"]: p["distinct_patterns"] for p in monitoring["by_platform"]}

    return {
        "monitoring": {
            "failed_messages": monitoring["totals"]["failed_events"],
            "completed": monitoring["totals"]["events_total"] - monitoring["totals"]["failed_events"],
            "events_total": monitoring["totals"]["events_total"],
            "interfaces_watched": monitoring["distinct_patterns"],
            "interfaces_by_platform": interfaces_by_platform,
            # tile now counts distinct SIGNATURES from the unified corpus
            # (runs + selected archive), so it reflects real diversity; falls
            # back to the coarse object_name count when the corpus is empty.
            "error_patterns": (corpus["distinct_patterns"]
                               if corpus and corpus["distinct_patterns"]
                               else monitoring["distinct_patterns"]),
            "error_patterns_by_object": monitoring["distinct_patterns"],
            "by_platform": monitoring["by_platform"],
            "recent_mpl": recent_mpl,
            "sources_connected": len(monitoring["by_platform"]),
            "corpus": corpus or {"distinct_patterns": 0, "patterns": [],
                                 "top_patterns": [], "by_source": {},
                                 "available_months": [], "selected_months": []},
        },
        "generation": {
            "eligible_patterns": monitoring["totals"]["eligible"],
            "ineligible": monitoring["totals"]["ineligible"],
            "ineligibility_reasons": monitoring["ineligibility_reasons"],
            "eligibility_by_platform": monitoring.get("eligibility_by_platform", {}),
            "top_patterns": monitoring["top_patterns"],
        },
        "benchmark": {
            "overall": bench.get("overall_score", 0),
            "status": bench.get("status", "—"),
            "profile": bench.get("profile", "standard"),
            "faithfulness_status": bench.get("faithfulness_status", ""),
            "categories": bench.get("category_scores", {}),
            "metrics": {
                "hit_at_3": rm.get("hit_at_3", rm.get("hit@3", 0)),
                "mrr": rm.get("mrr", 0),
                "context_recall": rm.get("context_recall", rm.get("recall", 0)),
                "noise_ratio": rm.get("noise_ratio", rm.get("noise", 0)),
                "evaluated_queries": bench.get("query_count", rm.get("evaluated_queries", 0)),
            },
            "documents": documents,
            "category_diagnostics": category_diagnostics,
        },
        "feedback": {
            "decision": promotion_decision,
            "items": feedback,
            "failing_count": len(feedback),
        },
        "packages": documents,
        "run_id": current["run_id"] if current else None,
        "run_count": state["run_count"],
        "runs": [{"run_id": r["run_id"], "status": r["status"]} for r in state["runs"]],
    }


if __name__ == "__main__":
    import sys
    state = build_dashboard_state(sys.argv[1] if len(sys.argv) > 1 else "runs")
    print(json.dumps(state, indent=2, ensure_ascii=False))

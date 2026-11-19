# Scenario Lab (QDKEP v16.0)

A pre-stage workspace between **Monitoring (01)** and **Generation (02)**. It
harvests preliminary material per connected source system into a governed
staging store, surfaces first-seen errors, and proposes **atomic KB-RB
candidates** (one error → one pair) for Generation to act on under approval.

## Where it sits in the loop

```
01 Monitoring → 02 Scenario Lab → 03 Generation → 04 Benchmark → 05 Feedback → 06 Packages
                      │                  ▲
                      └─ candidates ─────┘  (status = new, under approval)
```

Governance boundary: Scenario Lab is **staging, not knowledge**. It never
writes governed KB/RB documents and never indexes into the RAG system. It only
stages raw material and proposes candidates; Generation produces the governed
pair under explicit user approval.

## Architecture (native to the platform)

Built in the platform's own style — pure standard library, a JSON file-store,
the same first-seen / dedup discipline as `MPLStore`. No external database or
web framework is required for the live platform.

| Piece | Location |
|---|---|
| Staging store + signature logic | `platform_a/scenario_lab/store.py` |
| Harvest service (reuses connectors) | `platform_a/scenario_lab/service.py` |
| HTTP routes | `orchestrator/server.py` (`/api/scenario-lab/*`) |
| Dashboard tab | `platform_c_dashboard.html` (section `scenario`) |
| Tests | `tests/integration/test_scenario_lab.py` |

### Store layout

Created as a sibling of `runs/` (shares the project data root):

```
scenario_lab/
├── sources.json                  registry of source systems
├── runs.json                     harvest run audit trail
├── candidates.json               atomic KB-RB candidates (one per signature)
├── CPI-RCA/objects.json         per-system harvested objects
├── CPI-RCA/errors.json          error events + first-seen index
└── …                             one folder per system
```

## The atomic rule

`canonical_error_signature(interface, exception, phrase)` is a SHA-256 over the
normalized interface name, exception class, and error phrase, with volatile
fragments (UUIDs, guid-like ids, timestamps, long numeric ids) stripped so the
same error pattern collapses to one signature. The store seeds **exactly one**
candidate per `(system_id, signature)` — re-harvesting never duplicates.

First-seen detection keeps the **earliest** occurrence timestamp per signature,
so the bottom panel shows when each error was first observed.

## API

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/scenario-lab/sources` | selectable source systems |
| GET  | `/api/scenario-lab/errors?system_id=` | first-seen errors for a system |
| GET  | `/api/scenario-lab/candidates?status=new` | atomic candidates |
| POST | `/api/scenario-lab/harvest` | pull material into staging |
| POST | `/api/scenario-lab/candidate-status` | mark a candidate queued/generated |

### Harvest request

```json
{ "system_id": "CPI-RCA", "mode": "runtime_mpl",
  "local_source": "tests/fixtures/cpi/mpl_sample.json", "triggered_by": "operator" }
```

`local_source` is optional. When given, the harvest reads a local MPL export
via the platform's `LocalMPLSource`. Without it, live extraction is a controlled
action and yields an audited (possibly empty) run when no connector is configured.

## Using the tab

1. Open **Scenario Lab** in the Control Room.
2. Pick a source system (APIM/AEM appear as CPI sub-sources).
3. Choose harvest modes (design / config / MPL / params / full).
4. Optionally set a local export path, then click **Pull material**.
5. Review **New errors — first seen** (interface + exception + date/time).
6. Click **Generate** on a row to queue its atomic candidate toward Generation.

## Optional SQL Server backend

The live platform uses the JSON file-store like the rest of QDKEP. For
enterprises that want the staging mirrored in a relational store, a full MS SQL
Server 2022 schema (migrations, rollback, smoke test) and a PostgreSQL
reference are provided under `db/scenario_lab/`. These are optional and not
required to run the platform.

## Governance invariants (tested)

1. One error signature → exactly one atomic candidate (even on re-harvest).
2. Distinct signatures → distinct candidates.
3. Repeated occurrences dedup; first-seen keeps the earliest timestamp.
4. The first-seen panel shows one row per signature.
5. Staging writes **no** governed documents — only JSON staging artifacts.

All five are covered by `tests/integration/test_scenario_lab.py`.

---

## v15.1 additions

### CPIView enrichment (CPI)
Harvest can read a CPIView SQLite store via the optional `cpiview_db` parameter.
For each iFlow it stages an enriched object (channels, adapters, Groovy scripts,
mappings) and surfaces `iflow_validation` findings as design-time errors. The
reader is read-only (`platform_a/scenario_lab/cpiview_reader.py`).

### Preliminary priority
Each candidate and first-seen error gets a reviewable priority (P1..P4) from
`classify.py`, re-classified at generation time.

### Interface naming
`derive_interface_name` yields SAP-CPI-style names per kind (IFLOW/ICO/IDOC/
APIPROXY/EVENT); it never emits "UNKNOWN".

### Source order and attribution
Picker order: CPI → APIM → AEM → PIPO → S4. APIM/AEM are CPI sub-sources with
`sap_sid = "CPI"`.

### Generate Group
`POST /api/scenario-lab/generate-group` queues all `new` candidates as one
group, writing a `RUN-xxxx` folder with a `scenario_group.json` manifest and
moving candidates to `queued_for_generation`. It hands the group to Generation;
it does not write governed documents itself.

### Recent paths
`GET /api/scenario-lab/recent-paths` backs a `<datalist>` of recently used
local-export / CPIView paths (stored in `scenario_lab/recent_paths.json`).

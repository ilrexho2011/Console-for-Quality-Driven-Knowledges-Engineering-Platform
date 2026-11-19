# QDKEP Platform A — Knowledge Generation

Generates AI4BIZ-compliant knowledge artifacts (runbook, kedb, documentation /
interface_inventory) from SAP CPI/PI-PO sources and MPL evidence. Every output
is built against QDKEP Document Contract v1 and verified by the deterministic
package validator.

## Modules

- `models/artifact.py` — metadata block (fixed order), section model, renderer.
- `generators/error_artifacts.py` — KEDB + Runbook from ErrorRecords. Applies
  Rule A keyword anchoring and canonical-phrase anchoring.
- `generators/catalog_artifacts.py` — Catalog Docs + interface_inventory
  (both document_type=documentation).
- `generators/mpl_bridge.py` — turns failed MPL records into ErrorRecords,
  grouped by derived canonical pattern (one artifact = one pattern).
- `connectors/base.py` — read-only source contract, config models, MPL/inventory
  records, query model with Live/Local mode selection.
- `connectors/cpi_live.py` — live CPI MessageProcessingLogs OData v2 client,
  OAuth2 client-credentials, ErrorInformation navigation, inventory via
  IntegrationPackages.
- `connectors/local_mpl.py` — Local MPL mode and offline fallback from JSON.
- `ai/assistant.py` — optional, non-blocking AI enrichment (Claude / OpenAI /
  Gemini) with a deterministic no-op fallback.
- `build_package.py` — builds the full knowledge/ package (RB, KB, DOC).

## Security model

- No secret literals in source. OAuth client id/secret and AI API keys are read
  from environment variables (see `.env.example`).
- Connectors are read-only. Live MPL extraction is a controlled action selected
  by the caller (Live MPL vs Local MPL).
- AI assistance never blocks generation: without a key, the assistant returns
  input unchanged and the deterministic path produces the artifact.

## Modes

- Live MPL: `CPILiveSource(config)` pulls real MPL via OData.
- Local MPL: `LocalMPLSource(path)` reads downloaded MPL JSON; also the fallback.

## Run

    # from error + interface CSVs
    python -m platform_a.build_package sample_errors.csv sample_interfaces.csv

    # validate the generated package
    python /path/to/validate_ai4biz_knowledge_package.py generated_package/knowledge

## Output layout (matches AI4BIZ approved package)

    generated_package/knowledge/
      RB/   runbooks
      KB/   kedb
      DOC/  catalog docs + interface inventory

# Console-for-Quality-Driven-Knowledges-Engineering-Platform
A self-contained **testing system** for practising the closed-loop ingestion of
atomic KB/RB knowledge produced by the QDKEP platform: upload → quality scoring →
deduplication → chunking → embedding → indexing → retrieval → search → answering →
benchmark.

Built to mirror the native QDKEP execution plane: **pure Python standard library**,
`ThreadingHTTPServer` with `route_get`/`route_post`, a file-store working model,
and **Supabase (Postgres + pgvector + tsvector)** as the single vector + lexical store.

Single-user (admin) — no auth, no role tiers.

## Architecture

```text
platform_a/   parser.py     metadata + headings + canonical SHA-256 signature
              chunker.py    heading-aware chunking + 8-signal section_type
platform_b/   quality.py    static scoring (policy, BM25, chunking) — artifact-type-aware
              indexer.py    local sentence-transformers embeddings -> pgvector/tsvector
              retriever.py  hybrid SQL (pgvector + tsvector) + 8-signal rerank
              answerer.py   search + grounded AMS answering with citations
              benchmark.py  dynamic scoring (precision/recall/faithfulness/citation)
orchestrator/ db.py         Supabase persistence + dedup gate + audit
              handlers.py   endpoint logic (upload/search/answer/benchmark)
              server.py     stdlib HTTP server (route_get/route_post)
              platform_c_dashboard.html   admin UI
db/           schema.sql    Supabase schema (vector ext, 3 tables, indexes)
tests/        test_qdkep.py 40 tests, no network / no live DB
```

## Quality model (7 categories)

| Category | Weight | Phase |
| -------- | ------ | ----- |
| Ingestion policy compliance | 20% | static (Faza 1) |
| BM25 authoring readiness | 15% | static |
| Chunking quality | 15% | static |
| Retrieval precision | 20% | dynamic (Faza 4) |
| Retrieval recall | 15% | dynamic |
| Answer faithfulness | 10% | dynamic |
| Citation / evidence accuracy | 5% | dynamic |

Status: 90–100 enterprise_ready · 80–89 ready_minor · 70–79 conditional ·
60–69 needs_remediation · <60 not_ready. Documents scoring <60 are rejected, not indexed.

## Setup

### 1. Apply the schema to Supabase

No local `psql` needed: open your project at supabase.com → **SQL Editor** →
New query → paste the contents of `db/schema.sql` → **Run**.
(If you do have `psql`: `psql "$DATABASE_URL" -f db/schema.sql`.)

### 2. Configure your `.env`

Copy the template and fill in your real values:

```powershell
# Windows PowerShell
Copy-Item .env.example .env
notepad .env
```

```bash
# macOS / Linux
cp .env.example .env
```

`.env` (loaded automatically on server start — never commit it):

```text
DATABASE_URL=postgresql://postgres:YOUR_DB_PASSWORD@db.YOUR_PROJECT_REF.supabase.co:5432/postgres
ANTHROPIC_API_KEY=sk-ant-...
```

Shell environment variables, if set, take precedence over `.env`.

### 3. Install and run

```bash
pip install psycopg2-binary sentence-transformers anthropic
python -m orchestrator.server
# open http://127.0.0.1:8800  (health check: /health)
```

The embedding model (`all-MiniLM-L6-v2`, 384-dim) downloads on first use and runs
fully local thereafter — no embedding API cost.

> Note: `export VAR=...` is bash syntax. In PowerShell either use `.env` (above)
> or set a one-off var with `$env:DATABASE_URL = "..."`.

## Endpoints

| Method | Path | Body |
| ------ | ---- | ---- |
| GET | `/health` | — |
| GET | `/` | dashboard |
| POST | `/upload` | `{filename, text}` |
| POST | `/search` | `{query, top_k?, customer_id?, system_id?, interface_id?, artifact_type?}` |
| POST | `/answer` | `{query, ...filters}` |
| POST | `/benchmark` | `{query_set:[{query, filters?, relevant_doc_ids, top_k?}], static_categories?}` |
| GET | `/dashboard` | platform overview counters |
| GET | `/documents` | `?limit=&offset=` — document list |
| GET | `/document` | `?id=N` — full detail + audit |
| POST | `/delete` | `{id}` — delete a document |

## Tests

```bash
pip install pytest pytest-cov
python -m pytest        # 40 tests, coverage gate at 80%
```

## Design invariants (inherited from QDKEP)

- Dedup via canonical SHA-256 over normalized (system_id + object_name + error_pattern),
  volatile fragments stripped; enforced by `UNIQUE(customer_id, system_id, error_signature)`.
- Governance immutability — corrections write a new version; the DB never rewrites in place.
- Answers come only from indexed knowledge; no match → explicit not-found, no hallucination.
- Hard filters (customer_id, system_id) applied in SQL — no cross-customer leakage.

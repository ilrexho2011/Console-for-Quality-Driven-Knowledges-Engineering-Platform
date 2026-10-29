-- QDKEP Knowledge Console — Faza 1 schema (Supabase / Postgres)
-- Vector + lexical store, dedup gate, audit versioning.
-- Embedding dim = 384 (sentence-transformers all-MiniLM-L6-v2).

create extension if not exists vector;

-- ---------------------------------------------------------------------------
-- documents: nje rresht per artefakt atomik (KB / RB / DOC).
-- error_signature = SHA-256 mbi (system_id + object_name + error_pattern) i
-- normalizuar, me fragmente volatile (timestamp, MessageGuid, IP, port) te
-- stripuara para hash-it. Dedup gate eshte UNIQUE constraint me poshte.
-- ---------------------------------------------------------------------------
create table if not exists documents (
    id                  bigint generated always as identity primary key,
    filename            text        not null,
    environment         text        not null,
    customer_id         text        not null,
    system_id           text        not null,
    sap_sid             text,
    artifact_type       text        not null,   -- kedb | runbook | documentation | ...
    object_name         text        not null,
    priority            text        not null,
    interface_id        text,
    integration_flow_id text,
    package_name        text,
    version             text        not null default 'v1',
    error_signature     char(64)    not null,   -- canonical SHA-256
    quality_score       numeric(5,2),
    quality_status      text,                   -- enterprise_ready | ... | not_ready
    status              text        not null default 'draft',  -- draft|approved|indexed|rejected
    raw_text            text        not null,
    created_at          timestamptz not null default now(),
    -- Dedup gate: i njejti incident per te njejtin customer/system refuzohet.
    constraint uq_doc_signature unique (customer_id, system_id, error_signature)
);

-- ---------------------------------------------------------------------------
-- chunks: heading-aware chunks, me embedding (semantic) + tsv (lexical/BM25).
-- section_type drejton 8-signal reranking (error_signature, diagnosis_step, ...).
-- ---------------------------------------------------------------------------
create table if not exists chunks (
    id           bigint generated always as identity primary key,
    document_id  bigint      not null references documents(id) on delete cascade,
    chunk_index  int         not null,
    section_type text        not null default 'generic',
    content      text        not null,
    embedding    vector(384),
    tsv          tsvector,
    created_at   timestamptz not null default now()
);

create index if not exists idx_chunks_doc      on chunks(document_id);
create index if not exists idx_chunks_tsv       on chunks using gin(tsv);
create index if not exists idx_chunks_embedding on chunks
    using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- ---------------------------------------------------------------------------
-- audit_trail: governance immutability — kurre rishkrim in-place.
-- Korrigjimi prodhon version te ri; çdo veprim regjistrohet.
-- ---------------------------------------------------------------------------
create table if not exists audit_trail (
    id          bigint generated always as identity primary key,
    document_id bigint      not null references documents(id) on delete cascade,
    action      text        not null,   -- uploaded | status_changed | indexed | scored | rejected
    actor       text        not null default 'admin',
    detail      text,
    created_at  timestamptz not null default now()
);

create index if not exists idx_audit_doc on audit_trail(document_id);

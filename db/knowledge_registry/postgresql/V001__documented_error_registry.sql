-- =====================================================================
-- QDKEP  |  Knowledge Registry  |  "Documented Error Registry"
-- Quality-Driven Knowledge Engineering Platform / AI4BIZ ecosystem
-- Dialect: PostgreSQL 13+   (primary)
-- ---------------------------------------------------------------------
-- Purpose
--   A durable registry of the FUNDAMENTAL error information for every
--   error that has ALREADY been turned into an atomic KB-RB pair, plus
--   every new error (MPL / CPIView / logs) that feeds NEW atomic pairs
--   from now on. It complements, and does NOT replace, the JSON files:
--   the source JSON is preserved verbatim in raw_payload / design_payload.
--
--   Two population paths:
--     A) BACKFILL  - parse runs/RUN-NNN-YYYYMMDD/knowledge/{KB,RB}/*.md
--                    (and CPIView) for existing documented errors.
--     B) FORWARD   - the pipeline writes here whenever it documents a new
--                    error (MPL / CPIView JSON used to build a new pair).
--
--   When a field cannot be recovered it is left NULL / empty. Nothing is
--   fabricated (aligns with the ingestion policy and BM25 Rule 7/12).
--
-- Identifiers (MessageGuid, CorrelationId, integration_flow_id,
--   interface_id, package_name, KEDB/RB ids) are stored LITERALLY.
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- trigram search on error text

CREATE SCHEMA IF NOT EXISTS knowledge_registry;
SET search_path TO knowledge_registry, public;

-- ---------------------------------------------------------------------
-- Controlled vocabularies
-- ---------------------------------------------------------------------
-- system_kind: the five supported integration environments.
CREATE TYPE kr_system_kind AS ENUM ('CPI','PIPO','S4','APIM','AEM');

-- priority_level: incident priority carried into the KB-RB filename.
CREATE TYPE kr_priority_level AS ENUM ('P1','P2','P3','P4');

-- source_of_record: HOW this row was populated, for auditing + trust.
--   runs_backfill = reconstructed from an existing RUN-NNN document
--   mpl_live      = captured live from MPL during a fresh pipeline
--   cpiview       = enriched/sourced from the CPIView SQLite model
--   mixed         = combined (e.g. MPL runtime + CPIView design join)
CREATE TYPE kr_source_of_record AS ENUM ('runs_backfill','mpl_live','cpiview','mixed');

-- design_element_type: for the optional normalized design breakdown.
CREATE TYPE kr_design_element_type AS ENUM
    ('sender','receiver','channel','adapter','message_mapping',
     'router','script','content_modifier','other');

-- =====================================================================
-- 1) documented_error  --  ONE row per documented error occurrence
-- =====================================================================
CREATE TABLE documented_error (
    -- Surrogate identity ------------------------------------------------------
    error_uid            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Provenance / audit ------------------------------------------------------
    source_of_record     kr_source_of_record NOT NULL,
        -- where the data came from (backfill vs live vs cpiview vs mixed)
    run_id               TEXT,
        -- the RUN-NNN-YYYYMMDD folder that produced/holds the KB-RB pair
    registered_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- when this registry row was first written
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- last time the row was refreshed (re-observed / enriched)

    -- System & business context (literal enterprise identifiers) --------------
    system_kind          kr_system_kind NOT NULL,
        -- CPI | PIPO | S4 | APIM | AEM  (drives interpretation of fields)
    customer_id          TEXT,                    -- e.g. RCMO
    system_id            TEXT,                    -- e.g. CPI-RCMO
    sap_sid              TEXT,                    -- e.g. CPI / RO2 / S4D
    integration_flow_name TEXT,                   -- MPL IntegrationFlowName (literal)
    integration_flow_id  TEXT,                    -- CPI iFlow id (literal)
    interface_id         TEXT,                    -- canonical interface id
    package_name         TEXT,                    -- CPI package / API proxy / AEM package

    -- === THE REQUESTED FUNDAMENTAL ERROR FIELDS ==============================
    message_guid         TEXT,                    -- MPL MessageGuid  (literal; NULL if not recoverable)
    correlation_id       TEXT,                    -- MPL CorrelationId (literal)
    log_start            TIMESTAMPTZ,             -- LogStart (processing start)
    log_end              TIMESTAMPTZ,             -- LogEnd   (processing end; often NULL from a doc)
    sender               TEXT,                    -- Sender system/party (NULL/empty when 'n/a')
    receiver             TEXT,                    -- Receiver system/party (NULL/empty when 'n/a')
    status               TEXT,                    -- Status, e.g. FAILED
    error_message        TEXT,                    -- ErrorMessage: full literal error text
    -- ========================================================================

    -- Derived / supporting evidence -------------------------------------------
    error_phrase         TEXT,                    -- short canonical searchable phrase (BM25)
    exception_class      TEXT,                    -- e.g. HttpResponseException
    http_status_code     INTEGER,                 -- e.g. 404, 502 (NULL when N/A)
    adapter_type         TEXT,                    -- e.g. HTTP / SFTP / SOAP / IDoc (from design)
    communication_channel TEXT,                   -- channel name (from CPIView/ESR-DIR)

    -- Classification ----------------------------------------------------------
    object_name          TEXT,                    -- normalized object_name, e.g. CPI_HTTP_404
    taxonomy             TEXT,                    -- DOMAIN | CATEGORY | SUB | CODE
    priority             kr_priority_level,       -- P1..P4

    -- Link to the atomic KB-RB pair this error produced ------------------------
    kedb_id              TEXT,                    -- e.g. KEDB-P3-0237
    runbook_id           TEXT,                    -- e.g. RB-P3-0237 (same numeric component)
    pair_number          INTEGER,                 -- the shared numeric id (237)
    kb_filename          TEXT,                    -- KB_RCMO_CPI_..._P3_v1.md
    rb_filename          TEXT,                    -- RB_RCMO_CPI_..._P3_v1.md
    document_version     TEXT,                    -- v1 / v2 ...

    -- Raw preservation (JSON is kept, per requirement) ------------------------
    raw_payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- the original MPL/log JSON record, verbatim
    design_payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- CPIView/ESR-DIR design context: channels, scripts, mappings, routers

    -- Idempotency -------------------------------------------------------------
    occurred_at          TIMESTAMPTZ,             -- normalized time of occurrence (usually = log_start)
    content_hash         TEXT NOT NULL,           -- sha256 of the canonical identity+evidence

    CONSTRAINT ck_http_status CHECK (http_status_code IS NULL
                                     OR http_status_code BETWEEN 100 AND 599)
);

COMMENT ON TABLE documented_error IS
  'Fundamental error information for every error already turned into a KB-RB pair, plus new MPL/CPIView errors feeding new pairs. Source JSON preserved in raw_payload/design_payload; unrecoverable fields stay NULL.';

-- Idempotency: never register the exact same evidence twice.
CREATE UNIQUE INDEX uq_documented_error_hash
    ON documented_error (content_hash);

-- A concrete MessageGuid is unique per system when present (partial index
-- so many NULL/empty guids from backfill do not collide).
CREATE UNIQUE INDEX uq_documented_error_guid
    ON documented_error (system_id, message_guid)
    WHERE message_guid IS NOT NULL AND message_guid <> '';

-- Query indexes -----------------------------------------------------------
CREATE INDEX ix_de_system         ON documented_error (system_kind, occurred_at DESC);
CREATE INDEX ix_de_flow           ON documented_error (integration_flow_id);
CREATE INDEX ix_de_object         ON documented_error (object_name);
CREATE INDEX ix_de_kedb           ON documented_error (kedb_id);
CREATE INDEX ix_de_runbook        ON documented_error (runbook_id);
CREATE INDEX ix_de_run            ON documented_error (run_id);
CREATE INDEX ix_de_priority       ON documented_error (priority);
CREATE INDEX ix_de_errmsg_trgm    ON documented_error USING GIN (error_message gin_trgm_ops);
CREATE INDEX ix_de_raw_gin        ON documented_error USING GIN (raw_payload jsonb_path_ops);

-- =====================================================================
-- 2) documented_error_design_element  (OPTIONAL, normalized design)
--    One row per design item behind the error: sender, receiver,
--    channel/adapter, message_mapping, router (with condition), script.
--    Lets you query "which errors touch Groovy script X / mapping Y /
--    router condition Z" without unpacking design_payload each time.
-- =====================================================================
CREATE TABLE documented_error_design_element (
    element_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    error_uid        UUID NOT NULL REFERENCES documented_error(error_uid) ON DELETE CASCADE,
    element_type     kr_design_element_type NOT NULL,
    element_name     TEXT,               -- e.g. channel name / mapping name / script filename
    element_detail   TEXT,               -- e.g. adapter type+address, router condition expression
    ordinal          INTEGER,            -- position in the iFlow step sequence (when known)
    attributes       JSONB NOT NULL DEFAULT '{}'::jsonb   -- auth type, protocol, refs, etc.
);
COMMENT ON TABLE documented_error_design_element IS
  'Normalized design-time breakdown per documented error (sender/receiver/channel/adapter/mapping/router/script). Populated from CPIView or .iflw/ESR-DIR when available.';

CREATE INDEX ix_de_elem_error   ON documented_error_design_element (error_uid);
CREATE INDEX ix_de_elem_type    ON documented_error_design_element (element_type, element_name);

-- =====================================================================
-- 3) VIEW  --  the 7 requested fundamental fields, clean
-- =====================================================================
CREATE VIEW v_documented_error_core AS
SELECT
    error_uid,
    system_id,
    integration_flow_name,
    message_guid   AS "MessageGuid",
    log_start      AS "LogStart",
    log_end        AS "LogEnd",
    sender         AS "Sender",
    receiver       AS "Receiver",
    status         AS "Status",
    error_message  AS "ErrorMessage",
    kedb_id, runbook_id, run_id, source_of_record
FROM documented_error;
COMMENT ON VIEW v_documented_error_core IS
  'The fundamental error fields requested for the registry, one row per documented error.';

-- =====================================================================
-- 4) updated_at maintenance
-- =====================================================================
CREATE OR REPLACE FUNCTION kr_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_de_touch
    BEFORE UPDATE ON documented_error
    FOR EACH ROW EXECUTE FUNCTION kr_touch_updated_at();

COMMIT;

-- =====================================================================
-- SAMPLE QUERIES
-- =====================================================================
-- A) The registry, newest documented errors first:
-- SELECT "MessageGuid","LogStart","LogEnd","Sender","Receiver","Status","ErrorMessage"
-- FROM   v_documented_error_core
-- ORDER  BY "LogStart" DESC NULLS LAST;
--
-- B) Errors documented for one iFlow, with their KB-RB pair:
-- SELECT integration_flow_name, kedb_id, runbook_id, error_message
-- FROM   documented_error
-- WHERE  integration_flow_id = 'Get_Customers_from_NW_using_HTTP';
--
-- C) All errors that run through a given Groovy script:
-- SELECT d.kedb_id, d.error_message, e.element_name
-- FROM   documented_error d
-- JOIN   documented_error_design_element e ON e.error_uid = d.error_uid
-- WHERE  e.element_type = 'script' AND e.element_name ILIKE '%SetHeaders%';
--
-- D) Coverage of the fundamental fields (how much was recoverable):
-- SELECT source_of_record,
--        count(*)                                   AS total,
--        count(message_guid)                        AS have_guid,
--        count(sender)      FILTER (WHERE sender  <> '') AS have_sender,
--        count(receiver)    FILTER (WHERE receiver<> '') AS have_receiver,
--        count(log_end)                             AS have_log_end
-- FROM   documented_error
-- GROUP  BY source_of_record;

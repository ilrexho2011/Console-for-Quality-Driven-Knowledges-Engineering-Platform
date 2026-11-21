-- =====================================================================
-- QDKEP  |  Knowledge Registry  |  "Documented Error Registry"
-- Dialect: Microsoft SQL Server 2017+  (T-SQL variant)
-- ---------------------------------------------------------------------
-- Equivalent of the PostgreSQL V001 schema. Differences:
--   * ENUM types  -> CHECK constraints on NVARCHAR columns
--   * JSONB       -> NVARCHAR(MAX) with ISJSON() CHECK
--   * gen_random_uuid() -> NEWID()  (default)
--   * TIMESTAMPTZ -> DATETIME2 / DATETIMEOFFSET
--   * partial unique index -> filtered UNIQUE INDEX (WHERE ...)
-- Identifiers (MessageGuid, CorrelationId, iFlow ids, KEDB/RB ids) are
-- stored LITERALLY. Unrecoverable fields stay NULL. Nothing fabricated.
-- =====================================================================

IF SCHEMA_ID('knowledge_registry') IS NULL
    EXEC('CREATE SCHEMA knowledge_registry');
GO

-- =====================================================================
-- 1) documented_error
-- =====================================================================
CREATE TABLE knowledge_registry.documented_error (
    error_uid             UNIQUEIDENTIFIER NOT NULL CONSTRAINT pk_documented_error PRIMARY KEY
                              DEFAULT NEWID(),

    -- Provenance / audit ------------------------------------------------------
    source_of_record      NVARCHAR(20)  NOT NULL
        CONSTRAINT ck_de_source CHECK (source_of_record IN
            ('runs_backfill','mpl_live','cpiview','mixed')),
    run_id                NVARCHAR(64)  NULL,     -- RUN-NNN-YYYYMMDD folder
    registered_at         DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at            DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),

    -- System & business context (literal identifiers) ------------------------
    system_kind           NVARCHAR(8)   NOT NULL
        CONSTRAINT ck_de_kind CHECK (system_kind IN ('CPI','PIPO','S4','APIM','AEM')),
    customer_id           NVARCHAR(64)  NULL,     -- e.g. RCMO
    system_id             NVARCHAR(128) NULL,     -- e.g. CPI-RCMO
    sap_sid               NVARCHAR(16)  NULL,     -- e.g. CPI / RO2 / S4D
    integration_flow_name NVARCHAR(256) NULL,     -- MPL IntegrationFlowName
    integration_flow_id   NVARCHAR(256) NULL,     -- CPI iFlow id
    interface_id          NVARCHAR(256) NULL,
    package_name          NVARCHAR(256) NULL,

    -- === THE REQUESTED FUNDAMENTAL ERROR FIELDS =============================
    message_guid          NVARCHAR(128) NULL,     -- MessageGuid (literal)
    correlation_id        NVARCHAR(128) NULL,     -- CorrelationId (literal)
    log_start             DATETIME2(3)  NULL,     -- LogStart
    log_end               DATETIME2(3)  NULL,     -- LogEnd (often NULL from a doc)
    sender                NVARCHAR(256) NULL,     -- Sender (NULL/empty when 'n/a')
    receiver              NVARCHAR(256) NULL,     -- Receiver
    status                NVARCHAR(32)  NULL,     -- Status, e.g. FAILED
    error_message         NVARCHAR(MAX) NULL,     -- ErrorMessage (full literal text)
    -- =======================================================================

    -- Derived / supporting evidence ------------------------------------------
    error_phrase          NVARCHAR(512) NULL,
    exception_class       NVARCHAR(256) NULL,
    http_status_code      INT           NULL
        CONSTRAINT ck_de_http CHECK (http_status_code IS NULL
                                     OR http_status_code BETWEEN 100 AND 599),
    adapter_type          NVARCHAR(64)  NULL,     -- HTTP/SFTP/SOAP/IDoc (design)
    communication_channel NVARCHAR(256) NULL,     -- channel name (CPIView/ESR-DIR)

    -- Classification ---------------------------------------------------------
    object_name           NVARCHAR(256) NULL,     -- e.g. CPI_HTTP_404
    taxonomy              NVARCHAR(256) NULL,     -- DOMAIN | CATEGORY | SUB | CODE
    priority              NVARCHAR(2)   NULL
        CONSTRAINT ck_de_priority CHECK (priority IN ('P1','P2','P3','P4')),

    -- Link to the atomic KB-RB pair ------------------------------------------
    kedb_id               NVARCHAR(32)  NULL,     -- KEDB-P3-0237
    runbook_id            NVARCHAR(32)  NULL,     -- RB-P3-0237
    pair_number           INT           NULL,     -- 237
    kb_filename           NVARCHAR(256) NULL,
    rb_filename           NVARCHAR(256) NULL,
    document_version      NVARCHAR(8)   NULL,     -- v1 / v2

    -- Raw preservation (JSON kept as-is) -------------------------------------
    raw_payload           NVARCHAR(MAX) NOT NULL DEFAULT N'{}'
        CONSTRAINT ck_de_raw_json    CHECK (ISJSON(raw_payload) = 1),
    design_payload        NVARCHAR(MAX) NOT NULL DEFAULT N'{}'
        CONSTRAINT ck_de_design_json CHECK (ISJSON(design_payload) = 1),

    -- Idempotency ------------------------------------------------------------
    occurred_at           DATETIME2(3)  NULL,
    content_hash          CHAR(64)      NOT NULL  -- sha256 hex of canonical identity+evidence
);
GO

-- Idempotency: never register the same evidence twice.
CREATE UNIQUE INDEX uq_documented_error_hash
    ON knowledge_registry.documented_error (content_hash);
GO

-- MessageGuid unique per system when present (filtered index).
CREATE UNIQUE INDEX uq_documented_error_guid
    ON knowledge_registry.documented_error (system_id, message_guid)
    WHERE message_guid IS NOT NULL;
GO

CREATE INDEX ix_de_system   ON knowledge_registry.documented_error (system_kind, occurred_at DESC);
CREATE INDEX ix_de_flow     ON knowledge_registry.documented_error (integration_flow_id);
CREATE INDEX ix_de_object   ON knowledge_registry.documented_error (object_name);
CREATE INDEX ix_de_kedb     ON knowledge_registry.documented_error (kedb_id);
CREATE INDEX ix_de_runbook  ON knowledge_registry.documented_error (runbook_id);
CREATE INDEX ix_de_run      ON knowledge_registry.documented_error (run_id);
CREATE INDEX ix_de_priority ON knowledge_registry.documented_error (priority);
GO

-- =====================================================================
-- 2) documented_error_design_element  (OPTIONAL normalized design)
-- =====================================================================
CREATE TABLE knowledge_registry.documented_error_design_element (
    element_id     UNIQUEIDENTIFIER NOT NULL
                       CONSTRAINT pk_de_design_element PRIMARY KEY DEFAULT NEWID(),
    error_uid      UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_de_elem_error REFERENCES knowledge_registry.documented_error(error_uid)
        ON DELETE CASCADE,
    element_type   NVARCHAR(24) NOT NULL
        CONSTRAINT ck_de_elem_type CHECK (element_type IN
            ('sender','receiver','channel','adapter','message_mapping',
             'router','script','content_modifier','other')),
    element_name   NVARCHAR(256) NULL,   -- channel/mapping name, script filename
    element_detail NVARCHAR(MAX) NULL,   -- adapter type+address, router condition
    ordinal        INT           NULL,   -- step position in the iFlow (when known)
    attributes     NVARCHAR(MAX) NOT NULL DEFAULT N'{}'
        CONSTRAINT ck_de_elem_json CHECK (ISJSON(attributes) = 1)
);
GO

CREATE INDEX ix_de_elem_error ON knowledge_registry.documented_error_design_element (error_uid);
CREATE INDEX ix_de_elem_type  ON knowledge_registry.documented_error_design_element (element_type, element_name);
GO

-- =====================================================================
-- 3) VIEW  --  the 7 requested fundamental fields
-- =====================================================================
CREATE VIEW knowledge_registry.v_documented_error_core AS
SELECT
    error_uid,
    system_id,
    integration_flow_name,
    message_guid  AS [MessageGuid],
    log_start     AS [LogStart],
    log_end       AS [LogEnd],
    sender        AS [Sender],
    receiver      AS [Receiver],
    status        AS [Status],
    error_message AS [ErrorMessage],
    kedb_id, runbook_id, run_id, source_of_record
FROM knowledge_registry.documented_error;
GO

-- =====================================================================
-- 4) updated_at maintenance
-- =====================================================================
CREATE TRIGGER knowledge_registry.trg_de_touch
    ON knowledge_registry.documented_error
    AFTER UPDATE AS
BEGIN
    SET NOCOUNT ON;
    UPDATE d SET updated_at = SYSUTCDATETIME()
    FROM knowledge_registry.documented_error d
    INNER JOIN inserted i ON d.error_uid = i.error_uid;
END;
GO

/* =====================================================================
   Scenario Lab — Migration V002 — core staging tables
   ---------------------------------------------------------------------
   UUID PKs : NEWSEQUENTIALID() default (index-friendly on SQL Server).
   JSON     : NVARCHAR(MAX) + ISJSON() CHECK (no native JSONB).
   Time     : DATETIMEOFFSET(3) replaces TIMESTAMPTZ.
   Enums    : enforced via FK to scenario_lab.ref_* tables.
   ===================================================================== */

SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

IF EXISTS (SELECT 1 FROM scenario_lab.schema_migration WHERE version = 'V002')
BEGIN
    PRINT 'V002 already applied — skipping.';
    RETURN;
END
GO

BEGIN TRANSACTION;
GO

/* ---- 1) source systems -------------------------------------------- */
CREATE TABLE scenario_lab.scn_source_system (
    source_system_id    UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT df_src_id DEFAULT NEWSEQUENTIALID()
        CONSTRAINT pk_scn_source_system PRIMARY KEY,
    system_id           NVARCHAR(60)  NOT NULL,
    customer_id         NVARCHAR(60)  NOT NULL,
    sap_sid             NVARCHAR(10)  NULL,
    system_kind         VARCHAR(10)   NOT NULL
        CONSTRAINT fk_src_kind REFERENCES scenario_lab.ref_system_kind(code),
    parent_system_id    UNIQUEIDENTIFIER NULL
        CONSTRAINT fk_src_parent REFERENCES scenario_lab.scn_source_system(source_system_id),
    display_name        NVARCHAR(120) NOT NULL,
    monitoring_endpoint NVARCHAR(400) NULL,
    is_connected        BIT NOT NULL CONSTRAINT df_src_connected DEFAULT 0,
    is_active           BIT NOT NULL CONSTRAINT df_src_active DEFAULT 1,
    created_at          DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_src_created DEFAULT SYSDATETIMEOFFSET(),
    updated_at          DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_src_updated DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT uq_scn_source_system UNIQUE (customer_id, system_id)
);
GO

/* ---- 2) harvest run ------------------------------------------------ */
CREATE TABLE scenario_lab.scn_harvest_run (
    harvest_run_id   UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT df_run_id DEFAULT NEWSEQUENTIALID()
        CONSTRAINT pk_scn_harvest_run PRIMARY KEY,
    source_system_id UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_run_src REFERENCES scenario_lab.scn_source_system(source_system_id),
    mode             VARCHAR(20) NOT NULL
        CONSTRAINT fk_run_mode REFERENCES scenario_lab.ref_harvest_mode(code),
    mpl_source_mode  VARCHAR(10) NULL
        CONSTRAINT fk_run_mpl REFERENCES scenario_lab.ref_mpl_source_mode(code),
    status           VARCHAR(20) NOT NULL CONSTRAINT df_run_status DEFAULT 'running'
        CONSTRAINT fk_run_status REFERENCES scenario_lab.ref_harvest_status(code),
    triggered_by     NVARCHAR(128) NOT NULL,
    started_at       DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_run_started DEFAULT SYSDATETIMEOFFSET(),
    finished_at      DATETIMEOFFSET(3) NULL,
    objects_fetched  INT NOT NULL CONSTRAINT df_run_obj DEFAULT 0,
    errors_fetched   INT NOT NULL CONSTRAINT df_run_err DEFAULT 0,
    new_errors_count INT NOT NULL CONSTRAINT df_run_new DEFAULT 0,
    notes            NVARCHAR(MAX) NULL,
    CONSTRAINT ck_run_counts CHECK (objects_fetched >= 0 AND errors_fetched >= 0)
);
GO

/* ---- 3) harvested objects ----------------------------------------- */
CREATE TABLE scenario_lab.scn_object (
    object_id           UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT df_obj_id DEFAULT NEWSEQUENTIALID()
        CONSTRAINT pk_scn_object PRIMARY KEY,
    source_system_id    UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_obj_src REFERENCES scenario_lab.scn_source_system(source_system_id),
    harvest_run_id      UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_obj_run REFERENCES scenario_lab.scn_harvest_run(harvest_run_id),
    system_kind         VARCHAR(10) NOT NULL
        CONSTRAINT fk_obj_kind REFERENCES scenario_lab.ref_system_kind(code),
    category            VARCHAR(20) NOT NULL
        CONSTRAINT fk_obj_cat REFERENCES scenario_lab.ref_object_category(code),
    object_name         NVARCHAR(200) NOT NULL,
    interface_id        NVARCHAR(120) NULL,
    integration_flow_id NVARCHAR(120) NULL,
    package_id          NVARCHAR(120) NULL,
    object_version      NVARCHAR(40)  NOT NULL CONSTRAINT df_obj_ver DEFAULT N'v1',
    technology          NVARCHAR(40)  NULL,
    priority            VARCHAR(2) NULL
        CONSTRAINT fk_obj_prio REFERENCES scenario_lab.ref_priority(code),
    is_maintained       BIT NOT NULL CONSTRAINT df_obj_maint DEFAULT 1,
    payload             NVARCHAR(MAX) NOT NULL CONSTRAINT df_obj_payload DEFAULT N'{}',
    content_hash        CHAR(64) NOT NULL,
    first_harvested_at  DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_obj_first DEFAULT SYSDATETIMEOFFSET(),
    last_harvested_at   DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_obj_last DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT ck_obj_payload_json CHECK (ISJSON(payload) = 1),
    CONSTRAINT uq_scn_object_identity UNIQUE (source_system_id, object_name, object_version)
);
GO

/* ---- 4) error events ---------------------------------------------- */
CREATE TABLE scenario_lab.scn_error_event (
    error_event_id      UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT df_err_id DEFAULT NEWSEQUENTIALID()
        CONSTRAINT pk_scn_error_event PRIMARY KEY,
    source_system_id    UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_err_src REFERENCES scenario_lab.scn_source_system(source_system_id),
    harvest_run_id      UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_err_run REFERENCES scenario_lab.scn_harvest_run(harvest_run_id),
    object_id           UNIQUEIDENTIFIER NULL
        CONSTRAINT fk_err_obj REFERENCES scenario_lab.scn_object(object_id),
    system_kind         VARCHAR(10) NOT NULL
        CONSTRAINT fk_err_kind REFERENCES scenario_lab.ref_system_kind(code),
    interface_id        NVARCHAR(120) NULL,
    interface_name      NVARCHAR(200) NOT NULL,
    integration_flow_id NVARCHAR(120) NULL,
    message_guid        NVARCHAR(80) NULL,
    correlation_id      NVARCHAR(80) NULL,
    adapter_protocol    NVARCHAR(40) NULL,
    http_status_code    INT NULL,
    exception_class     NVARCHAR(200) NULL,
    error_phrase        NVARCHAR(1000) NOT NULL,
    raw_log_excerpt     NVARCHAR(MAX) NULL,
    error_signature     CHAR(64) NOT NULL,
    priority            VARCHAR(2) NULL
        CONSTRAINT fk_err_prio REFERENCES scenario_lab.ref_priority(code),
    occurred_at         DATETIMEOFFSET(3) NOT NULL,
    ingested_at         DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_err_ingested DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT ck_err_http CHECK (http_status_code IS NULL OR (http_status_code BETWEEN 100 AND 599)),
    CONSTRAINT uq_err_occurrence UNIQUE (source_system_id, error_signature, occurred_at)
);
GO

/* ---- 5) atomic KB-RB candidate pairs ------------------------------ */
CREATE TABLE scenario_lab.scn_candidate_pair (
    candidate_pair_id   UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT df_pair_id DEFAULT NEWSEQUENTIALID()
        CONSTRAINT pk_scn_candidate_pair PRIMARY KEY,
    source_system_id    UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_pair_src REFERENCES scenario_lab.scn_source_system(source_system_id),
    error_signature     CHAR(64) NOT NULL,
    interface_name      NVARCHAR(200) NOT NULL,
    interface_id        NVARCHAR(120) NULL,
    seed_error_event_id UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_pair_seed REFERENCES scenario_lab.scn_error_event(error_event_id),
    priority            VARCHAR(2) NULL
        CONSTRAINT fk_pair_prio REFERENCES scenario_lab.ref_priority(code),
    status              VARCHAR(25) NOT NULL CONSTRAINT df_pair_status DEFAULT 'new'
        CONSTRAINT fk_pair_status REFERENCES scenario_lab.ref_pair_status(code),
    kb_document_id      NVARCHAR(200) NULL,
    rb_document_id      NVARCHAR(200) NULL,
    created_at          DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_pair_created DEFAULT SYSDATETIMEOFFSET(),
    updated_at          DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_pair_updated DEFAULT SYSDATETIMEOFFSET(),
    /* HARD RULE: one error signature -> exactly one atomic pair per system */
    CONSTRAINT uq_scn_atomic_pair UNIQUE (source_system_id, error_signature)
);
GO

INSERT INTO scenario_lab.schema_migration (version, description)
VALUES ('V002', N'Core staging tables');
GO

COMMIT TRANSACTION;
GO
PRINT 'V002 applied.';
GO

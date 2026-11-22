/* =====================================================================
   Scenario Lab — Migration V005 — Signals Evidence (F1)
   ---------------------------------------------------------------------
   Mirrors the JSON evidence index (scenario_lab/evidence/<SYSTEM_ID>.json):
   System > Package > iFlow > {negative | positive}. One row per case in
   scn_iflow_evidence; individual samples in scn_evidence_sample. Readiness
   (>=2 FAILED & >=1 SUCCESS => generation_ready) is derived in a view.
   Governed-safe: staging evidence only; never governed KB/RB content.
   ===================================================================== */

SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

IF EXISTS (SELECT 1 FROM scenario_lab.schema_migration WHERE version = 'V005')
BEGIN
    PRINT 'V005 already applied — skipping.';
    RETURN;
END
GO

BEGIN TRANSACTION;
GO

/* ---- 1) per-(system,package,iFlow,case) evidence aggregate --------- */
CREATE TABLE scenario_lab.scn_iflow_evidence (
    iflow_evidence_id    UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT df_evid_id DEFAULT NEWSEQUENTIALID()
        CONSTRAINT pk_scn_iflow_evidence PRIMARY KEY,
    system_id            NVARCHAR(60)  NOT NULL,
    package_id           NVARCHAR(200) NOT NULL CONSTRAINT df_evid_pkg DEFAULT 'UNKNOWN',
    integration_flow_id  NVARCHAR(200) NOT NULL,
    case_kind            VARCHAR(10)   NOT NULL
        CONSTRAINT ck_evid_case CHECK (case_kind IN ('negative','positive')),
    sample_count         INT NOT NULL CONSTRAINT df_evid_count DEFAULT 0,
    by_category          NVARCHAR(MAX) NULL
        CONSTRAINT ck_evid_bycat CHECK (by_category IS NULL OR ISJSON(by_category) = 1),
    first_seen           DATETIMEOFFSET(3) NULL,
    last_seen            DATETIMEOFFSET(3) NULL,
    updated_at           DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_evid_upd DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT uq_scn_iflow_evidence UNIQUE (system_id, package_id, integration_flow_id, case_kind)
);
GO

/* ---- 2) individual evidence samples (literal MPL, preserved) ------- */
CREATE TABLE scenario_lab.scn_evidence_sample (
    evidence_sample_id   UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT df_evsample_id DEFAULT NEWSEQUENTIALID()
        CONSTRAINT pk_scn_evidence_sample PRIMARY KEY,
    iflow_evidence_id    UNIQUEIDENTIFIER NOT NULL
        CONSTRAINT fk_evsample_evid REFERENCES scenario_lab.scn_iflow_evidence(iflow_evidence_id),
    occurred_at          DATETIMEOFFSET(3) NULL,
    message_guid         NVARCHAR(80)  NULL,
    sender               NVARCHAR(200) NULL,
    receiver             NVARCHAR(200) NULL,
    status               VARCHAR(20)   NULL,
    taxonomy             NVARCHAR(200) NULL,   -- negative only
    error_text           NVARCHAR(2000) NULL,  -- negative only, literal (BM25 Rule 7)
    CONSTRAINT uq_scn_evidence_sample UNIQUE (iflow_evidence_id, message_guid, occurred_at)
);
GO

CREATE INDEX ix_scn_evidence_sample_evid
    ON scenario_lab.scn_evidence_sample (iflow_evidence_id, occurred_at DESC);
GO

/* ---- 3) readiness view: >=2 FAILED & >=1 SUCCESS per iFlow --------- */
EXEC('CREATE VIEW scenario_lab.scn_iflow_readiness AS
    SELECT
        system_id, package_id, integration_flow_id,
        SUM(CASE WHEN case_kind = ''negative'' THEN sample_count ELSE 0 END) AS failed_count,
        SUM(CASE WHEN case_kind = ''positive'' THEN sample_count ELSE 0 END) AS success_count,
        CASE WHEN SUM(CASE WHEN case_kind = ''negative'' THEN sample_count ELSE 0 END) >= 2
              AND SUM(CASE WHEN case_kind = ''positive'' THEN sample_count ELSE 0 END) >= 1
             THEN CAST(1 AS BIT) ELSE CAST(0 AS BIT) END AS generation_ready
    FROM scenario_lab.scn_iflow_evidence
    GROUP BY system_id, package_id, integration_flow_id;');
GO

INSERT INTO scenario_lab.schema_migration (version, description)
VALUES ('V005', N'Signals Evidence — iFlow evidence + samples + readiness view');
GO

COMMIT TRANSACTION;
GO
PRINT 'V005 applied.';
GO

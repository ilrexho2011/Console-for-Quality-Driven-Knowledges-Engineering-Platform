/* =====================================================================
   QDKEP v14.0  |  Scenario Lab  |  MS SQL Server 2022 (SQL Server 16)
   Migration V001 — schema, migration history, reference tables
   ---------------------------------------------------------------------
   Target  : on-premise SQL Server, accessed via SAP Cloud Connector.
   Style   : Flyway-style numbered migration. Idempotent guards added so
             re-running a partially applied script is safe.
   Enums   : modelled as reference tables + FK (governed normalization),
             not CHECK constraints.
   ===================================================================== */

SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

/* ---- schema -------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'scenario_lab')
    EXEC(N'CREATE SCHEMA scenario_lab AUTHORIZATION dbo;');
GO

/* ---- migration history (so we never double-apply) ------------------ */
IF OBJECT_ID(N'scenario_lab.schema_migration', N'U') IS NULL
BEGIN
    CREATE TABLE scenario_lab.schema_migration (
        version       VARCHAR(10)   NOT NULL CONSTRAINT pk_schema_migration PRIMARY KEY,
        description   NVARCHAR(200) NOT NULL,
        applied_at    DATETIMEOFFSET(3) NOT NULL CONSTRAINT df_migration_applied DEFAULT SYSDATETIMEOFFSET(),
        applied_by    NVARCHAR(128) NOT NULL CONSTRAINT df_migration_by DEFAULT SUSER_SNAME()
    );
END
GO

IF EXISTS (SELECT 1 FROM scenario_lab.schema_migration WHERE version = 'V001')
BEGIN
    PRINT 'V001 already applied — skipping.';
    RETURN;
END
GO

BEGIN TRANSACTION;
GO

/* ---- reference tables (replace PostgreSQL ENUMs) ------------------- */
CREATE TABLE scenario_lab.ref_system_kind (
    code        VARCHAR(10)   NOT NULL CONSTRAINT pk_ref_system_kind PRIMARY KEY,
    description NVARCHAR(100) NOT NULL
);
INSERT INTO scenario_lab.ref_system_kind (code, description) VALUES
    ('CPI',  N'SAP Cloud Integration'),
    ('PIPO', N'SAP PI/PO'),
    ('S4',   N'S/4HANA'),
    ('APIM', N'API Management (CPI sub-source)'),
    ('AEM',  N'Advanced Event Mesh (CPI sub-source)');

CREATE TABLE scenario_lab.ref_harvest_mode (
    code VARCHAR(20) NOT NULL CONSTRAINT pk_ref_harvest_mode PRIMARY KEY,
    description NVARCHAR(100) NOT NULL
);
INSERT INTO scenario_lab.ref_harvest_mode (code, description) VALUES
    ('design',      N'Design-time objects'),
    ('config',      N'Configuration objects'),
    ('runtime_mpl', N'Runtime MPL / error evidence'),
    ('parameters',  N'Maintainable parameters'),
    ('full',        N'Full discovery');

CREATE TABLE scenario_lab.ref_harvest_status (
    code VARCHAR(20) NOT NULL CONSTRAINT pk_ref_harvest_status PRIMARY KEY,
    description NVARCHAR(100) NOT NULL
);
INSERT INTO scenario_lab.ref_harvest_status (code, description) VALUES
    ('running',   N'In progress'),
    ('succeeded', N'Completed'),
    ('partial',   N'Completed with gaps'),
    ('failed',    N'Failed');

CREATE TABLE scenario_lab.ref_object_category (
    code VARCHAR(20) NOT NULL CONSTRAINT pk_ref_object_category PRIMARY KEY,
    description NVARCHAR(100) NOT NULL
);
INSERT INTO scenario_lab.ref_object_category (code, description) VALUES
    ('design',N'Design'),('configuration',N'Configuration'),('parameter',N'Parameter'),
    ('adapter',N'Adapter'),('mapping',N'Mapping'),('channel',N'Channel'),
    ('policy',N'Policy'),('schedule',N'Schedule'),('other',N'Other');

CREATE TABLE scenario_lab.ref_priority (
    code VARCHAR(2) NOT NULL CONSTRAINT pk_ref_priority PRIMARY KEY,
    description NVARCHAR(100) NOT NULL
);
INSERT INTO scenario_lab.ref_priority (code, description) VALUES
    ('P1',N'Business critical'),('P2',N'Important'),
    ('P3',N'Standard'),('P4',N'Low');

CREATE TABLE scenario_lab.ref_pair_status (
    code VARCHAR(25) NOT NULL CONSTRAINT pk_ref_pair_status PRIMARY KEY,
    description NVARCHAR(100) NOT NULL
);
INSERT INTO scenario_lab.ref_pair_status (code, description) VALUES
    ('new',N'New candidate'),('queued_for_generation',N'Queued'),
    ('generated',N'Generated'),('approved',N'Approved'),
    ('rejected',N'Rejected'),('superseded',N'Superseded');

CREATE TABLE scenario_lab.ref_mpl_source_mode (
    code VARCHAR(10) NOT NULL CONSTRAINT pk_ref_mpl_source_mode PRIMARY KEY,
    description NVARCHAR(100) NOT NULL
);
INSERT INTO scenario_lab.ref_mpl_source_mode (code, description) VALUES
    ('live', N'Live connector extraction'),
    ('local',N'Local uploaded export');
GO

INSERT INTO scenario_lab.schema_migration (version, description)
VALUES ('V001', N'Schema, migration history, reference (enum) tables');
GO

COMMIT TRANSACTION;
GO
PRINT 'V001 applied.';
GO

/* Rollback U001 — remove reference tables, migration history, schema.
   WARNING: drops the entire scenario_lab schema. Run only for a full teardown. */
SET XACT_ABORT ON;
GO
IF OBJECT_ID(N'scenario_lab.ref_mpl_source_mode', N'U') IS NOT NULL DROP TABLE scenario_lab.ref_mpl_source_mode;
IF OBJECT_ID(N'scenario_lab.ref_pair_status',     N'U') IS NOT NULL DROP TABLE scenario_lab.ref_pair_status;
IF OBJECT_ID(N'scenario_lab.ref_priority',        N'U') IS NOT NULL DROP TABLE scenario_lab.ref_priority;
IF OBJECT_ID(N'scenario_lab.ref_object_category', N'U') IS NOT NULL DROP TABLE scenario_lab.ref_object_category;
IF OBJECT_ID(N'scenario_lab.ref_harvest_status',  N'U') IS NOT NULL DROP TABLE scenario_lab.ref_harvest_status;
IF OBJECT_ID(N'scenario_lab.ref_harvest_mode',    N'U') IS NOT NULL DROP TABLE scenario_lab.ref_harvest_mode;
IF OBJECT_ID(N'scenario_lab.ref_system_kind',     N'U') IS NOT NULL DROP TABLE scenario_lab.ref_system_kind;
GO
/* Remove migration history last */
IF OBJECT_ID(N'scenario_lab.schema_migration', N'U') IS NOT NULL DROP TABLE scenario_lab.schema_migration;
GO
IF EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'scenario_lab')
    EXEC(N'DROP SCHEMA scenario_lab;');
GO
PRINT 'U001 rolled back (schema removed).';
GO

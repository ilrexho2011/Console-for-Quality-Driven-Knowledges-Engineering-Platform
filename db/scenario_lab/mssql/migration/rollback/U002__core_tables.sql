/* Rollback U002 — remove core tables (child-first to respect FKs) */
SET XACT_ABORT ON;
GO
IF OBJECT_ID(N'scenario_lab.scn_candidate_pair', N'U') IS NOT NULL DROP TABLE scenario_lab.scn_candidate_pair;
IF OBJECT_ID(N'scenario_lab.scn_error_event',    N'U') IS NOT NULL DROP TABLE scenario_lab.scn_error_event;
IF OBJECT_ID(N'scenario_lab.scn_object',         N'U') IS NOT NULL DROP TABLE scenario_lab.scn_object;
IF OBJECT_ID(N'scenario_lab.scn_harvest_run',    N'U') IS NOT NULL DROP TABLE scenario_lab.scn_harvest_run;
IF OBJECT_ID(N'scenario_lab.scn_source_system',  N'U') IS NOT NULL DROP TABLE scenario_lab.scn_source_system;
GO
DELETE FROM scenario_lab.schema_migration WHERE version = 'V002';
GO
PRINT 'U002 rolled back.';
GO

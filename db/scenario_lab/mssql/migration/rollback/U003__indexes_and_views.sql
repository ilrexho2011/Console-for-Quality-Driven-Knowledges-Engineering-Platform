/* Rollback U003 — remove views and indexes */
SET XACT_ABORT ON;
GO
IF OBJECT_ID(N'scenario_lab.scn_first_seen_errors', N'V') IS NOT NULL DROP VIEW scenario_lab.scn_first_seen_errors;
IF OBJECT_ID(N'scenario_lab.scn_aem_objects',  N'V') IS NOT NULL DROP VIEW scenario_lab.scn_aem_objects;
IF OBJECT_ID(N'scenario_lab.scn_apim_objects', N'V') IS NOT NULL DROP VIEW scenario_lab.scn_apim_objects;
IF OBJECT_ID(N'scenario_lab.scn_s4_objects',   N'V') IS NOT NULL DROP VIEW scenario_lab.scn_s4_objects;
IF OBJECT_ID(N'scenario_lab.scn_pipo_objects', N'V') IS NOT NULL DROP VIEW scenario_lab.scn_pipo_objects;
IF OBJECT_ID(N'scenario_lab.scn_cpi_objects',  N'V') IS NOT NULL DROP VIEW scenario_lab.scn_cpi_objects;
GO
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_pair_status'        AND object_id = OBJECT_ID('scenario_lab.scn_candidate_pair')) DROP INDEX ix_pair_status ON scenario_lab.scn_candidate_pair;
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_error_interface'    AND object_id = OBJECT_ID('scenario_lab.scn_error_event'))    DROP INDEX ix_error_interface ON scenario_lab.scn_error_event;
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_error_signature'    AND object_id = OBJECT_ID('scenario_lab.scn_error_event'))    DROP INDEX ix_error_signature ON scenario_lab.scn_error_event;
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_error_system'       AND object_id = OBJECT_ID('scenario_lab.scn_error_event'))    DROP INDEX ix_error_system ON scenario_lab.scn_error_event;
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_object_priority'    AND object_id = OBJECT_ID('scenario_lab.scn_object'))         DROP INDEX ix_object_priority ON scenario_lab.scn_object;
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_object_flow'        AND object_id = OBJECT_ID('scenario_lab.scn_object'))         DROP INDEX ix_object_flow ON scenario_lab.scn_object;
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_object_interface'   AND object_id = OBJECT_ID('scenario_lab.scn_object'))         DROP INDEX ix_object_interface ON scenario_lab.scn_object;
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_object_system_kind' AND object_id = OBJECT_ID('scenario_lab.scn_object'))         DROP INDEX ix_object_system_kind ON scenario_lab.scn_object;
GO
DELETE FROM scenario_lab.schema_migration WHERE version = 'V003';
GO
PRINT 'U003 rolled back.';
GO

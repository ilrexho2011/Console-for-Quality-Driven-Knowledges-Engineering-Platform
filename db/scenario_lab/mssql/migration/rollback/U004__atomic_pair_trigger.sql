/* Rollback U004 — remove atomic-pair seeding trigger */
SET XACT_ABORT ON;
GO
IF OBJECT_ID(N'scenario_lab.trg_seed_atomic_pair', N'TR') IS NOT NULL
    DROP TRIGGER scenario_lab.trg_seed_atomic_pair;
GO
DELETE FROM scenario_lab.schema_migration WHERE version = 'V004';
GO
PRINT 'U004 rolled back.';
GO

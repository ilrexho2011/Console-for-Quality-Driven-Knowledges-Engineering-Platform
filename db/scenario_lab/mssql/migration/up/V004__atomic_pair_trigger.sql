/* =====================================================================
   Scenario Lab — Migration V004 — atomic-pair seeding trigger
   ---------------------------------------------------------------------
   T-SQL trigger is SET-BASED (fires once per statement, not per row).
   It promotes each newly first-seen error_signature into exactly one
   scn_candidate_pair. The UNIQUE(source_system_id, error_signature)
   constraint plus NOT EXISTS guard preserve the one-error-one-pair rule.
   ===================================================================== */

SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

IF EXISTS (SELECT 1 FROM scenario_lab.schema_migration WHERE version = 'V004')
BEGIN
    PRINT 'V004 already applied — skipping.';
    RETURN;
END
GO

CREATE OR ALTER TRIGGER scenario_lab.trg_seed_atomic_pair
ON scenario_lab.scn_error_event
AFTER INSERT
AS
BEGIN
    SET NOCOUNT ON;

    /* Pick one seed row per (system, signature) from the inserted set,
       then insert only signatures that do not yet have a candidate pair. */
    ;WITH seed AS (
        SELECT i.source_system_id, i.error_signature, i.interface_name,
               i.interface_id, i.error_event_id, i.priority,
               ROW_NUMBER() OVER (
                   PARTITION BY i.source_system_id, i.error_signature
                   ORDER BY i.occurred_at ASC
               ) AS rn
        FROM inserted i
    )
    INSERT INTO scenario_lab.scn_candidate_pair
        (source_system_id, error_signature, interface_name, interface_id,
         seed_error_event_id, priority, status)
    SELECT seed.source_system_id, seed.error_signature, seed.interface_name,
           seed.interface_id, seed.error_event_id, seed.priority, 'new'
    FROM seed
    WHERE seed.rn = 1
      AND NOT EXISTS (
          SELECT 1 FROM scenario_lab.scn_candidate_pair p
          WHERE p.source_system_id = seed.source_system_id
            AND p.error_signature  = seed.error_signature
      );
END
GO

INSERT INTO scenario_lab.schema_migration (version, description)
VALUES ('V004', N'Atomic-pair seeding trigger');
GO
PRINT 'V004 applied.';
GO

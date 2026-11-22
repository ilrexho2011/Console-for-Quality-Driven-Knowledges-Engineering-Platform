/* =====================================================================
   Scenario Lab — Migration V003 — indexes and views
   ---------------------------------------------------------------------
   Per-system "tables the user sees" are VIEWs over scn_object, exposing
   JSON keys via JSON_VALUE(). first-seen panel is a windowed VIEW.
   ===================================================================== */

SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

IF EXISTS (SELECT 1 FROM scenario_lab.schema_migration WHERE version = 'V003')
BEGIN
    PRINT 'V003 already applied — skipping.';
    RETURN;
END
GO

/* ---- indexes (DDL not allowed to share a tx batch with views via GO) */
CREATE INDEX ix_object_system_kind ON scenario_lab.scn_object (system_kind, category);
CREATE INDEX ix_object_interface   ON scenario_lab.scn_object (interface_id);
CREATE INDEX ix_object_flow        ON scenario_lab.scn_object (integration_flow_id);
CREATE INDEX ix_object_priority    ON scenario_lab.scn_object (priority);
GO
CREATE INDEX ix_error_system    ON scenario_lab.scn_error_event (source_system_id, occurred_at DESC);
CREATE INDEX ix_error_signature ON scenario_lab.scn_error_event (error_signature);
CREATE INDEX ix_error_interface ON scenario_lab.scn_error_event (interface_name);
GO
CREATE INDEX ix_pair_status ON scenario_lab.scn_candidate_pair (status, priority);
GO

/* ---- per-system VIEWs (each CREATE VIEW must be first in its batch) - */
CREATE VIEW scenario_lab.scn_cpi_objects AS
SELECT o.object_id, s.system_id, o.object_name, o.integration_flow_id, o.package_id,
       o.category, o.priority,
       JSON_VALUE(o.payload, '$.adapter')              AS adapter,
       JSON_VALUE(o.payload, '$.sender_system')        AS sender_system,
       JSON_VALUE(o.payload, '$.receiver_system')      AS receiver_system,
       JSON_VALUE(o.payload, '$.channel')              AS channel,
       JSON_VALUE(o.payload, '$.exception_subprocess') AS exception_subprocess,
       o.last_harvested_at
FROM scenario_lab.scn_object o
JOIN scenario_lab.scn_source_system s ON s.source_system_id = o.source_system_id
WHERE o.system_kind = 'CPI';
GO

CREATE VIEW scenario_lab.scn_pipo_objects AS
SELECT o.object_id, s.system_id, o.object_name, o.interface_id, o.category, o.priority,
       JSON_VALUE(o.payload, '$.artifact_layer')        AS artifact_layer,
       JSON_VALUE(o.payload, '$.namespace')             AS namespace,
       JSON_VALUE(o.payload, '$.software_component')     AS software_component,
       JSON_VALUE(o.payload, '$.communication_channel')  AS communication_channel,
       o.last_harvested_at
FROM scenario_lab.scn_object o
JOIN scenario_lab.scn_source_system s ON s.source_system_id = o.source_system_id
WHERE o.system_kind = 'PIPO';
GO

CREATE VIEW scenario_lab.scn_s4_objects AS
SELECT o.object_id, s.system_id, s.sap_sid, o.object_name, o.interface_id,
       o.category, o.priority,
       JSON_VALUE(o.payload, '$.api_type')   AS api_type,
       JSON_VALUE(o.payload, '$.cds_service') AS cds_service,
       JSON_VALUE(o.payload, '$.idoc_type')  AS idoc_type,
       o.last_harvested_at
FROM scenario_lab.scn_object o
JOIN scenario_lab.scn_source_system s ON s.source_system_id = o.source_system_id
WHERE o.system_kind = 'S4';
GO

CREATE VIEW scenario_lab.scn_apim_objects AS
SELECT o.object_id, s.system_id, o.object_name, o.package_id AS api_proxy,
       o.category, o.priority,
       JSON_VALUE(o.payload, '$.rate_limit')            AS rate_limit,
       JSON_VALUE(o.payload, '$.policy')                AS policy,
       JSON_VALUE(o.payload, '$.target_endpoint_alias')  AS target_endpoint_alias,
       o.last_harvested_at
FROM scenario_lab.scn_object o
JOIN scenario_lab.scn_source_system s ON s.source_system_id = o.source_system_id
WHERE o.system_kind = 'APIM';
GO

CREATE VIEW scenario_lab.scn_aem_objects AS
SELECT o.object_id, s.system_id, o.object_name, o.category, o.priority,
       JSON_VALUE(o.payload, '$.event_topic')   AS event_topic,
       JSON_VALUE(o.payload, '$.subscription')   AS subscription,
       JSON_VALUE(o.payload, '$.queue')          AS queue,
       o.last_harvested_at
FROM scenario_lab.scn_object o
JOIN scenario_lab.scn_source_system s ON s.source_system_id = o.source_system_id
WHERE o.system_kind = 'AEM';
GO

/* ---- first-seen errors VIEW (bottom panel) ------------------------- */
/* SQL Server has no DISTINCT ON; use ROW_NUMBER() over earliest occurrence */
CREATE VIEW scenario_lab.scn_first_seen_errors AS
SELECT system_id, system_kind, interface_name, interface_id, exception_class,
       error_phrase, http_status_code, priority,
       first_seen_at, error_signature, error_event_id
FROM (
    SELECT s.system_id, e.system_kind, e.interface_name, e.interface_id,
           e.exception_class, e.error_phrase, e.http_status_code, e.priority,
           e.occurred_at AS first_seen_at, e.error_signature, e.error_event_id,
           ROW_NUMBER() OVER (
               PARTITION BY e.source_system_id, e.error_signature
               ORDER BY e.occurred_at ASC
           ) AS rn
    FROM scenario_lab.scn_error_event e
    JOIN scenario_lab.scn_source_system s ON s.source_system_id = e.source_system_id
) ranked
WHERE rn = 1;
GO

INSERT INTO scenario_lab.schema_migration (version, description)
VALUES ('V003', N'Indexes and views');
GO
PRINT 'V003 applied.';
GO

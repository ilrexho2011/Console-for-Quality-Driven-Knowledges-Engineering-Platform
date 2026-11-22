/* =====================================================================
   QDKEP v15.0 | Scenario Lab | Seed source systems (MS SQL Server)
   ---------------------------------------------------------------------
   Registers the connected systems shown in the Control Room. APIM and AEM
   are CPI sub-sources and point to the CPI parent via parent_system_id.
   Idempotent: re-running does not duplicate rows.
   Adjust system_id / sap_sid / endpoints to your landscape before use.
   ===================================================================== */
SET NOCOUNT ON;
GO

DECLARE @cpi UNIQUEIDENTIFIER;

/* CPI (parent) */
IF NOT EXISTS (SELECT 1 FROM scenario_lab.scn_source_system WHERE customer_id='RCMO' AND system_id='CPI-RCMO')
    INSERT INTO scenario_lab.scn_source_system
        (system_id, customer_id, sap_sid, system_kind, display_name, monitoring_endpoint, is_connected)
    VALUES ('CPI-RCMO','RCMO','RO2','CPI', N'SAP CPI (RCMO)', N'CPI MPL API', 1);

SELECT @cpi = source_system_id FROM scenario_lab.scn_source_system
WHERE customer_id='RCMO' AND system_id='CPI-RCMO';

/* PI/PO */
IF NOT EXISTS (SELECT 1 FROM scenario_lab.scn_source_system WHERE customer_id='RCMO' AND system_id='PIPO-RCMO')
    INSERT INTO scenario_lab.scn_source_system
        (system_id, customer_id, sap_sid, system_kind, display_name, monitoring_endpoint, is_connected)
    VALUES ('PIPO-RCMO','RCMO','RO2','PIPO', N'SAP PI/PO (RCMO)', N'PI channel monitoring', 1);

/* S/4HANA */
IF NOT EXISTS (SELECT 1 FROM scenario_lab.scn_source_system WHERE customer_id='RCMO' AND system_id='S4-RCMO')
    INSERT INTO scenario_lab.scn_source_system
        (system_id, customer_id, sap_sid, system_kind, display_name, monitoring_endpoint, is_connected)
    VALUES ('S4-RCMO','RCMO','RO2','S4', N'S/4HANA (RCMO)', N'S4 IDoc monitoring', 1);

/* APIM (CPI sub-source) */
IF NOT EXISTS (SELECT 1 FROM scenario_lab.scn_source_system WHERE customer_id='RCMO' AND system_id='APIM-RCMO')
    INSERT INTO scenario_lab.scn_source_system
        (system_id, customer_id, system_kind, parent_system_id, display_name, monitoring_endpoint, is_connected)
    VALUES ('APIM-RCMO','RCMO','APIM', @cpi, N'SAP APIM (RCMO)', N'APIM analytics', 1);

/* AEM (CPI sub-source) */
IF NOT EXISTS (SELECT 1 FROM scenario_lab.scn_source_system WHERE customer_id='RCMO' AND system_id='AEM-RCMO')
    INSERT INTO scenario_lab.scn_source_system
        (system_id, customer_id, system_kind, parent_system_id, display_name, monitoring_endpoint, is_connected)
    VALUES ('AEM-RCMO','RCMO','AEM', @cpi, N'SAP AEM (RCMO)', N'AEM event monitoring', 0);
GO

SELECT system_id, system_kind, is_connected FROM scenario_lab.scn_source_system ORDER BY system_kind, system_id;
GO
PRINT 'Seed complete.';
GO

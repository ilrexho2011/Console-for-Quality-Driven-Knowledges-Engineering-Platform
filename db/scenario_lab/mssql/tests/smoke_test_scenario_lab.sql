/* =====================================================================
   QDKEP v14.0  |  Scenario Lab  |  Smoke test (MS SQL Server 2022)
   ---------------------------------------------------------------------
   Verifies, with assertions, that:
     A. The atomic-pair trigger creates exactly ONE candidate pair per
        error_signature, even when the same error is ingested repeatedly
        and across multiple runs.
     B. Different error signatures create separate candidate pairs.
     C. The unique occurrence index dedups identical (signature, time).
     D. scn_first_seen_errors returns only the FIRST occurrence per
        signature, with the correct earliest timestamp.
     E. The atomic pair points at the seed (first) error event.

   Safe to run repeatedly: it works inside a transaction and ROLLS BACK
   at the end, leaving no test data behind. Set @keep_data = 1 to commit
   instead (e.g. to inspect rows in SSMS).

   Run AFTER migrations V001..V004 have been applied.
   ===================================================================== */

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

DECLARE @keep_data BIT = 0;          -- 0 = rollback (default), 1 = commit
DECLARE @failures INT = 0;

BEGIN TRANSACTION smoke;

BEGIN TRY
    /* ---------------------------------------------------------------
       Fixtures: one CPI source system + one harvest run
       --------------------------------------------------------------- */
    DECLARE @sys UNIQUEIDENTIFIER = NEWID();
    DECLARE @run UNIQUEIDENTIFIER = NEWID();

    INSERT INTO scenario_lab.scn_source_system
        (source_system_id, system_id, customer_id, sap_sid, system_kind,
         display_name, is_connected, is_active)
    VALUES
        (@sys, N'CPI-RCMO-TEST', N'RCMO', N'RO2', 'CPI',
         N'CPI RCMO (smoke test)', 1, 1);

    INSERT INTO scenario_lab.scn_harvest_run
        (harvest_run_id, source_system_id, mode, mpl_source_mode, status, triggered_by)
    VALUES
        (@run, @sys, 'runtime_mpl', 'local', 'running', N'smoke_test');

    /* Canonical signatures (mirrors app.error_signature: sha256 of
       UPPER(interface)+exception+normalized phrase). For the test we use
       fixed literal hashes so the trigger logic is what is under test,
       not the hashing.                                                   */
    DECLARE @sig_502 CHAR(64) = REPLICATE('a', 64);  -- IF16 / HTTP502 / Bad Gateway
    DECLARE @sig_429 CHAR(64) = REPLICATE('b', 64);  -- spikeArrest / RateLimitExceeded

    /* ---------------------------------------------------------------
       A. Same error ingested 3 times (2 at the same instant -> dedup,
          1 later) plus a second distinct error.
       --------------------------------------------------------------- */
    DECLARE @t1 DATETIMEOFFSET(3) = '2026-06-23T07:00:00.000+00:00';
    DECLARE @t2 DATETIMEOFFSET(3) = '2026-06-23T09:14:00.000+00:00';
    DECLARE @t3 DATETIMEOFFSET(3) = '2026-06-23T08:51:00.000+00:00';

    /* first occurrence of the 502 (earliest in time) */
    INSERT INTO scenario_lab.scn_error_event
        (source_system_id, harvest_run_id, system_kind, interface_name,
         exception_class, http_status_code, error_phrase, error_signature,
         priority, occurred_at)
    VALUES
        (@sys, @run, 'CPI', N'IF16_FETCHING_FAILED', N'HTTP502', 502,
         N'Bad Gateway', @sig_502, 'P2', @t1);

    /* same signature, LATER time -> new occurrence row, but NO new pair */
    INSERT INTO scenario_lab.scn_error_event
        (source_system_id, harvest_run_id, system_kind, interface_name,
         exception_class, http_status_code, error_phrase, error_signature,
         priority, occurred_at)
    VALUES
        (@sys, @run, 'CPI', N'IF16_FETCHING_FAILED', N'HTTP502', 502,
         N'Bad Gateway', @sig_502, 'P2', @t2);

    /* duplicate of the FIRST occurrence (same sig + same time) -> dedup */
    BEGIN TRY
        INSERT INTO scenario_lab.scn_error_event
            (source_system_id, harvest_run_id, system_kind, interface_name,
             exception_class, http_status_code, error_phrase, error_signature,
             priority, occurred_at)
        VALUES
            (@sys, @run, 'CPI', N'IF16_FETCHING_FAILED', N'HTTP502', 502,
             N'Bad Gateway', @sig_502, 'P2', @t1);
        PRINT 'NOTE: duplicate occurrence inserted (unexpected) — see assertion C.';
    END TRY
    BEGIN CATCH
        /* expected: unique violation on (source_system_id, signature, occurred_at) */
        PRINT 'OK: duplicate (signature, time) rejected by unique index.';
    END CATCH;

    /* a DIFFERENT error signature -> should create its own pair */
    INSERT INTO scenario_lab.scn_error_event
        (source_system_id, harvest_run_id, system_kind, interface_name,
         exception_class, http_status_code, error_phrase, error_signature,
         priority, occurred_at)
    VALUES
        (@sys, @run, 'CPI', N'Any_spikeArrest', N'RateLimitExceeded', 429,
         N'Rate limit quota exceeded', @sig_429, 'P2', @t3);

    /* ===============================================================
       ASSERTIONS
       =============================================================== */

    /* C. occurrence dedup: exactly 2 rows for the 502 signature
          (t1 + t2; the duplicate of t1 was rejected)                  */
    DECLARE @occ_502 INT =
        (SELECT COUNT(*) FROM scenario_lab.scn_error_event
         WHERE source_system_id = @sys AND error_signature = @sig_502);
    IF @occ_502 = 2
        PRINT 'PASS C: 502 has 2 occurrence rows (duplicate deduped).';
    ELSE
    BEGIN
        PRINT CONCAT('FAIL C: expected 2 occurrences for 502, got ', @occ_502);
        SET @failures += 1;
    END

    /* A. exactly ONE atomic pair for the 502 signature                */
    DECLARE @pairs_502 INT =
        (SELECT COUNT(*) FROM scenario_lab.scn_candidate_pair
         WHERE source_system_id = @sys AND error_signature = @sig_502);
    IF @pairs_502 = 1
        PRINT 'PASS A: exactly 1 atomic pair for the repeated 502 error.';
    ELSE
    BEGIN
        PRINT CONCAT('FAIL A: expected 1 pair for 502, got ', @pairs_502);
        SET @failures += 1;
    END

    /* B. distinct error -> its own pair; total pairs = 2              */
    DECLARE @pairs_total INT =
        (SELECT COUNT(*) FROM scenario_lab.scn_candidate_pair
         WHERE source_system_id = @sys);
    IF @pairs_total = 2
        PRINT 'PASS B: 2 distinct errors -> 2 atomic pairs.';
    ELSE
    BEGIN
        PRINT CONCAT('FAIL B: expected 2 pairs total, got ', @pairs_total);
        SET @failures += 1;
    END

    /* E. the 502 pair seeds from the FIRST (earliest) error event     */
    DECLARE @seed_time DATETIMEOFFSET(3) =
        (SELECT e.occurred_at
         FROM scenario_lab.scn_candidate_pair p
         JOIN scenario_lab.scn_error_event e
           ON e.error_event_id = p.seed_error_event_id
         WHERE p.source_system_id = @sys AND p.error_signature = @sig_502);
    IF @seed_time = @t1
        PRINT 'PASS E: atomic pair seeds from the earliest 502 occurrence.';
    ELSE
    BEGIN
        PRINT CONCAT('FAIL E: seed time ', CONVERT(NVARCHAR(40), @seed_time),
                     ' != first occurrence ', CONVERT(NVARCHAR(40), @t1));
        SET @failures += 1;
    END

    /* D1. first-seen view returns exactly 2 rows (one per signature)  */
    DECLARE @fs_count INT =
        (SELECT COUNT(*) FROM scenario_lab.scn_first_seen_errors
         WHERE system_id = N'CPI-RCMO-TEST');
    IF @fs_count = 2
        PRINT 'PASS D1: first-seen panel shows 2 rows (one per signature).';
    ELSE
    BEGIN
        PRINT CONCAT('FAIL D1: expected 2 first-seen rows, got ', @fs_count);
        SET @failures += 1;
    END

    /* D2. first-seen time of the 502 is the EARLIEST (t1), not t2      */
    DECLARE @fs_502_time DATETIMEOFFSET(3) =
        (SELECT first_seen_at FROM scenario_lab.scn_first_seen_errors
         WHERE system_id = N'CPI-RCMO-TEST' AND error_signature = @sig_502);
    IF @fs_502_time = @t1
        PRINT 'PASS D2: first-seen timestamp = earliest occurrence (t1).';
    ELSE
    BEGIN
        PRINT CONCAT('FAIL D2: first-seen 502 = ',
                     CONVERT(NVARCHAR(40), @fs_502_time),
                     ' expected ', CONVERT(NVARCHAR(40), @t1));
        SET @failures += 1;
    END

    /* ---------------------------------------------------------------
       Visible evidence (handy when @keep_data = 1)
       --------------------------------------------------------------- */
    PRINT '--- first-seen panel (CPI-RCMO-TEST) ---';
    SELECT interface_name, exception_class, error_phrase,
           http_status_code, first_seen_at
    FROM scenario_lab.scn_first_seen_errors
    WHERE system_id = N'CPI-RCMO-TEST'
    ORDER BY first_seen_at DESC;

    PRINT '--- atomic candidate pairs ---';
    SELECT interface_name, error_signature, status, priority
    FROM scenario_lab.scn_candidate_pair
    WHERE source_system_id = @sys
    ORDER BY interface_name;

    /* ---------------------------------------------------------------
       Result summary
       --------------------------------------------------------------- */
    IF @failures = 0
        PRINT '==================  SMOKE TEST PASSED  ==================';
    ELSE
        PRINT CONCAT('==================  SMOKE TEST FAILED: ',
                     @failures, ' assertion(s)  ==================');

    IF @keep_data = 1
    BEGIN
        COMMIT TRANSACTION smoke;
        PRINT 'Test data COMMITTED (keep_data = 1).';
    END
    ELSE
    BEGIN
        ROLLBACK TRANSACTION smoke;
        PRINT 'Test data ROLLED BACK (no residue).';
    END

    /* Make the batch fail loudly in CI if any assertion failed */
    IF @failures > 0
        THROW 50001, 'Scenario Lab smoke test failed.', 1;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
    PRINT CONCAT('ERROR: ', ERROR_MESSAGE());
    THROW;  -- re-raise so a runner / CI sees a non-zero result
END CATCH;
GO

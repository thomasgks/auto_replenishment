-- =============================================================================
-- migrate_db_fix.sql
-- Fixes the parts that failed due to safe update mode
-- Run via:  bench --site moosa.test mariadb < apps/auto_replenishment/migrate_db_fix.sql
-- =============================================================================

-- Disable safe update mode for this session only
SET SQL_SAFE_UPDATES = 0;
SET FOREIGN_KEY_CHECKS = 0;

-- =============================================================================
-- Fix tabDocField options (Table field references)
-- Uses name column (PK) to satisfy safe update mode
-- =============================================================================
SELECT '-- Fixing tabDocField Table options --' AS '';

UPDATE `tabDocField`
SET options = 'Replenishment Run Store'
WHERE options = 'Auto Replenishment Log Store'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField`
        WHERE options = 'Auto Replenishment Log Store'
    ) t
  );

UPDATE `tabDocField`
SET options = 'Replenishment Store Plan Item'
WHERE options = 'Auto Replenishment Forecast Item'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField`
        WHERE options = 'Auto Replenishment Forecast Item'
    ) t
  );

UPDATE `tabDocField`
SET options = 'Replenishment Allocation'
WHERE options = 'AR Forecast Allocation'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField`
        WHERE options = 'AR Forecast Allocation'
    ) t
  );

UPDATE `tabDocField`
SET options = 'Replenishment MR Link'
WHERE options = 'AR Forecast Material Request'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField`
        WHERE options = 'AR Forecast Material Request'
    ) t
  );

-- =============================================================================
-- Fix tabDocField parent references (in case any remain)
-- =============================================================================
SELECT '-- Fixing tabDocField parent references --' AS '';

UPDATE `tabDocField`
SET parent = 'Replenishment Run'
WHERE parent = 'Auto Replenishment Log'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField` WHERE parent = 'Auto Replenishment Log'
    ) t
  );

UPDATE `tabDocField`
SET parent = 'Replenishment Run Store'
WHERE parent = 'Auto Replenishment Log Store'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField` WHERE parent = 'Auto Replenishment Log Store'
    ) t
  );

UPDATE `tabDocField`
SET parent = 'Replenishment Store Plan'
WHERE parent = 'Auto Replenishment Forecast'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField` WHERE parent = 'Auto Replenishment Forecast'
    ) t
  );

UPDATE `tabDocField`
SET parent = 'Replenishment Store Plan Item'
WHERE parent = 'Auto Replenishment Forecast Item'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField` WHERE parent = 'Auto Replenishment Forecast Item'
    ) t
  );

UPDATE `tabDocField`
SET parent = 'Replenishment Allocation'
WHERE parent = 'AR Forecast Allocation'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField` WHERE parent = 'AR Forecast Allocation'
    ) t
  );

UPDATE `tabDocField`
SET parent = 'Replenishment MR Link'
WHERE parent = 'AR Forecast Material Request'
  AND name IN (
    SELECT name FROM (
        SELECT name FROM `tabDocField` WHERE parent = 'AR Forecast Material Request'
    ) t
  );

-- =============================================================================
-- Fix tabDocPerm
-- =============================================================================
SELECT '-- Fixing tabDocPerm --' AS '';

UPDATE `tabDocPerm`
SET parent = 'Replenishment Run'
WHERE parent = 'Auto Replenishment Log'
  AND name IN (SELECT name FROM (SELECT name FROM `tabDocPerm` WHERE parent = 'Auto Replenishment Log') t);

UPDATE `tabDocPerm`
SET parent = 'Replenishment Store Plan'
WHERE parent = 'Auto Replenishment Forecast'
  AND name IN (SELECT name FROM (SELECT name FROM `tabDocPerm` WHERE parent = 'Auto Replenishment Forecast') t);

UPDATE `tabDocPerm`
SET parent = 'Replenishment Allocation'
WHERE parent = 'AR Forecast Allocation'
  AND name IN (SELECT name FROM (SELECT name FROM `tabDocPerm` WHERE parent = 'AR Forecast Allocation') t);

UPDATE `tabDocPerm`
SET parent = 'Replenishment MR Link'
WHERE parent = 'AR Forecast Material Request'
  AND name IN (SELECT name FROM (SELECT name FROM `tabDocPerm` WHERE parent = 'AR Forecast Material Request') t);

-- =============================================================================
-- Fix parenttype in child tables
-- =============================================================================
SELECT '-- Fixing parenttype in child tables --' AS '';

UPDATE `tabReplenishment Run Store`
SET parenttype = 'Replenishment Run'
WHERE parenttype = 'Auto Replenishment Log'
  AND name IN (SELECT name FROM (SELECT name FROM `tabReplenishment Run Store` WHERE parenttype = 'Auto Replenishment Log') t);

UPDATE `tabReplenishment Store Plan Item`
SET parenttype = 'Replenishment Store Plan'
WHERE parenttype = 'Auto Replenishment Forecast'
  AND name IN (SELECT name FROM (SELECT name FROM `tabReplenishment Store Plan Item` WHERE parenttype = 'Auto Replenishment Forecast') t);

UPDATE `tabReplenishment Allocation`
SET parenttype = 'Replenishment Store Plan'
WHERE parenttype = 'Auto Replenishment Forecast'
  AND name IN (SELECT name FROM (SELECT name FROM `tabReplenishment Allocation` WHERE parenttype = 'Auto Replenishment Forecast') t);

UPDATE `tabReplenishment MR Link`
SET parenttype = 'Replenishment Store Plan'
WHERE parenttype = 'Auto Replenishment Forecast'
  AND name IN (SELECT name FROM (SELECT name FROM `tabReplenishment MR Link` WHERE parenttype = 'Auto Replenishment Forecast') t);

-- =============================================================================
-- Fix Custom Fields
-- =============================================================================
SELECT '-- Fixing tabCustom Field --' AS '';

UPDATE `tabCustom Field`
SET dt = 'Replenishment Store Plan'
WHERE dt = 'Auto Replenishment Forecast'
  AND name IN (SELECT name FROM (SELECT name FROM `tabCustom Field` WHERE dt = 'Auto Replenishment Forecast') t);

UPDATE `tabCustom Field`
SET dt = 'Replenishment Run'
WHERE dt = 'Auto Replenishment Log'
  AND name IN (SELECT name FROM (SELECT name FROM `tabCustom Field` WHERE dt = 'Auto Replenishment Log') t);

UPDATE `tabCustom Field`
SET options = 'Replenishment Run'
WHERE options = 'Auto Replenishment Log'
  AND name IN (SELECT name FROM (SELECT name FROM `tabCustom Field` WHERE options = 'Auto Replenishment Log') t);

UPDATE `tabCustom Field`
SET options = 'Replenishment Store Plan'
WHERE options = 'Auto Replenishment Forecast'
  AND name IN (SELECT name FROM (SELECT name FROM `tabCustom Field` WHERE options = 'Auto Replenishment Forecast') t);

-- =============================================================================
-- Fix has_permission path in tabDocType
-- =============================================================================
SELECT '-- Fixing has_permission in tabDocType --' AS '';

UPDATE `tabDocType`
SET has_permission = REPLACE(
    has_permission,
    'auto_replenishment_forecast.auto_replenishment_forecast.has_permission',
    'replenishment_store_plan.replenishment_store_plan.has_permission'
)
WHERE name = 'Replenishment Store Plan'
  AND has_permission LIKE '%auto_replenishment_forecast%';

-- Re-enable
SET SQL_SAFE_UPDATES = 1;
SET FOREIGN_KEY_CHECKS = 1;

SELECT '-- Fix migration complete --' AS '';
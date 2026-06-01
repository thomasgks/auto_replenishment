SET SQL_SAFE_UPDATES = 0;

-- Update label shown on Material Request form
UPDATE `tabCustom Field`
SET
    label   = 'Replenishment Store Plan',
    options = 'Replenishment Store Plan'
WHERE
    fieldname = 'custom_auto_replenishment_forecast'
    AND dt = 'Material Request';

-- Verify
SELECT name, dt, fieldname, label, options
FROM `tabCustom Field`
WHERE fieldname = 'custom_auto_replenishment_forecast';

SET SQL_SAFE_UPDATES = 1;

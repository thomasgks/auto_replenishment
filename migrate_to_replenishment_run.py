"""
migrate_to_replenishment_run.py
================================
Run this ONCE on the bench server to rename all Auto Replenishment DocTypes
to the new Replenishment Run naming convention.

Usage (called automatically by build.sh — or manually):
    cd /home/erpnext/frappe-bench
    bench --site moosa.test execute \
        auto_replenishment.auto_replenishment.setup.migrate_to_replenishment_run.run

File location:
    apps/auto_replenishment/auto_replenishment/setup/migrate_to_replenishment_run.py

What this script does:
  1. Renames DocType records in tabDocType
  2. Renames DB tables  (tabXxx → tabYyy)
  3. Updates all parent/parenttype references in child tables
  4. Updates Link field values across all related tables
  5. Updates Custom Fields referencing old DocType names
  6. Clears DocType cache
  7. Does NOT touch the file system — that is handled by the shell script
"""

import frappe


# ── Name mapping ────────────────────────────────────────────────────────────

DOCTYPE_RENAMES = [
    # (old_doctype_name, new_doctype_name, old_table, new_table)
    (
        "Auto Replenishment Log",
        "Replenishment Run",
        "tabAuto Replenishment Log",
        "tabReplenishment Run",
    ),
    (
        "Auto Replenishment Log Store",
        "Replenishment Run Store",
        "tabAuto Replenishment Log Store",
        "tabReplenishment Run Store",
    ),
    (
        "Auto Replenishment Forecast",
        "Replenishment Store Plan",
        "tabAuto Replenishment Forecast",
        "tabReplenishment Store Plan",
    ),
    (
        "Auto Replenishment Forecast Item",
        "Replenishment Store Plan Item",
        "tabAuto Replenishment Forecast Item",
        "tabReplenishment Store Plan Item",
    ),
    (
        "AR Forecast Allocation",
        "Replenishment Allocation",
        "tabAR Forecast Allocation",
        "tabReplenishment Allocation",
    ),
    (
        "AR Forecast Material Request",
        "Replenishment MR Link",
        "tabAR Forecast Material Request",
        "tabReplenishment MR Link",
    ),
]

# ── Child table parenttype fixes (old_parenttype → new_parenttype) ──────────

PARENTTYPE_FIXES = [
    # (table, old_parenttype, new_parenttype)
    ("tabReplenishment Run Store",    "Auto Replenishment Log",      "Replenishment Run"),
    ("tabReplenishment Store Plan Item", "Auto Replenishment Forecast", "Replenishment Store Plan"),
    ("tabReplenishment Allocation",   "Auto Replenishment Forecast", "Replenishment Store Plan"),
    ("tabReplenishment MR Link",      "Auto Replenishment Forecast", "Replenishment Store Plan"),
]

# ── Link field value updates (table, field, old_value, new_value) ───────────
# These cover Link fields that stored old DocType names as values

LINK_VALUE_FIXES = [
    # tabDocField — field options that reference old DocType names
    ("tabDocField",    "options",
     "Auto Replenishment Log",       "Replenishment Run"),
    ("tabDocField",    "options",
     "Auto Replenishment Forecast",  "Replenishment Store Plan"),
    ("tabDocField",    "options",
     "AR Forecast Allocation",       "Replenishment Allocation"),
    ("tabDocField",    "options",
     "AR Forecast Material Request", "Replenishment MR Link"),
    ("tabDocField",    "options",
     "Auto Replenishment Log Store", "Replenishment Run Store"),
    ("tabDocField",    "options",
     "Auto Replenishment Forecast Item", "Replenishment Store Plan Item"),

    # tabCustom Field — same
    ("tabCustom Field", "options",
     "Auto Replenishment Log",       "Replenishment Run"),
    ("tabCustom Field", "options",
     "Auto Replenishment Forecast",  "Replenishment Store Plan"),

    # tabProperty Setter — dt field
    ("tabProperty Setter", "doc_type",
     "Auto Replenishment Log",       "Replenishment Run"),
    ("tabProperty Setter", "doc_type",
     "Auto Replenishment Forecast",  "Replenishment Store Plan"),
]


def run():
    """Entry point — called by bench execute."""
    frappe.logger().info("[AR Rename] Starting migration to Replenishment Run naming")
    errors = []

    # Step 1: Rename DocType records
    _step1_rename_doctype_records(errors)

    # Step 2: Rename database tables
    _step2_rename_tables(errors)

    # Step 3: Fix parenttype in child tables
    _step3_fix_parenttypes(errors)

    # Step 4: Fix Link field values
    _step4_fix_link_values(errors)

    # Step 5: Fix has_permission and other meta references
    _step5_fix_meta_references(errors)

    # Step 6: Clear cache
    frappe.clear_cache()
    frappe.db.commit()

    if errors:
        frappe.logger().warning(f"[AR Rename] Completed with {len(errors)} warnings:")
        for e in errors:
            frappe.logger().warning(f"  {e}")
    else:
        frappe.logger().info("[AR Rename] Migration completed successfully ✓")

    print("\n" + "=" * 60)
    print("Migration result:")
    print(f"  Renames attempted : {len(DOCTYPE_RENAMES)}")
    print(f"  Warnings          : {len(errors)}")
    if errors:
        print("\nWarnings (non-fatal):")
        for e in errors:
            print(f"  • {e}")
    print("=" * 60)
    print("\nNext step: run the shell build script.")
    print("=" * 60 + "\n")


# ── Step implementations ─────────────────────────────────────────────────────

def _step1_rename_doctype_records(errors):
    print("\n[1/5] Renaming DocType records...")
    for old_name, new_name, _, _ in DOCTYPE_RENAMES:
        try:
            exists = frappe.db.exists("DocType", old_name)
            already_renamed = frappe.db.exists("DocType", new_name)

            if already_renamed and not exists:
                print(f"  SKIP  {old_name} → already renamed to {new_name}")
                continue

            if not exists:
                print(f"  SKIP  {old_name} → not found in DocType table")
                continue

            # Update the DocType record name
            frappe.db.sql(
                "UPDATE `tabDocType` SET name = %s, modified = NOW() WHERE name = %s",
                (new_name, old_name)
            )
            # Update tabDocField parent references
            frappe.db.sql(
                "UPDATE `tabDocField` SET parent = %s WHERE parent = %s",
                (new_name, old_name)
            )
            # Update tabDocPerm
            frappe.db.sql(
                "UPDATE `tabDocPerm` SET parent = %s WHERE parent = %s",
                (new_name, old_name)
            )
            print(f"  OK    {old_name} → {new_name}")

        except Exception as e:
            msg = f"Step 1 {old_name}: {e}"
            errors.append(msg)
            print(f"  WARN  {msg}")


def _step2_rename_tables(errors):
    print("\n[2/5] Renaming database tables...")

    # Get existing tables
    existing_tables = {
        row[0] for row in frappe.db.sql("SHOW TABLES", as_list=True)
    }

    for _, _, old_table, new_table in DOCTYPE_RENAMES:
        try:
            if new_table in existing_tables:
                print(f"  SKIP  {old_table} → table {new_table} already exists")
                continue
            if old_table not in existing_tables:
                print(f"  SKIP  {old_table} → table not found")
                continue

            frappe.db.sql(f"RENAME TABLE `{old_table}` TO `{new_table}`")
            print(f"  OK    {old_table} → {new_table}")

        except Exception as e:
            msg = f"Step 2 {old_table}: {e}"
            errors.append(msg)
            print(f"  WARN  {msg}")


def _step3_fix_parenttypes(errors):
    print("\n[3/5] Fixing parenttype in child tables...")
    for table, old_pt, new_pt in PARENTTYPE_FIXES:
        try:
            count = frappe.db.sql(
                f"SELECT COUNT(*) FROM `{table}` WHERE parenttype = %s",
                old_pt
            )[0][0]

            if count == 0:
                print(f"  SKIP  {table} parenttype={old_pt} → no rows")
                continue

            frappe.db.sql(
                f"UPDATE `{table}` SET parenttype = %s WHERE parenttype = %s",
                (new_pt, old_pt)
            )
            print(f"  OK    {table}: {count} rows parenttype {old_pt} → {new_pt}")

        except Exception as e:
            msg = f"Step 3 {table}: {e}"
            errors.append(msg)
            print(f"  WARN  {msg}")


def _step4_fix_link_values(errors):
    print("\n[4/5] Fixing Link field values...")
    for table, field, old_val, new_val in LINK_VALUE_FIXES:
        try:
            count = frappe.db.sql(
                f"SELECT COUNT(*) FROM `{table}` WHERE `{field}` = %s",
                old_val
            )[0][0]

            if count == 0:
                continue

            frappe.db.sql(
                f"UPDATE `{table}` SET `{field}` = %s WHERE `{field}` = %s",
                (new_val, old_val)
            )
            print(f"  OK    {table}.{field}: {count} rows {old_val!r} → {new_val!r}")

        except Exception as e:
            msg = f"Step 4 {table}.{field}: {e}"
            errors.append(msg)
            print(f"  WARN  {msg}")


def _step5_fix_meta_references(errors):
    print("\n[5/5] Fixing meta references...")

    # Fix has_permission references in tabDocType
    fixes = [
        (
            "auto_replenishment.auto_replenishment.doctype"
            ".auto_replenishment_forecast.auto_replenishment_forecast"
            ".has_permission",
            "auto_replenishment.auto_replenishment.doctype"
            ".replenishment_store_plan.replenishment_store_plan"
            ".has_permission",
        ),
    ]

    for old_val, new_val in fixes:
        try:
            frappe.db.sql(
                "UPDATE `tabDocType` SET has_permission = %s WHERE has_permission = %s",
                (new_val, old_val)
            )
            print(f"  OK    has_permission reference updated")
        except Exception as e:
            errors.append(f"Step 5 has_permission: {e}")

    # Fix module_def if exists
    try:
        frappe.db.sql(
            "UPDATE `tabModule Def` SET module_name = 'Auto Replenishment' "
            "WHERE module_name = 'Auto Replenishment'"
        )  # no change needed — module name stays the same
    except Exception:
        pass

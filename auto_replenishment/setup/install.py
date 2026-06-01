"""
auto_replenishment/setup/install.py

Installation and uninstallation script for the Auto Replenishment app.

Available commands (run via bench execute):
    auto_replenishment.setup.install.after_install
    auto_replenishment.setup.install.create_performance_indexes
    auto_replenishment.setup.install.drop_performance_indexes
    auto_replenishment.setup.install.after_uninstall
    auto_replenishment.setup.install.verify_installation

Frappe hooks (called automatically):
    after_install  → runs after `bench install-app`
    after_uninstall → runs after `bench remove-app`
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# ---------------------------------------------------------------------------
# Frappe lifecycle hooks (called automatically by bench)
# ---------------------------------------------------------------------------


def after_install():
    """
    Main post-install hook — called automatically by `bench install-app`.
    Run manually: bench --site [site] execute auto_replenishment.setup.install.after_install
    """
    print("\n=== Auto Replenishment: Starting Installation ===")

    try:
        print("  [1/4] Creating Item Master custom fields...")
        create_item_master_custom_fields()
        print("        ✓ Item Master fields created.")

        print("  [2/4] Creating Material Request custom fields...")
        create_material_request_custom_fields()
        print("        ✓ Material Request fields created.")

        print("  [3/4] Creating performance indexes...")
        create_performance_indexes()
        print("        ✓ Performance indexes created.")

        print("  [4/4] Setting up default Replenishment Config...")
        _create_default_config()
        print("        ✓ Default config ready (update Central Warehouse in UI).")

        frappe.db.commit()
        print("\n=== Auto Replenishment: Installation Complete ===")
        print("    Next step: Configure 'Replenishment Config' in the UI.")

    except Exception as e:
        frappe.db.rollback()
        print(f"\n[ERROR] Auto Replenishment installation failed: {e}")
        frappe.log_error(frappe.get_traceback(), "Auto Replenishment Install Error")
        raise


def after_uninstall():
    """
    Cleanup hook — called automatically by `bench remove-app`.
    Run manually: bench --site [site] execute auto_replenishment.setup.install.after_uninstall
    Removes custom fields and performance indexes added by this app.
    Does NOT delete forecast/log data — remove DocType records manually if needed.
    """
    print("\n=== Auto Replenishment: Starting Uninstall Cleanup ===")

    try:
        print("  [1/3] Removing Item Master custom fields...")
        _remove_custom_fields(
            "Item",
            [
                "custom_auto_replenishment_section",
                "custom_exclude_from_replenishment",
                "custom_safety_days",
                "custom_replenishment_notes",
            ],
        )
        print("        ✓ Item Master fields removed.")

        print("  [2/3] Removing Material Request custom fields...")
        _remove_custom_fields(
            "Material Request",
            [
                "custom_auto_replenishment_forecast",
                "custom_source_warehouse",
            ],
        )
        _remove_custom_fields(
            "Material Request Item",
            [
                "custom_forecast_item",
            ],
        )
        print("        ✓ Material Request fields removed.")

        print("  [3/3] Dropping performance indexes...")
        drop_performance_indexes()
        print("        ✓ Performance indexes dropped.")

        frappe.db.commit()
        print("\n=== Auto Replenishment: Uninstall Cleanup Complete ===")

    except Exception as e:
        frappe.db.rollback()
        print(f"\n[ERROR] Auto Replenishment uninstall cleanup failed: {e}")
        frappe.log_error(frappe.get_traceback(), "Auto Replenishment Uninstall Error")
        raise


# ---------------------------------------------------------------------------
# Custom field creation
# ---------------------------------------------------------------------------


def create_item_master_custom_fields():
    """Add Auto Replenishment fields to Item Master."""
    custom_fields = {
        "Item": [
            {
                "fieldname": "custom_auto_replenishment_section",
                "fieldtype": "Section Break",
                "label": "Auto Replenishment",
                "insert_after": "reorder_levels",
                "collapsible": 1,
                "module": "Auto Replenishment",
            },
            {
                "fieldname": "custom_exclude_from_replenishment",
                "fieldtype": "Check",
                "label": "Exclude from Auto Replenishment",
                "insert_after": "custom_auto_replenishment_section",
                "description": (
                    "Tick to exclude this item from automated replenishment "
                    "(e.g. discontinued lines, items managed manually)."
                ),
                "module": "Auto Replenishment",
            },
            {
                "fieldname": "custom_safety_days",
                "fieldtype": "Int",
                "label": "Custom Safety Days",
                "insert_after": "custom_exclude_from_replenishment",
                "description": (
                    "Override the system-wide safety days for this specific item. "
                    "Leave 0 to use the default from Replenishment Config."
                ),
                "default": "0",
                "module": "Auto Replenishment",
            },
            {
                "fieldname": "custom_replenishment_notes",
                "fieldtype": "Small Text",
                "label": "Replenishment Notes",
                "insert_after": "custom_safety_days",
                "description": "Free-text notes visible to the Allocator during forecast review.",
                "module": "Auto Replenishment",
            },
        ]
    }
    create_custom_fields(custom_fields, ignore_validate=True)


def create_material_request_custom_fields():
    """Add AR tracking fields to Material Request and Material Request Item."""
    custom_fields = {
        "Material Request": [
            {
                "fieldname": "custom_auto_replenishment_section",
                "fieldtype": "Section Break",
                "label": "Auto Replenishment",
                "insert_after": "amended_from",
                "collapsible": 1,
                "module": "Auto Replenishment",
            },
            {
                "fieldname": "custom_auto_replenishment_forecast",
                "fieldtype": "Link",
                "label": "Replenishment Store Plan",
                "options": "Replenishment Store Plan",
                "insert_after": "custom_auto_replenishment_section",
                "read_only": 1,
                "description": "The forecast document that triggered this Material Request.",
                "module": "Auto Replenishment",
            },
            {
                "fieldname": "custom_source_warehouse",
                "fieldtype": "Link",
                "label": "Source Warehouse (AR)",
                "options": "Warehouse",
                "insert_after": "custom_auto_replenishment_forecast",
                "read_only": 1,
                "description": "Supply source for this auto-replenishment MR (warehouse or donor store).",
                "module": "Auto Replenishment",
            },
        ],
        "Material Request Item": [
            {
                "fieldname": "custom_forecast_item",
                "fieldtype": "Data",
                "label": "Forecast Item Ref",
                "insert_after": "item_name",
                "read_only": 1,
                "hidden": 1,
                "description": "Internal reference to the Auto Replenishment Forecast Item row.",
                "module": "Auto Replenishment",
            },
        ],
    }
    create_custom_fields(custom_fields, ignore_validate=True)


# ---------------------------------------------------------------------------
# Performance indexes (CRITICAL for 300K items × 35 stores)
# ---------------------------------------------------------------------------

# Each entry: (table, index_name, columns, extra_clause)
# extra_clause is appended verbatim after the column list (e.g. for partial indexes)
_INDEXES = [
    # ── Stock Ledger Entry ──────────────────────────────────────────────────
    # Main selling-rate lookup: outbound movements per warehouse per date range
    (
        "tabStock Ledger Entry",
        "idx_ar_sle_wh_date_type",
        "`warehouse`, `posting_date`, `voucher_type`, `actual_qty`",
        "",
    ),
    # Secondary: item_code lookup within a warehouse (used in multi-warehouse donor eval)
    (
        "tabStock Ledger Entry",
        "idx_ar_sle_item_wh_date",
        "`item_code`, `warehouse`, `posting_date`",
        "",
    ),
    # ── Bin ────────────────────────────────────────────────────────────────
    # Primary: current on-hand per item per warehouse — single most-used lookup
    (
        "tabBin",
        "idx_ar_bin_item_wh",
        "`item_code`, `warehouse`",
        "",
    ),
    # ── Stock Entry Detail ─────────────────────────────────────────────────
    # In-transit lookup: transfers arriving at a warehouse
    (
        "tabStock Entry Detail",
        "idx_ar_sed_item_twh",
        "`item_code`, `t_warehouse`",
        "",
    ),
    # ── Stock Entry (parent) ───────────────────────────────────────────────
    # Filter by type + docstatus + schedule_date when joining to SED
    (
        "tabStock Entry",
        "idx_ar_se_type_status_date",
        "`stock_entry_type`, `docstatus`",
        "",
    ),
    # ── Item ───────────────────────────────────────────────────────────────
    # Exclusion flag + disabled filter — scanned on every forecast run
    (
        "tabItem",
        "idx_ar_item_excl_disabled",
        "`custom_exclude_from_replenishment`, `disabled`",
        "",
    ),
    # ── Purchase Order Item ────────────────────────────────────────────────
    # In-transit PO stock: item + schedule_date filter
    (
        "tabPurchase Order Item",
        "idx_ar_poi_item_date",
        "`item_code`, `schedule_date`",
        "",
    ),
    # ── Auto Replenishment Forecast Item (child table) ─────────────────────
    # Supply status reporting queries
    (
        "tabAuto Replenishment Forecast Item",
        "idx_ar_fi_status_parent",
        "`supply_status`, `parent`",
        "",
    ),
    # Item code lookup within forecast items (used in allocator agent)
    (
        "tabAuto Replenishment Forecast Item",
        "idx_ar_fi_item_parent",
        "`item_code`, `parent`",
        "",
    ),
    # ── Auto Replenishment Forecast ────────────────────────────────────────
    # Duplicate-run check: warehouse + forecast_date + docstatus
    (
        "tabAuto Replenishment Forecast",
        "idx_ar_forecast_wh_date",
        "`warehouse`, `forecast_date`, `docstatus`",
        "",
    ),
]


def create_performance_indexes():
    """
    Create all performance indexes required by Auto Replenishment.

    Safe to run multiple times — skips indexes that already exist.
    Run manually: bench --site [site] execute auto_replenishment.setup.install.create_performance_indexes
    """
    print("\n  Creating Auto Replenishment performance indexes...")
    created = 0
    skipped = 0
    failed = 0

    for table, index_name, columns, extra in _INDEXES:
        result = _create_index_if_not_exists(table, index_name, columns, extra)
        if result == "created":
            created += 1
            print(f"    ✓ Created:  {index_name}  on  {table}")
        elif result == "skipped":
            skipped += 1
            print(f"    – Skipped:  {index_name}  (already exists)")
        else:
            failed += 1
            print(f"    ✗ FAILED:   {index_name}  on  {table}  — {result}")

    frappe.db.commit()
    print(
        f"\n  Index summary: {created} created, {skipped} already existed, {failed} failed."
    )
    if failed:
        print(
            "  WARNING: Some indexes failed to create. "
            "Run with --verbose or check the Error Log for details."
        )


def drop_performance_indexes():
    """
    Drop all Auto Replenishment performance indexes.

    Called during uninstall. Safe to run multiple times — skips missing indexes.
    Run manually: bench --site [site] execute auto_replenishment.setup.install.drop_performance_indexes
    """
    print("\n  Dropping Auto Replenishment performance indexes...")
    dropped = 0
    skipped = 0

    for table, index_name, _, __ in _INDEXES:
        if _index_exists(table, index_name):
            try:
                frappe.db.sql(f"ALTER TABLE `{table}` DROP INDEX `{index_name}`")
                dropped += 1
                print(f"    ✓ Dropped:  {index_name}  from  {table}")
            except Exception as e:
                print(f"    ✗ FAILED to drop {index_name}: {e}")
        else:
            skipped += 1
            print(f"    – Skipped:  {index_name}  (not found)")

    frappe.db.commit()
    print(f"\n  Drop summary: {dropped} dropped, {skipped} not found.")


# ---------------------------------------------------------------------------
# Verification utility
# ---------------------------------------------------------------------------


def verify_installation():
    """
    Check that all custom fields and indexes are present and correct.

    Run manually: bench --site [site] execute auto_replenishment.setup.install.verify_installation
    Useful after upgrades or if something seems wrong.
    """
    print("\n=== Auto Replenishment: Installation Verification ===\n")
    all_ok = True

    # ── Check custom fields ─────────────────────────────────────────────────
    required_fields = {
        "Item": [
            "custom_exclude_from_replenishment",
            "custom_safety_days",
            "custom_replenishment_notes",
        ],
        "Material Request": [
            "custom_auto_replenishment_forecast",
            "custom_source_warehouse",
        ],
        "Material Request Item": [
            "custom_forecast_item",
        ],
    }

    print("  Custom Fields:")
    for doctype, fields in required_fields.items():
        for fieldname in fields:
            exists = frappe.db.exists(
                "Custom Field", {"dt": doctype, "fieldname": fieldname}
            )
            if exists:
                print(f"    ✓  {doctype}.{fieldname}")
            else:
                print(f"    ✗  MISSING: {doctype}.{fieldname}")
                all_ok = False

    # ── Check indexes ───────────────────────────────────────────────────────
    print("\n  Performance Indexes:")
    for table, index_name, columns, _ in _INDEXES:
        if _index_exists(table, index_name):
            print(f"    ✓  {index_name}")
        else:
            print(f"    ✗  MISSING: {index_name}  on  {table}")
            all_ok = False

    # ── Check Replenishment Config ──────────────────────────────────────────
    print("\n  Replenishment Config:")
    try:
        cfg = frappe.get_single("Replenishment Config")
        if cfg.central_warehouse:
            print(f"    ✓  Central Warehouse: {cfg.central_warehouse}")
        else:
            print("    ✗  Central Warehouse NOT set — required before first run")
            all_ok = False
        print(f"    ✓  Demand History Days: {cfg.demand_history_days}")
        print(f"    ✓  Safety Days: {cfg.safety_days}")
        print(
            f"    ✓  Internal Lead Time: {cfg.internal_intransit_lead_time_days} day(s)"
        )
        print(f"    ✓  Protection Days: {cfg.protection_days}")
    except Exception as e:
        print(f"    ✗  Could not read Replenishment Config: {e}")
        all_ok = False

    # ── Check DocTypes exist ────────────────────────────────────────────────
    print("\n  DocTypes:")
    required_doctypes = [
        "Replenishment Store Plan",
        "Replenishment Store Plan Item",
        "Replenishment Run",
        "Replenishment Config",
    ]
    for dt in required_doctypes:
        if frappe.db.exists("DocType", dt):
            print(f"    ✓  {dt}")
        else:
            print(f"    ✗  MISSING DocType: {dt}")
            all_ok = False

    print(
        f"\n{'=== All checks passed ===' if all_ok else '=== ISSUES FOUND — see above ==='}\n"
    )
    return all_ok


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _create_index_if_not_exists(
    table: str, index_name: str, columns: str, extra: str = ""
) -> str:
    """
    Create a database index if it does not already exist.

    Returns:
        "created"  — index was created successfully
        "skipped"  — index already exists, nothing done
        str        — error message if creation failed
    """
    if _index_exists(table, index_name):
        return "skipped"

    # Verify the table itself exists before attempting to add an index.
    # Forecast child tables may not exist yet if migrations haven't run.
    if not _table_exists(table):
        return f"table '{table}' does not exist yet — run bench migrate first"

    try:
        sql = f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({columns})"
        if extra:
            sql += f" {extra}"
        frappe.db.sql(sql)
        return "created"
    except Exception as e:
        error_msg = str(e)
        frappe.log_error(
            f"Failed to create index {index_name} on {table}: {error_msg}",
            "Auto Replenishment Index Error",
        )
        return error_msg


def _index_exists(table: str, index_name: str) -> bool:
    """Return True if the named index already exists on the table."""
    result = frappe.db.sql(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE table_schema = DATABASE() "
        "  AND table_name = %s "
        "  AND index_name = %s",
        (table, index_name),
    )
    return bool(result and result[0][0] > 0)


def _table_exists(table: str) -> bool:
    """Return True if the MariaDB table exists in the current schema."""
    result = frappe.db.sql(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return bool(result and result[0][0] > 0)


def _create_default_config():
    """
    Insert a Replenishment Config record with sensible defaults if one
    does not already exist. Central Warehouse is left blank — the admin
    must set it in the UI before the first forecast run.
    """
    if frappe.db.exists("Replenishment Config", "Replenishment Config"):
        return  # Already configured — don't overwrite user settings

    try:
        cfg = frappe.new_doc("Replenishment Config")
        cfg.demand_history_days = 30
        cfg.safety_days = 7
        cfg.internal_intransit_lead_time_days = 3
        cfg.protection_days = 5
        cfg.forecast_schedule = "Daily"
        cfg.auto_create_forecast = 1
        cfg.batch_size = 500
        cfg.parallel_workers = 4
        cfg.enable_redis_cache = 1
        cfg.insert(ignore_permissions=True)
    except Exception as e:
        # Non-fatal — config can be created manually in UI
        print(f"    Note: Could not create default config automatically: {e}")


def _remove_custom_fields(doctype: str, fieldnames: list):
    """
    Delete custom fields by fieldname for a given doctype.
    Safe to call when fields are already absent.
    """
    for fieldname in fieldnames:
        name = frappe.db.get_value(
            "Custom Field", {"dt": doctype, "fieldname": fieldname}
        )
        if name:
            frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)

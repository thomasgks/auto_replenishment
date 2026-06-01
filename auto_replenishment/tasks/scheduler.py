"""
auto_replenishment/tasks/scheduler.py

Scheduled tasks for automated forecast generation.
Optimised for 300K items × 35 stores:
  - One background job per store (parallel workers)
  - One Auto Replenishment Log DB record per run (lightweight)
  - Per-store child rows in that record (status, timings, metrics)
  - Detailed per-store file logs written via StoreLogger (zero DB impact)
  - UI polls log files via get_store_log_content() for live-tail viewing
"""

import frappe
from frappe.utils import now_datetime, today
from datetime import date, datetime
import time

# ---------------------------------------------------------------------------
# Public scheduler entry points (called by Frappe scheduler)
# ---------------------------------------------------------------------------


def run_daily_forecast():
    """Called automatically by the Frappe daily scheduler event."""
    _run_scheduled_forecast("Daily")


def run_weekly_forecast():
    """Called automatically by the Frappe weekly scheduler event."""
    _run_scheduled_forecast("Weekly")


def trigger_manual_forecast():
    """
    Whitelisted API entry point for manual runs triggered from the UI
    or bench execute.

    Returns:
        dict with log_name and enqueued store count.
    """
    return _run_scheduled_forecast("Manual")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _run_scheduled_forecast(schedule_type: str) -> dict:
    """
    Create the master Log document, add one child row per store (status=Queued),
    then enqueue one background job per store.

    Database impact: O(1) inserts regardless of item count — only the Log
    header + one child row per store.  All detail goes to log files.
    """
    try:
        config_doc = frappe.get_single("Replenishment Config")

        # ── Guard: respect schedule setting ───────────────────────────────
        if not config_doc.auto_create_forecast and schedule_type != "Manual":
            frappe.logger().info(
                "[AR Scheduler] auto_create_forecast disabled — skipping."
            )
            return {}

        if config_doc.forecast_schedule != schedule_type and schedule_type not in (
            "Manual",
        ):
            frappe.logger().info(
                f"[AR Scheduler] Schedule type '{schedule_type}' does not match "
                f"config setting '{config_doc.forecast_schedule}' — skipping."
            )
            return {}

        if not config_doc.central_warehouse:
            frappe.log_error(
                "Auto Replenishment: Central Warehouse is not configured in "
                "Replenishment Config — cannot run forecast.",
                "AR Scheduler",
            )
            return {}

        config = _build_config_dict(config_doc, schedule_type)

        # ── Fetch store warehouses ─────────────────────────────────────────
        # Use item_filter_engine to respect scheduler filters from config
        from auto_replenishment.utils.item_filter_engine import get_store_warehouses_from_config
        store_warehouses = get_store_warehouses_from_config(config_doc)

        if not store_warehouses:
            frappe.log_error(
                "Auto Replenishment: No store warehouses found. "
                "Ensure warehouses are not disabled and are different from the Central Warehouse.",
                "AR Scheduler",
            )
            return {}

        run_date = str(date.today())

        # ── Create master Log document with per-store child rows ───────────
        log = frappe.new_doc("Replenishment Run")
        log.run_date         = run_date
        log.run_type         = "Scheduler" if schedule_type != "Manual" else "Manual"
        log.status           = "Forecasting"
        log.filters_locked   = 1
        log.allocation_status = "Not Started"
        log.total_stores     = len(store_warehouses)
        log.processed_stores = 0
        log.error_count      = 0
        log.started_at       = now_datetime()

        for wh in store_warehouses:
            log.append(
                "store_logs",
                {
                    "store_warehouse": wh,
                    "status": "Queued",
                    "queued_at": now_datetime(),
                    "log_file_path": "",  # filled in by the worker
                },
            )

        log.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(
            f"[AR Scheduler] Log created: {log.name}  "
            f"Enqueuing {len(store_warehouses)} store jobs for run_date={run_date}"
        )

        # ── Build a warehouse → child row name map so workers can update ───
        store_row_map = {
            row.store_warehouse: row.name
            for row in frappe.get_doc("Replenishment Run", log.name).store_logs
        }

        # ── Enqueue one background job per store ───────────────────────────
        for wh in store_warehouses:
            frappe.enqueue(
                "auto_replenishment.tasks.scheduler.generate_forecast_for_store",
                queue="long",
                timeout=7200,
                is_async=True,
                warehouse=wh,
                config=config,
                log_name=log.name,
                store_row_name=store_row_map.get(wh, ""),
                run_date=run_date,
                replenishment_run=log.name,
            )

        return {
            "log_name": log.name,
            "stores_enqueued": len(store_warehouses),
            "message": (
                f"Forecast jobs enqueued for {len(store_warehouses)} stores. "
                f"Log: {log.name}"
            ),
        }

    except Exception as e:
        frappe.log_error(
            f"AR Scheduler orchestrator failed: {e}\n{frappe.get_traceback()}",
            "AR Scheduler Error",
        )
        return {}


# ---------------------------------------------------------------------------
# Per-store background worker
# ---------------------------------------------------------------------------


def generate_forecast_for_store(
    warehouse: str,
    config: dict,
    log_name: str,
    store_row_name: str,
    run_date: str,
    replenishment_run: str = "",
):
    """
    Background worker (runs in a separate Frappe worker process).

    Responsibilities:
      1. Create a StoreLogger → opens the per-store log file
      2. Log all phases of the forecast calculation with full detail
      3. Update the DB child row status (Queued → Running → Completed/Failed)
         using direct SQL to avoid row-level locking
      4. Update the parent log processed_stores counter atomically
    """
    from auto_replenishment.utils.forecast_engine import (
        run_forecast_for_store,
        _get_eligible_items,
    )
    from auto_replenishment.utils.store_logger import StoreLogger

    as_of = date.fromisoformat(run_date)
    worker_start = time.monotonic()

    # ── Mark child row as Running ──────────────────────────────────────────
    _update_store_row(
        store_row_name,
        {
            "status": "Running",
            "started_at": now_datetime(),
        },
    )

    with StoreLogger(log_name=log_name, warehouse=warehouse, run_date=run_date) as log:

        # Store the log file path in the child row immediately so UI can open it
        _update_store_row(store_row_name, {"log_file_path": log.file_path or ""})

        # ── Log run configuration ──────────────────────────────────────────
        log.step("RUN CONFIGURATION")
        log.info(f"  Run Date                  : {run_date}")
        log.info(
            f"  Schedule Type             : {config.get('_schedule_type', 'Unknown')}"
        )
        log.info(f"  Central Warehouse         : {config.get('central_warehouse')}")
        log.info(
            f"  Demand History Window     : {config.get('demand_history_days')} days"
        )
        log.info(f"  Default Safety Days       : {config.get('safety_days')} days")
        log.info(
            f"  Internal Lead Time        : {config.get('internal_intransit_lead_time_days')} days"
        )
        log.info(f"  Donor Protection Days     : {config.get('protection_days')} days")
        log.info(
            f"  Batch Size                : {config.get('batch_size')} items/batch"
        )

        try:
            # ── Check for duplicate ───────────────────────────────────────
            log.step("DUPLICATE CHECK")
            log.info(
                f"Checking if forecast already exists for {warehouse} on {run_date} ..."
            )

            # Only skip if a Store Plan for THIS specific Run already exists
            # (not plans from other runs on the same date with different filters)
            existing = frappe.db.exists(
                "Replenishment Store Plan",
                {
                    "warehouse":          warehouse,
                    "forecast_date":      run_date,
                    "replenishment_run":  log_name,
                    "docstatus":          ["!=", 2],
                },
            )

            if existing:
                log.warning(
                    f"Forecast document '{existing}' already exists for this run+store. "
                    f"Skipping to avoid duplicates."
                )
                _update_store_row(
                    store_row_name, {"status": "Skipped (Already Exists)"}
                )
                _increment_processed(log_name, error=False)
                return

            log.success("No duplicate found — proceeding with forecast calculation.")

            # ── Phase 1: Eligible items query ─────────────────────────────
            log.step("PHASE 1 — ELIGIBLE ITEMS QUERY")
            log.info(f"Querying items eligible for replenishment at [{warehouse}] ...")
            log.info(
                f"Eligibility criteria: "
                f"(1) Not excluded from replenishment, "
                f"(2) Has supply potential in company, "
                f"(3) Has sales or stock in this store within last "
                f"{config.get('demand_history_days')} days."
            )

            t0 = time.monotonic()
            eligible_df = _get_eligible_items(
                warehouse,
                config.get("central_warehouse"),
                as_of,
                config,
            )
            elapsed_eligible = time.monotonic() - t0

            # Apply Run-level filters to eligible items too
            eligible_df = _apply_item_filters(eligible_df, config)
            eligible_count = len(eligible_df) if not eligible_df.empty else 0
            log.metric("eligible_items_found", eligible_count, "items")
            log.metric("eligible_query_time", f"{elapsed_eligible:.2f}", "seconds")

            if eligible_df.empty:
                log.warning(
                    "Zero eligible items found for this store. Possible reasons:\n"
                    "  • All items are flagged as 'Exclude from Replenishment'\n"
                    "  • No sales history in the last "
                    f"{config.get('demand_history_days')} days\n"
                    "  • No current stock AND no open POs for this store\n"
                    "  • Warehouse name mismatch in Stock Ledger Entry"
                )
                _finalize_store_row(
                    store_row_name,
                    status="Completed (No Items)",
                    items_eligible=0,
                    items_requiring=0,
                    duration=time.monotonic() - worker_start,
                    replenishment_run=replenishment_run,
                    run_type=config.get("_schedule_type", "Manual"),
                )
                _increment_processed(log_name, error=False)
                return

            log.success(
                f"Eligible items query complete: {eligible_count} items in scope."
            )

            # ── Phase 2: Full forecast calculation ────────────────────────
            log.step("PHASE 2 — FORECAST CALCULATIONS")
            log.info(
                f"Running vectorised forecast for {eligible_count} items across "
                f"6 calculation steps ..."
            )
            log.info("  Step 1: Selling Rate  = Total Sales ÷ Demand History Days")
            log.info("  Step 2: Lead Time Demand = Selling Rate × Internal Lead Time")
            log.info("  Step 3: Safety Stock  = Selling Rate × Safety Days")
            log.info("  Step 4: Target Stock  = Lead Time Demand + Safety Stock")
            log.info("  Step 5: Effective OnHand = Current OnHand + Usable In-Transit")
            log.info(
                "  Step 6: Forecasted Req = max(0, Target Stock − Effective OnHand)"
            )

            t1 = time.monotonic()
            result_df = run_forecast_for_store(warehouse, config, as_of)
            elapsed_calc = time.monotonic() - t1

            # Apply Run-level item filters (supplier, item group, year, season)
            result_df = _apply_item_filters(result_df, config, log)

            items_requiring = len(result_df) if not result_df.empty else 0

            log.metric("calculation_time", f"{elapsed_calc:.2f}", "seconds")
            log.metric("items_requiring_replenishment", items_requiring, "items")
            log.metric(
                "items_filtered_out",
                eligible_count - items_requiring,
                "items (requirement was zero)",
            )

            if not result_df.empty:
                # Log distribution statistics
                log.step("PHASE 2 — CALCULATION STATISTICS")
                try:
                    avg_req = result_df["forecasted_requirement"].mean()
                    max_req = result_df["forecasted_requirement"].max()
                    min_req = result_df["forecasted_requirement"].min()
                    zero_onhand = (result_df["current_onhand"] == 0).sum()
                    has_intransit = (result_df["usable_intransit"] > 0).sum()

                    log.metric("avg_forecasted_requirement", f"{avg_req:.2f}", "units")
                    log.metric("max_forecasted_requirement", f"{max_req:.2f}", "units")
                    log.metric("min_forecasted_requirement", f"{min_req:.2f}", "units")
                    log.metric("items_with_zero_onhand", zero_onhand, "items")
                    log.metric("items_with_usable_intransit", has_intransit, "items")

                    # Top 10 items by requirement — helps spot data issues
                    log.info("")
                    log.info("  Top 10 items by forecasted requirement:")
                    top10 = result_df.nlargest(10, "forecasted_requirement")[
                        [
                            "item_code",
                            "item_name",
                            "selling_rate",
                            "effective_onhand",
                            "target_stock",
                            "forecasted_requirement",
                        ]
                    ]
                    header = (
                        f"  {'Item Code':<20} {'Item Name':<30} "
                        f"{'Rate/day':>8} {'Eff.OH':>8} "
                        f"{'Target':>8} {'Req.':>8}"
                    )
                    log.info(header)
                    log.info("  " + "-" * (len(header) - 2))
                    for _, r in top10.iterrows():
                        name_trunc = str(r.get("item_name", ""))[:28]
                        log.info(
                            f"  {str(r['item_code']):<20} {name_trunc:<30} "
                            f"{r['selling_rate']:>8.3f} {r['effective_onhand']:>8.1f} "
                            f"{r['target_stock']:>8.1f} {r['forecasted_requirement']:>8.1f}"
                        )
                except Exception as stats_err:
                    log.warning(f"Could not compute statistics: {stats_err}")

            if result_df.empty:
                log.info(
                    "No items have a forecasted requirement > 0. "
                    "The store is sufficiently stocked against all targets."
                )
                _finalize_store_row(
                    store_row_name,
                    status="Completed (No Items)",
                    items_eligible=eligible_count,
                    items_requiring=0,
                    duration=time.monotonic() - worker_start,
                    replenishment_run=replenishment_run,
                    run_type=config.get("_schedule_type", "Manual"),
                )
                _increment_processed(log_name, error=False)
                return

            # ── Phase 3: Create Forecast document ─────────────────────────
            log.step("PHASE 3 — CREATING FORECAST DOCUMENT")
            log.info(
                f"Saving Auto Replenishment Forecast document for [{warehouse}] "
                f"with {items_requiring} items ..."
            )

            t2 = time.monotonic()
            doc = frappe.new_doc("Replenishment Store Plan")
            doc.warehouse = warehouse
            doc.forecast_date = run_date
            doc.status = "Draft"

            for _, row in result_df.iterrows():
                doc.append(
                    "items",
                    {
                        "item_code": row["item_code"],
                        "item_name": row.get("item_name", ""),
                        "uom": row.get("uom", "Nos"),
                        "selling_rate": float(row.get("selling_rate", 0)),
                        "lead_time_days": int(row.get("lead_time_days", 0)),
                        "safety_days": int(row.get("safety_days", 0)),
                        "lead_time_demand": float(row.get("lead_time_demand", 0)),
                        "safety_stock": float(row.get("safety_stock", 0)),
                        "target_stock": float(row.get("target_stock", 0)),
                        "current_onhand": float(row.get("current_onhand", 0)),
                        "usable_intransit": float(row.get("usable_intransit", 0)),
                        "effective_onhand": float(row.get("effective_onhand", 0)),
                        "forecasted_requirement": float(
                            row.get("forecasted_requirement", 0)
                        ),
                        "supply_status": "Pending",
                        "allocated_qty": 0,
                        "shortage_qty": 0,
                    },
                )

            doc.total_items = len(doc.items)
            doc.insert(ignore_permissions=True)
            elapsed_save = time.monotonic() - t2

            log.metric("document_save_time", f"{elapsed_save:.2f}", "seconds")
            log.success(
                f"Forecast document created: {doc.name}  "
                f"({items_requiring} items, saved in {elapsed_save:.1f}s)"
            )

            # ── Final summary ─────────────────────────────────────────────
            total_elapsed = time.monotonic() - worker_start
            log.step("SUMMARY")
            log.metric("total_elapsed_seconds", f"{total_elapsed:.1f}", "seconds")
            log.metric("eligible_items", eligible_count)
            log.metric("items_in_forecast", items_requiring)
            log.metric("forecast_document", doc.name)
            log.success(f"Store [{warehouse}] forecast completed successfully.")

            _finalize_store_row(
                store_row_name,
                status="Completed",
                items_eligible=eligible_count,
                items_requiring=items_requiring,
                duration=total_elapsed,
                forecast_doc=doc.name,
                replenishment_run=replenishment_run,
                run_type=config.get("_schedule_type", "Manual"),
            )
            _increment_processed(log_name, error=False)

        except Exception as exc:
            total_elapsed = time.monotonic() - worker_start
            tb = frappe.get_traceback()

            log.step("FATAL ERROR")
            log.error(
                f"Forecast generation failed for [{warehouse}]: {exc}",
                exc_info=True,
            )
            log.info("")
            log.info("Full traceback:")
            for line in tb.splitlines():
                log.error(f"  {line}")
            log.info("")
            log.info("Troubleshooting hints:")
            log.info("  • Check that the Warehouse exists and is not disabled")
            log.info("  • Verify Stock Ledger Entry records exist for this warehouse")
            log.info("  • Ensure 'tabBin' has rows for this warehouse")
            log.info("  • Check Replenishment Config — Central Warehouse must be set")
            log.info(f"  • Elapsed before failure: {total_elapsed:.1f}s")

            frappe.log_error(
                f"AR Forecast worker failed for {warehouse}: {exc}\n{tb}",
                "AR Worker Error",
            )

            _finalize_store_row(
                store_row_name,
                status="Failed",
                items_eligible=0,
                items_requiring=0,
                duration=total_elapsed,
                replenishment_run=replenishment_run,
                run_type=config.get("_schedule_type", "Manual"),
            )
            _increment_processed(log_name, error=True)


# ---------------------------------------------------------------------------
# DB update helpers — all use direct SQL for thread safety
# ---------------------------------------------------------------------------


def _apply_item_filters(df, config: dict, log=None):
    """
    Apply all Run-level item filters to a forecast dataframe.
    Fetches item_group / year / season from DB when not in dataframe.
    Filters: supplier, item_group (L4/L5), year, season.
    """
    if df is None or df.empty:
        return df

    item_codes = list(df["item_code"].unique())
    if not item_codes:
        return df

    # Collect all active filters
    suppliers   = config.get("filter_suppliers", []) or []
    groups_l4   = config.get("filter_item_groups_l4", []) or []
    groups_l5   = config.get("filter_item_groups_l5", []) or []
    # year/season may be a list (from child table) or comma-separated string (legacy)
    year_raw   = config.get("filter_year", "") or []
    season_raw = config.get("filter_season", "") or []
    year   = year_raw   if isinstance(year_raw, list)   else [y.strip() for y in str(year_raw).split(",")   if y.strip()]
    season = season_raw if isinstance(season_raw, list) else [s.strip() for s in str(season_raw).split(",") if s.strip()]

    # Nothing to filter
    if not any([suppliers, groups_l4, groups_l5, year, season]):
        return df

    # ── Supplier filter ───────────────────────────────────────────────────────
    if suppliers:
        ph = ", ".join(["%s"] * len(suppliers))
        # Check both variant item codes AND their template (variant_of)
        # because supplier is often set on the template, not each variant
        rows = frappe.db.sql(f"""
            SELECT DISTINCT i.name
            FROM `tabItem` i
            WHERE (
                EXISTS (
                    SELECT 1 FROM `tabItem Supplier` s
                    WHERE s.parent = i.name AND s.supplier IN ({ph})
                )
                OR EXISTS (
                    SELECT 1 FROM `tabItem Supplier` s
                    WHERE s.parent = i.variant_of AND s.supplier IN ({ph})
                    AND i.variant_of IS NOT NULL AND i.variant_of != ''
                )
            )
        """, suppliers + suppliers, as_list=True)
        allowed = {r[0] for r in rows}
        before  = len(df)
        df      = df[df["item_code"].isin(allowed)]
        if log:
            log.info(f"Supplier filter: kept {len(df)} of {before} items "
                     f"(suppliers: {suppliers}, checked variant+template)")
        item_codes = list(df["item_code"].unique())
        if df.empty:
            return df

    # ── Fetch item attributes from DB when needed ─────────────────────────────
    needs_group  = bool(groups_l4 or groups_l5)
    needs_year   = bool(year)    # now a list
    needs_season = bool(season)  # now a list

    if needs_group or needs_year or needs_season:
        ph = ", ".join(["%s"] * len(item_codes))

        # Get item_group from tabItem
        if needs_group and "item_group" not in df.columns:
            group_rows = frappe.db.sql(
                f"SELECT name, item_group FROM `tabItem` WHERE name IN ({ph})",
                item_codes, as_dict=True
            )
            group_map = {r.name: r.item_group for r in group_rows}
            df = df.copy()
            df["item_group"] = df["item_code"].map(group_map)

        # Get year/season from tabItem Variant Attribute
        if needs_year or needs_season:
            attr_rows = frappe.db.sql(f"""
                SELECT parent, attribute, attribute_value
                FROM `tabItem Variant Attribute`
                WHERE parent IN ({ph})
                  AND attribute IN ('Year','Season')
            """, item_codes, as_dict=True)
            year_map   = {}
            season_map = {}
            for r in attr_rows:
                if r.attribute == "Year":
                    year_map[r.parent]   = r.attribute_value
                elif r.attribute == "Season":
                    season_map[r.parent] = r.attribute_value
            df = df.copy()
            if needs_year:
                df["_year"]   = df["item_code"].map(year_map).fillna("")
            if needs_season:
                df["_season"] = df["item_code"].map(season_map).fillna("")

    # ── Item group L4 filter ──────────────────────────────────────────────────
    if groups_l4 and "item_group" in df.columns:
        from auto_replenishment.utils.item_filter_engine import _get_child_groups
        allowed = set(_get_child_groups(groups_l4))
        before  = len(df)
        df      = df[df["item_group"].isin(allowed)]
        if log:
            log.info(f"Item Group L4 filter: {before - len(df)} items removed")
        if df.empty:
            return df

    # ── Item group L5 filter ──────────────────────────────────────────────────
    if groups_l5 and "item_group" in df.columns:
        allowed = set(groups_l5)
        before  = len(df)
        df      = df[df["item_group"].isin(allowed)]
        if log:
            log.info(f"Item Group L5 filter: {before - len(df)} items removed")
        if df.empty:
            return df

    # ── Year filter ───────────────────────────────────────────────────────────
    if year and "_year" in df.columns:
        before = len(df)
        df     = df[df["_year"].isin(year)]
        if log:
            log.info(f"Year filter {year}: {before - len(df)} items removed")
        if df.empty:
            return df

    # ── Season filter ─────────────────────────────────────────────────────────
    if season and "_season" in df.columns:
        before = len(df)
        df     = df[df["_season"].isin(season)]
        if log:
            log.info(f"Season filter {season}: {before - len(df)} items removed")

    return df


def _build_config_dict(config_doc, schedule_type: str = "Manual") -> dict:
    # Read scheduler item filters from child tables
    filter_suppliers      = [r.supplier   for r in (config_doc.scheduler_suppliers      or []) if r.supplier]
    filter_groups_l4      = [r.item_group for r in (config_doc.scheduler_item_groups_l4 or []) if r.item_group]
    filter_groups_l5      = [r.item_group for r in (config_doc.scheduler_item_groups_l5 or []) if r.item_group]
    # Year/Season: child table stores attribute_value directly (Data field)
    filter_year           = [r.attribute_value for r in (config_doc.scheduler_year   or []) if r.attribute_value]
    filter_season         = [r.attribute_value for r in (config_doc.scheduler_season or []) if r.attribute_value]

    return {
        "demand_history_days":               config_doc.demand_history_days or 30,
        "safety_days":                       config_doc.safety_days or 7,
        "internal_intransit_lead_time_days": config_doc.internal_intransit_lead_time_days or 3,
        "protection_days":                   config_doc.protection_days or 5,
        "central_warehouse":                 config_doc.central_warehouse,
        "batch_size":                        config_doc.batch_size or 500,
        "parallel_workers":                  config_doc.parallel_workers or 4,
        "quantity_rounding":                 config_doc.quantity_rounding or "Ceil (Round Up)",
        "allocation_algorithm":              getattr(config_doc, "allocation_algorithm", "Pro-rata"),
        "enable_auto_allocation":            getattr(config_doc, "enable_auto_allocation", 0),
        "_schedule_type":                    schedule_type,
        # Item filters — applied by _apply_item_filters in every store job
        "filter_suppliers":                  filter_suppliers,
        "filter_item_groups_l4":             filter_groups_l4,
        "filter_item_groups_l5":             filter_groups_l5,
        "filter_year":                       filter_year,
        "filter_season":                     filter_season,
    }


def _update_store_row(store_row_name: str, fields: dict):
    """
    Update individual fields on an Auto Replenishment Log Store child row.
    Uses SET col=val SQL directly for thread-safety across parallel workers.
    """
    if not store_row_name:
        return
    try:
        set_clauses = ", ".join(f"`{k}` = %s" for k in fields)
        values = list(fields.values()) + [store_row_name]
        frappe.db.sql(
            f"UPDATE `tabReplenishment Run Store` SET {set_clauses} WHERE name = %s",
            values,
        )
        frappe.db.commit()
    except Exception as e:
        frappe.logger().warning(
            f"[AR] _update_store_row failed for {store_row_name}: {e}"
        )


def _finalize_store_row(
    store_row_name: str,
    status: str,
    items_eligible: int,
    items_requiring: int,
    duration: float,
    forecast_doc: str = "",
    replenishment_run: str = "",
    run_type: str = "Manual",
):
    """Write final completion fields to the store child row."""
    _update_store_row(
        store_row_name,
        {
            "status": status,
            "completed_at": now_datetime(),
            "duration_seconds": round(duration, 1),
            "items_eligible": items_eligible,
            "items_requiring_replenishment": items_requiring,
            "forecast_doc": forecast_doc,
            "store_plan": forecast_doc,
        },
    )

    # Link the Store Plan back to the Replenishment Run
    # Guard: only write if replenishment_run is a valid RR- document
    if replenishment_run and forecast_doc and replenishment_run.startswith("RR-"):
        try:
            frappe.db.set_value(
                "Replenishment Store Plan", forecast_doc,
                {
                    "replenishment_run": replenishment_run,
                    "run_type": run_type,
                }
            )
        except Exception:
            pass


def _increment_processed(log_name: str, error: bool):
    """
    Atomically increment processed_stores (and optionally error_count)
    on the parent Log document, then check if all stores are done.
    """
    if not log_name or log_name == "manual":
        return
    try:
        if error:
            frappe.db.sql(
                """
                UPDATE `tabReplenishment Run`
                SET processed_stores = processed_stores + 1,
                    error_count      = IFNULL(error_count, 0) + 1,
                    modified         = NOW()
                WHERE name = %s
            """,
                (log_name,),
            )
        else:
            frappe.db.sql(
                """
                UPDATE `tabReplenishment Run`
                SET processed_stores = processed_stores + 1,
                    modified         = NOW()
                WHERE name = %s
            """,
                (log_name,),
            )

        # Check completion by counting actual store row statuses
        # (reliable even when total_stores counter is 0 from older runs)
        store_counts = frappe.db.sql("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status IN ('Completed','Completed (No Items)','Failed')
                    THEN 1 ELSE 0 END) as done,
                SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END) as failed
            FROM `tabReplenishment Run Store`
            WHERE parent = %s
        """, log_name, as_dict=True)

        counts = store_counts[0] if store_counts else None

        # Only mark complete when ALL stores are in a terminal state
        if (counts and int(counts.total or 0) > 0
                and int(counts.done or 0) >= int(counts.total or 0)):

            row = frappe.db.get_value(
                "Replenishment Run", log_name,
                ["error_count", "started_at"], as_dict=True,
            )

            duration_sec = None
            if row and row.started_at:
                try:
                    started = row.started_at
                    if isinstance(started, str):
                        started = datetime.fromisoformat(started)
                    duration_sec = round(
                        (datetime.now() - started).total_seconds(), 1
                    )
                except Exception:
                    pass

            update_fields = {
                "status":           "Forecast Complete",
                "completed_at":     now_datetime(),
                "total_stores":     int(counts.total or 0),
                "processed_stores": int(counts.done or 0),
            }
            if duration_sec is not None:
                update_fields["total_duration_seconds"] = duration_sec

            frappe.db.set_value("Replenishment Run", log_name, update_fields)

            frappe.logger().info(
                f"[AR Scheduler] Run {log_name} forecast complete: "
                f"{counts.done}/{counts.total} stores, {counts.failed} failed"
            )

            # ── Trigger auto-allocation if enabled in config ──────────────
            _maybe_trigger_auto_allocation(log_name)


        frappe.db.commit()

    except Exception as e:
        frappe.logger().warning(
            f"[AR] _increment_processed failed for log {log_name}: {e}"
        )

def _maybe_trigger_auto_allocation(run_name: str):
    """
    Called when all store jobs complete.
    If enable_auto_allocation is set in config, enqueue allocation job.
    """
    if not run_name or run_name == "manual":
        return
    try:
        config_doc = frappe.get_single("Replenishment Config")
        if not getattr(config_doc, "enable_auto_allocation", 0):
            return

        frappe.logger().info(
            f"[AR Scheduler] Auto-allocation triggered for run {run_name}"
        )

        # Update run status
        frappe.db.set_value("Replenishment Run", run_name, {
            "status": "Forecast Complete",
        })
        frappe.db.commit()

        # Enqueue allocation job
        frappe.enqueue(
            "auto_replenishment.tasks.scheduler.run_allocation_job",
            queue="long",
            timeout=3600,
            is_async=True,
            run_name=run_name,
        )

    except Exception as e:
        frappe.logger().warning(
            f"[AR] Auto-allocation trigger failed for {run_name}: {e}"
        )


def run_allocation_job(run_name: str):
    """
    Background job: run cross-store allocation for a Replenishment Run.
    Called by:
      - scheduler auto-allocation (after all store jobs complete)
      - manual Run Allocation button on the Run form
    Updates Run status throughout so the UI shows live progress.
    """
    try:
        from auto_replenishment.utils.allocation_engine import run_allocation

        # Mark all Store Plans as "Allocating" so their forms show progress
        store_plans = frappe.get_all(
            "Replenishment Store Plan",
            filters={"replenishment_run": run_name},
            fields=["name"]
        )
        for p in store_plans:
            frappe.db.set_value(
                "Replenishment Store Plan", p.name,
                "allocation_status", "Pending"
            )

        frappe.db.set_value("Replenishment Run", run_name, {
            "status":            "Allocating",
            "allocation_status": "Running",
        })
        frappe.db.commit()

        summary = run_allocation(run_name)

        frappe.db.set_value("Replenishment Run", run_name, {
            "status":            "Allocation Complete",
            "allocation_status": "Complete",
        })
        frappe.db.commit()

        frappe.logger().info(
            f"[AR] Allocation complete for {run_name}: "
            f"full={summary['full_supply']} partial={summary['partial_supply']} "
            f"no_supply={summary['no_supply']}"
        )

    except Exception as e:
        frappe.db.set_value("Replenishment Run", run_name, {
            "status":            "Forecast Complete",
            "allocation_status": "Failed",
        })
        frappe.db.commit()
        frappe.log_error(str(e)[:200], "AR Allocation Error")


def create_transfer_mrs_job(run_name: str):
    """
    Background job: create Transfer MRs for all stores in a Replenishment Run.
    Creates one MR per source_warehouse → store pair.
    Updates Run and each Store Plan status.
    """
    try:
        frappe.db.set_value("Replenishment Run", run_name, "status", "Creating MRs")
        frappe.db.commit()

        from auto_replenishment.utils.mr_creator import create_transfer_mrs

        # Get all store plans and process one at a time so each plan updates
        plans = frappe.get_all(
            "Replenishment Store Plan",
            filters={"replenishment_run": run_name},
            fields=["name", "warehouse"]
        )

        total_mrs = 0
        for plan in plans:
            try:
                result = create_transfer_mrs(
                    plan.name, "Replenishment Store Plan"
                )
                mr_count = result.get("mr_count", 0)
                total_mrs += mr_count

                # Update this Store Plan status
                frappe.db.set_value(
                    "Replenishment Store Plan", plan.name,
                    "status", "Material Requests Created"
                )
                frappe.db.commit()
                frappe.logger().info(
                    f"[AR] {plan.warehouse}: {mr_count} Transfer MRs created"
                )
            except Exception as e:
                frappe.log_error(
                    f"[AR] Transfer MR failed for {plan.name}: {e}",
                    "AR Transfer MR Error"
                )

        frappe.db.set_value("Replenishment Run", run_name, {
            "status":           "Material Requests Created",
            "last_mr_creation": frappe.utils.now_datetime(),
            "created_transfer_mr_count": total_mrs,
        })
        frappe.db.commit()
        frappe.logger().info(
            f"[AR] Transfer MRs complete for {run_name}: {total_mrs} total MRs"
        )

    except Exception as e:
        frappe.db.set_value("Replenishment Run", run_name, "status", "Submitted")
        frappe.db.commit()
        frappe.log_error(str(e)[:200], "AR Transfer MR Job Error")


def create_purchase_mrs_job(run_name: str):
    """
    Background job: create Purchase MRs for shortage items in a Replenishment Run.
    """
    try:
        from auto_replenishment.utils.mr_creator import create_purchase_mrs
        result = create_purchase_mrs(run_name, "Replenishment Run")

        frappe.db.set_value("Replenishment Run", run_name, {
            "last_mr_creation":           frappe.utils.now_datetime(),
            "created_purchase_mr_count":  result.get("mr_count", 0),
        })
        frappe.db.commit()

        frappe.logger().info(
            f"[AR] Purchase MRs created for {run_name}: "
            f"{result.get('mr_count', 0)} MRs"
        )

    except Exception as e:
        frappe.log_error(str(e)[:200], "AR Purchase MR Error")
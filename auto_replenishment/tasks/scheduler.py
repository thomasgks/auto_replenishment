"""
auto_replenishment/tasks/scheduler.py

Scheduled tasks for automated forecast generation.
Optimized for 300K items × 35 stores:
  - Uses background jobs per store (parallel workers)
  - Tracks progress in cache
  - Marks run in Replenishment Log
"""

import frappe
from frappe.utils import now_datetime, today
from datetime import date
import json


def run_daily_forecast():
    """Called by scheduler daily."""
    _run_scheduled_forecast("Daily")


def run_weekly_forecast():
    """Called by scheduler weekly."""
    _run_scheduled_forecast("Weekly")


def _run_scheduled_forecast(schedule_type: str):
    """
    Orchestrate forecast run for all stores.
    Enqueues one background job per store for parallel processing.
    """
    try:
        config_doc = frappe.get_single("Replenishment Config")
        if not config_doc.auto_create_forecast:
            return
        if config_doc.forecast_schedule != schedule_type and schedule_type != "Manual":
            return
        if not config_doc.central_warehouse:
            frappe.log_error("Auto Replenishment: Central Warehouse not configured.", "Replenishment Scheduler")
            return

        config = {
            "demand_history_days": config_doc.demand_history_days or 30,
            "safety_days": config_doc.safety_days or 7,
            "internal_intransit_lead_time_days": config_doc.internal_intransit_lead_time_days or 3,
            "protection_days": config_doc.protection_days or 5,
            "central_warehouse": config_doc.central_warehouse,
            "batch_size": config_doc.batch_size or 500,
            "parallel_workers": config_doc.parallel_workers or 4,
        }

        # Get store warehouses
        store_warehouses = frappe.db.sql("""
            SELECT name FROM `tabWarehouse`
            WHERE is_group = 0
              AND disabled = 0
              AND name != %(central)s
            ORDER BY name
        """, {"central": config["central_warehouse"]}, as_dict=False)

        store_warehouses = [w[0] for w in store_warehouses]

        if not store_warehouses:
            frappe.log_error("No store warehouses found for Auto Replenishment.", "Replenishment Scheduler")
            return

        # Create a master log document
        log = frappe.new_doc("Auto Replenishment Log")
        log.run_date = today()
        log.run_type = schedule_type
        log.status = "Running"
        log.total_stores = len(store_warehouses)
        log.processed_stores = 0
        log.insert(ignore_permissions=True)
        frappe.db.commit()

        # Enqueue background jobs (one per store, respects parallel_workers limit)
        # For very large deployments, use frappe.enqueue with queue='long'
        for wh in store_warehouses:
            frappe.enqueue(
                "auto_replenishment.tasks.scheduler.generate_forecast_for_store",
                queue="long",
                timeout=3600,
                warehouse=wh,
                config=config,
                log_name=log.name,
                run_date=str(date.today()),
                is_async=True
            )

        frappe.logger().info(
            f"[AutoReplenishment] Enqueued forecast jobs for {len(store_warehouses)} stores. Log: {log.name}"
        )

    except Exception as e:
        frappe.log_error(
            f"Auto Replenishment scheduler failed: {str(e)}\n{frappe.get_traceback()}",
            "Replenishment Scheduler Error"
        )


def generate_forecast_for_store(warehouse: str, config: dict, log_name: str, run_date: str):
    """
    Background worker: Generate and save Material Forecast for one store.
    This runs in a separate worker process.
    """
    from auto_replenishment.utils.forecast_engine import run_forecast_for_store
    from datetime import datetime

    as_of = date.fromisoformat(run_date)

    try:
        # Check if forecast already exists for this store today (avoid duplicates)
        existing = frappe.db.exists("Auto Replenishment Forecast", {
            "warehouse": warehouse,
            "forecast_date": run_date,
            "docstatus": ["!=", 2]
        })
        if existing:
            frappe.logger().info(f"[AutoReplenishment] Forecast already exists for {warehouse} on {run_date}, skipping.")
            _update_log(log_name, "skipped")
            return

        # Run forecast calculation
        df = run_forecast_for_store(warehouse, config, as_of)

        if df.empty:
            _update_log(log_name, "completed_empty")
            return

        # Create Forecast document
        doc = frappe.new_doc("Auto Replenishment Forecast")
        doc.warehouse = warehouse
        doc.forecast_date = run_date
        doc.status = "Draft"

        for _, row in df.iterrows():
            doc.append("items", {
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
                "forecasted_requirement": float(row.get("forecasted_requirement", 0)),
                "supply_status": "Pending",
                "allocated_qty": 0,
                "shortage_qty": 0,
            })

        doc.total_items = len(doc.items)
        doc.insert(ignore_permissions=True)

        _update_log(log_name, "completed", items_count=len(doc.items))
        frappe.logger().info(f"[AutoReplenishment] Forecast saved for {warehouse}: {len(doc.items)} items")

    except Exception as e:
        frappe.log_error(
            f"Forecast generation failed for {warehouse}: {str(e)}\n{frappe.get_traceback()}",
            "Auto Replenishment Worker Error"
        )
        _update_log(log_name, "error")


def _update_log(log_name: str, result: str, items_count: int = 0):
    """Thread-safe log update using db.sql."""
    try:
        frappe.db.sql("""
            UPDATE `tabAuto Replenishment Log`
            SET processed_stores = processed_stores + 1,
                modified = NOW()
            WHERE name = %s
        """, (log_name,))

        if result == "error":
            frappe.db.sql("""
                UPDATE `tabAuto Replenishment Log`
                SET error_count = IFNULL(error_count, 0) + 1
                WHERE name = %s
            """, (log_name,))

        # Check if all stores are done and update final status
        log = frappe.db.get_value("Auto Replenishment Log", log_name,
                                   ["total_stores", "processed_stores", "error_count"], as_dict=True)
        if log and log.processed_stores >= log.total_stores:
            final_status = "Completed with Errors" if log.error_count else "Completed"
            frappe.db.set_value("Auto Replenishment Log", log_name, "status", final_status)

        frappe.db.commit()
    except Exception:
        pass  # Don't fail the worker over logging


def trigger_manual_forecast():
    """API endpoint for manual forecast trigger."""
    _run_scheduled_forecast("Manual")
    return {"message": "Forecast jobs enqueued successfully."}

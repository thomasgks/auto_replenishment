"""
auto_replenishment/utils/allocator_agent.py

Allocator Agent — creates Material Requests in ERPNext.
Called when the Allocator clicks "Create Material Requests" on a forecast.

Logic:
  1. Re-read live stock data (real-time snapshot)
  2. Try Central Warehouse first
  3. If gap remains → evaluate donor stores (DOS fairness check)
  4. Create one Material Request per supply source
  5. Mark items as Full / Partial / No Supply
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today
from datetime import date
import json

from auto_replenishment.utils.forecast_engine import (
    evaluate_donor_stores,
    _get_single_item_onhand,
    _get_single_item_intransit,
    _get_single_item_sales,
)


def create_material_requests_for_forecast(forecast_name: str, override_qtys: dict = None) -> dict:
    """
    Main entry point called by the "Create Material Requests" button.

    Args:
        forecast_name: Name of Auto Replenishment Forecast document
        override_qtys: Optional {item_code: qty} dict if Allocator has overridden quantities

    Returns:
        {
            "created_mrs": [list of MR names],
            "summary": {item_code: {status, qty_allocated, mrs}},
            "partial_items": [item_codes],
            "no_supply_items": [item_codes]
        }
    """
    forecast_doc = frappe.get_doc("Auto Replenishment Forecast", forecast_name)
    config = _get_config()
    as_of = date.today()

    created_mrs = []
    item_summaries = {}
    partial_items = []
    no_supply_items = []

    # Group MR items by source warehouse to batch into single MR per source
    # {source_warehouse: [(item_code, qty, uom, forecast_item_name)]}
    mr_batches = {}

    for fi in forecast_doc.items:
        item_code = fi.item_code
        required_qty = override_qtys.get(item_code, fi.forecasted_requirement) if override_qtys else fi.forecasted_requirement

        if required_qty <= 0:
            continue

        # ── Real-time recalculation ─────────────────────────────────────────
        live_onhand = _get_single_item_onhand(item_code, forecast_doc.warehouse)
        live_intransit = _get_single_item_intransit(
            item_code, forecast_doc.warehouse, as_of,
            config.get("internal_intransit_lead_time_days", 3)
        )
        live_effective = live_onhand + live_intransit

        # Recalculate requirement based on live data
        live_target = fi.target_stock  # Use original target (doesn't change)
        live_requirement = max(0, live_target - live_effective)

        if live_requirement <= 0:
            fi.db_set("supply_status", "No Longer Required")
            continue

        gap = live_requirement
        allocated_sources = []  # [(source_wh, qty)]

        # ── Step 1: Check Central Warehouse ────────────────────────────────
        wh_available = _get_warehouse_available_qty(
            item_code, config["central_warehouse"]
        )

        if wh_available > 0:
            wh_alloc = min(wh_available, gap)
            allocated_sources.append((config["central_warehouse"], wh_alloc))
            gap -= wh_alloc

        # ── Step 2: Donor Store Allocation (if gap remains) ────────────────
        if gap > 0:
            donors = evaluate_donor_stores(
                item_code, forecast_doc.warehouse, gap, config, as_of
            )

            for donor in donors:
                if gap <= 0:
                    break
                if not donor["fairness_pass"]:
                    continue
                take_qty = min(donor["transferable_qty"], gap)
                if take_qty > 0:
                    allocated_sources.append((donor["warehouse"], take_qty))
                    gap -= take_qty

        # ── Determine supply status ─────────────────────────────────────────
        total_allocated = sum(q for _, q in allocated_sources)

        if total_allocated <= 0:
            supply_status = "No Supply"
            no_supply_items.append(item_code)
        elif total_allocated < live_requirement * 0.999:  # 0.1% tolerance
            supply_status = "Partial Supply"
            partial_items.append(item_code)
        else:
            supply_status = "Full Supply"

        # ── Queue items into MR batches (one MR per source per forecast) ────
        for source_wh, alloc_qty in allocated_sources:
            if source_wh not in mr_batches:
                mr_batches[source_wh] = []
            mr_batches[source_wh].append({
                "item_code": item_code,
                "qty": alloc_qty,
                "uom": fi.uom,
                "forecast_item": fi.name
            })

        # Update forecast item
        fi.db_set("supply_status", supply_status)
        fi.db_set("allocated_qty", total_allocated)
        fi.db_set("shortage_qty", max(0, live_requirement - total_allocated))

        item_summaries[item_code] = {
            "status": supply_status,
            "required": live_requirement,
            "allocated": total_allocated,
            "shortage": max(0, live_requirement - total_allocated),
            "sources": [{"warehouse": w, "qty": q} for w, q in allocated_sources]
        }

    # ── Step 3: Create one Material Request per source warehouse ───────────
    for source_wh, items in mr_batches.items():
        mr_name = _create_material_request(
            source_warehouse=source_wh,
            target_warehouse=forecast_doc.warehouse,
            items=items,
            forecast_name=forecast_name
        )
        created_mrs.append(mr_name)

        # Link MR name back to item summaries
        for item_entry in items:
            if item_entry["item_code"] in item_summaries:
                item_summaries[item_entry["item_code"]].setdefault("mrs", []).append(mr_name)

    # ── Update forecast document status ────────────────────────────────────
    if created_mrs:
        forecast_doc.db_set("status", "Material Requests Created")
        forecast_doc.db_set("last_mr_creation", now_datetime())
        forecast_doc.db_set("created_mr_count", len(created_mrs))

    # Log summary
    frappe.logger().info(
        f"[AutoReplenishment] Forecast {forecast_name}: "
        f"{len(created_mrs)} MRs created, "
        f"{len(partial_items)} partial, "
        f"{len(no_supply_items)} no supply"
    )

    return {
        "created_mrs": created_mrs,
        "summary": item_summaries,
        "partial_items": partial_items,
        "no_supply_items": no_supply_items,
        "mr_count": len(created_mrs)
    }


def _create_material_request(
    source_warehouse: str,
    target_warehouse: str,
    items: list,
    forecast_name: str
) -> str:
    """
    Create a single Material Request in ERPNext.
    Type = 'Material Transfer' for internal moves.
    """
    mr = frappe.new_doc("Material Request")
    mr.material_request_type = "Material Transfer"
    mr.transaction_date = today()
    mr.schedule_date = today()
    mr.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
    mr.custom_auto_replenishment_forecast = forecast_name
    mr.custom_source_warehouse = source_warehouse

    # Set title for easy identification
    source_abbr = source_warehouse.split(" - ")[0] if " - " in source_warehouse else source_warehouse[:20]
    target_abbr = target_warehouse.split(" - ")[0] if " - " in target_warehouse else target_warehouse[:20]
    mr.title = f"AR: {source_abbr} → {target_abbr}"

    for item_entry in items:
        mr.append("items", {
            "item_code": item_entry["item_code"],
            "qty": item_entry["qty"],
            "uom": item_entry["uom"],
            "warehouse": target_warehouse,  # Destination
            "from_warehouse": source_warehouse,
            "custom_forecast_item": item_entry.get("forecast_item"),
        })

    mr.insert(ignore_permissions=True)
    mr.submit()
    return mr.name


def _get_warehouse_available_qty(item_code: str, warehouse: str) -> float:
    """
    Available = actual_qty - reserved_qty (for warehouse).
    Uses Bin table for live data.
    """
    result = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        ["actual_qty", "reserved_qty"],
        as_dict=True
    )
    if not result:
        return 0.0
    return max(0.0, float(result.get("actual_qty", 0)) - float(result.get("reserved_qty", 0)))


def _get_config() -> dict:
    """Load Replenishment Config as dict."""
    try:
        cfg = frappe.get_single("Replenishment Config")
        return {
            "demand_history_days": cfg.demand_history_days or 30,
            "safety_days": cfg.safety_days or 7,
            "internal_intransit_lead_time_days": cfg.internal_intransit_lead_time_days or 3,
            "protection_days": cfg.protection_days or 5,
            "central_warehouse": cfg.central_warehouse,
            "batch_size": cfg.batch_size or 500,
            "parallel_workers": cfg.parallel_workers or 4,
        }
    except Exception:
        frappe.throw(_("Replenishment Config is not set up. Please configure it first."))

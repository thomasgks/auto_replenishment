"""
auto_replenishment/utils/allocator_agent.py

Step 2 — Create Material Requests from the evaluated allocation plan.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, today
from datetime import date


def create_material_requests_for_forecast(forecast_name: str) -> dict:
    """
    Read the allocation plan and create one Material Request per source warehouse.
    Uses override_qty when set, else suggested_qty.
    """
    forecast_doc = frappe.get_doc("Replenishment Store Plan", forecast_name)
    config = _get_config()

    if forecast_doc.evaluation_status not in (
        "Evaluation Complete",
        "Material Requests Created",
    ):
        frappe.throw(
            _("Please run 'Evaluate Allocation' before creating Material Requests.")
        )

    # {source_warehouse: [item_entries]}
    mr_batches: dict = {}
    partial_items = []
    no_supply_items = []

    for fi in forecast_doc.items:
        if float(fi.forecasted_requirement or 0) <= 0:
            continue

        alloc_rows = frappe.get_all(
            "Replenishment Allocation",
            filters={
                "parent": forecast_doc.name,
                "forecast_item_ref": fi.name,
                "excluded": 0,
            },
            fields=["source_warehouse", "suggested_qty", "override_qty", "source_type"],
            order_by="source_type asc, idx asc",
        )

        if not alloc_rows:
            fi.db_set("supply_status", "No Supply")
            no_supply_items.append(fi.item_code)
            continue

        total_allocated = 0
        for row in alloc_rows:
            qty = (
                float(row.override_qty or 0)
                if float(row.override_qty or 0) > 0
                else float(row.suggested_qty or 0)
            )
            if qty <= 0:
                continue
            if row.source_warehouse not in mr_batches:
                mr_batches[row.source_warehouse] = []
            mr_batches[row.source_warehouse].append(
                {
                    "item_code": fi.item_code,
                    "qty": qty,
                    "uom": fi.uom or "Nos",
                    "forecast_item": fi.name,
                    "target_warehouse": forecast_doc.warehouse,
                }
            )
            total_allocated += qty

        required = float(fi.forecasted_requirement or 0)
        if total_allocated <= 0:
            fi.db_set("supply_status", "No Supply")
            no_supply_items.append(fi.item_code)
        elif total_allocated < required * 0.999:
            fi.db_set("supply_status", "Partial Supply")
            partial_items.append(fi.item_code)
        else:
            fi.db_set("supply_status", "Full Supply")

        fi.db_set("allocated_qty", total_allocated)
        fi.db_set("shortage_qty", max(0, required - total_allocated))

    created_mrs = []
    for source_wh, items in mr_batches.items():
        mr_name = _create_material_request(
            source_warehouse=source_wh,
            target_warehouse=forecast_doc.warehouse,
            items=items,
            forecast_name=forecast_name,
        )
        if mr_name:
            created_mrs.append(mr_name)

    frappe.db.commit()

    frappe.logger().info(
        f"[AR] {forecast_name}: {len(created_mrs)} MRs, "
        f"{len(partial_items)} partial, {len(no_supply_items)} no supply"
    )

    return {
        "created_mrs": created_mrs,
        "mr_count": len(created_mrs),
        "partial_items": partial_items,
        "no_supply_items": no_supply_items,
    }


def _create_material_request(
    source_warehouse: str, target_warehouse: str, items: list, forecast_name: str
) -> str:
    """
    Create and SAVE (docstatus=0, Draft) a Material Request.

    Key fields for Material Transfer in ERPNext v15:
      - material_request_type = "Material Transfer"
      - set_from_warehouse    = source (header level — shown in list view)
      - set_warehouse         = destination store (header level)
      - items[].from_warehouse = source (item level)
      - items[].warehouse      = destination (item level)

    We save as Draft (not submitted) so the warehouse team can review
    and process it. The forecast tracks the link.
    """
    if not items:
        return None

    company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )
    if not company:
        frappe.throw(_("Default Company not set. Please set it in Global Defaults."))

    src_abbr = (
        source_warehouse.split(" - ")[0]
        if " - " in source_warehouse
        else source_warehouse[:20]
    )
    tgt_abbr = (
        target_warehouse.split(" - ")[0]
        if " - " in target_warehouse
        else target_warehouse[:20]
    )

    mr = frappe.new_doc("Material Request")
    mr.material_request_type = "Material Transfer"
    mr.transaction_date = today()
    mr.schedule_date = today()
    mr.company = company
    mr.title = f"AR: {src_abbr} → {tgt_abbr}"

    # Header-level warehouse fields — these make the MR appear correctly
    # in the Material Request list and filter views
    mr.set_from_warehouse = source_warehouse  # source (for Material Transfer)
    mr.set_warehouse = target_warehouse  # destination store

    # Custom tracking fields
    mr.custom_auto_replenishment_forecast = forecast_name
    mr.custom_source_warehouse = source_warehouse

    for item_entry in items:
        mr.append(
            "items",
            {
                "item_code": item_entry["item_code"],
                "qty": item_entry["qty"],
                "uom": item_entry.get("uom", "Nos"),
                "warehouse": item_entry.get("target_warehouse", target_warehouse),
                "from_warehouse": source_warehouse,
                "custom_forecast_item": item_entry.get("forecast_item", ""),
            },
        )

    try:
        mr.insert(ignore_permissions=True)
        # Save as Draft — warehouse team reviews and submits themselves
        frappe.logger().info(
            f"[AR] Created MR {mr.name}: {source_warehouse} → {target_warehouse}"
        )
        return mr.name
    except Exception as e:
        frappe.log_error(
            f"Failed to create MR for {source_warehouse} → {target_warehouse}: {e}\n"
            f"{frappe.get_traceback()}",
            "AR MR Creation Error",
        )
        frappe.throw(_("Failed to create Material Request: {0}").format(str(e)))


def _get_config() -> dict:
    try:
        cfg = frappe.get_single("Replenishment Config")
        return {
            "demand_history_days": cfg.demand_history_days or 30,
            "safety_days": cfg.safety_days or 7,
            "internal_intransit_lead_time_days": cfg.internal_intransit_lead_time_days
            or 3,
            "protection_days": cfg.protection_days or 5,
            "central_warehouse": cfg.central_warehouse,
            "quantity_rounding": cfg.quantity_rounding or "Ceil (Round Up)",
        }
    except Exception:
        frappe.throw(
            _("Replenishment Config is not set up. Please configure it first.")
        )

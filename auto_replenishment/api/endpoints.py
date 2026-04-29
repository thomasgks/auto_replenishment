"""
auto_replenishment/api/endpoints.py

Whitelisted API endpoints for:
  - Creating Material Requests from forecast
  - Triggering manual forecast
  - Getting live donor analysis
  - Supply status reports
"""

import frappe
from frappe import _
from frappe.utils import today
import json


@frappe.whitelist()
def create_material_requests(forecast_name: str, override_qtys: str = None):
    """
    Button handler: Create Material Requests for a forecast.
    Called from the Forecast form's 'Create Material Requests' button.
    """
    from auto_replenishment.utils.allocator_agent import create_material_requests_for_forecast

    if not frappe.has_permission("Auto Replenishment Forecast", "write"):
        frappe.throw(_("Insufficient permissions."), frappe.PermissionError)

    parsed_overrides = {}
    if override_qtys:
        try:
            parsed_overrides = json.loads(override_qtys)
        except Exception:
            frappe.throw(_("Invalid override quantities format."))

    result = create_material_requests_for_forecast(forecast_name, parsed_overrides)
    return result


@frappe.whitelist()
def trigger_manual_forecast():
    """Trigger a manual forecast run for all stores."""
    if not frappe.has_permission("Auto Replenishment Forecast", "create"):
        frappe.throw(_("Insufficient permissions."), frappe.PermissionError)

    from auto_replenishment.tasks.scheduler import trigger_manual_forecast as _trigger
    return _trigger()


@frappe.whitelist()
def generate_forecast_single_store(warehouse: str):
    """Generate forecast for a single store on demand."""
    if not frappe.has_permission("Auto Replenishment Forecast", "create"):
        frappe.throw(_("Insufficient permissions."), frappe.PermissionError)

    from auto_replenishment.tasks.scheduler import generate_forecast_for_store

    config_doc = frappe.get_single("Replenishment Config")
    config = {
        "demand_history_days": config_doc.demand_history_days or 30,
        "safety_days": config_doc.safety_days or 7,
        "internal_intransit_lead_time_days": config_doc.internal_intransit_lead_time_days or 3,
        "protection_days": config_doc.protection_days or 5,
        "central_warehouse": config_doc.central_warehouse,
        "batch_size": config_doc.batch_size or 500,
    }

    frappe.enqueue(
        "auto_replenishment.tasks.scheduler.generate_forecast_for_store",
        queue="long",
        timeout=3600,
        warehouse=warehouse,
        config=config,
        log_name="manual",
        run_date=today(),
        is_async=True
    )

    return {"message": f"Forecast job enqueued for {warehouse}"}


@frappe.whitelist()
def get_donor_analysis(forecast_name: str, item_code: str):
    """
    Get live donor store analysis for a specific item.
    Used in the Forecast form to show the Allocator which donors are available.
    """
    from auto_replenishment.utils.forecast_engine import evaluate_donor_stores
    from datetime import date

    doc = frappe.get_doc("Auto Replenishment Forecast", forecast_name)
    config_doc = frappe.get_single("Replenishment Config")
    config = {
        "demand_history_days": config_doc.demand_history_days or 30,
        "safety_days": config_doc.safety_days or 7,
        "internal_intransit_lead_time_days": config_doc.internal_intransit_lead_time_days or 3,
        "protection_days": config_doc.protection_days or 5,
        "central_warehouse": config_doc.central_warehouse,
    }

    # Get the forecasted requirement for this item
    gap = 0
    for fi in doc.items:
        if fi.item_code == item_code:
            gap = fi.forecasted_requirement
            break

    if gap <= 0:
        return {"donors": [], "message": "No requirement for this item."}

    donors = evaluate_donor_stores(item_code, doc.warehouse, gap, config, date.today())
    return {
        "donors": donors,
        "gap": gap,
        "item_code": item_code
    }


@frappe.whitelist()
def get_supply_status_report(from_date: str = None, to_date: str = None):
    """
    Get a summary report of supply statuses across all recent forecasts.
    Shows partial and no-supply items for management review.
    """
    if not from_date:
        from_date = frappe.utils.add_days(today(), -7)
    if not to_date:
        to_date = today()

    rows = frappe.db.sql("""
        SELECT
            f.warehouse,
            f.forecast_date,
            fi.item_code,
            fi.item_name,
            fi.forecasted_requirement,
            fi.allocated_qty,
            fi.shortage_qty,
            fi.supply_status
        FROM `tabAuto Replenishment Forecast` f
        JOIN `tabAuto Replenishment Forecast Item` fi ON fi.parent = f.name
        WHERE f.forecast_date BETWEEN %(from_date)s AND %(to_date)s
          AND fi.supply_status IN ('Partial Supply', 'No Supply')
          AND f.docstatus != 2
        ORDER BY f.forecast_date DESC, fi.supply_status, f.warehouse
    """, {"from_date": from_date, "to_date": to_date}, as_dict=True)

    return rows


@frappe.whitelist()
def get_forecast_dashboard_data():
    """Dashboard summary data for the Auto Replenishment module."""
    data = frappe.db.sql("""
        SELECT
            COUNT(DISTINCT f.name) AS total_forecasts,
            SUM(CASE WHEN fi.supply_status = 'Full Supply' THEN 1 ELSE 0 END) AS full_supply,
            SUM(CASE WHEN fi.supply_status = 'Partial Supply' THEN 1 ELSE 0 END) AS partial_supply,
            SUM(CASE WHEN fi.supply_status = 'No Supply' THEN 1 ELSE 0 END) AS no_supply,
            SUM(CASE WHEN fi.supply_status = 'Pending' THEN 1 ELSE 0 END) AS pending
        FROM `tabAuto Replenishment Forecast` f
        JOIN `tabAuto Replenishment Forecast Item` fi ON fi.parent = f.name
        WHERE f.forecast_date = %(today)s
          AND f.docstatus != 2
    """, {"today": today()}, as_dict=True)

    return data[0] if data else {}

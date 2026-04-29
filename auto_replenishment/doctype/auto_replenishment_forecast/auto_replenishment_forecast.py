"""
auto_replenishment/doctype/auto_replenishment_forecast/auto_replenishment_forecast.py

Main Forecast DocType controller.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, today
from datetime import date
import json


class AutoReplenishmentForecast(Document):

    def validate(self):
        if not self.warehouse:
            frappe.throw(_("Warehouse is required."))
        if not self.forecast_date:
            self.forecast_date = today()

    def before_submit(self):
        self.status = "Submitted"

    # ── Custom button actions ───────────────────────────────────────────────

    @frappe.whitelist()
    def create_material_requests(self, override_qtys=None):
        """Called by the 'Create Material Requests' button on the form."""
        from auto_replenishment.utils.allocator_agent import create_material_requests_for_forecast

        if self.status not in ("Draft", "Submitted", "Material Requests Created"):
            frappe.throw(_("Cannot create Material Requests for a forecast with status: {0}").format(self.status))

        if isinstance(override_qtys, str):
            override_qtys = json.loads(override_qtys)

        result = create_material_requests_for_forecast(self.name, override_qtys or {})

        return result

    @frappe.whitelist()
    def recalculate_forecast(self):
        """Recalculate forecast quantities using live data."""
        from auto_replenishment.utils.forecast_engine import run_forecast_for_store

        config = _get_replenishment_config()
        df = run_forecast_for_store(self.warehouse, config, date.today())

        if df.empty:
            frappe.msgprint(_("No items require replenishment for this store at this time."))
            return

        # Rebuild items child table
        self.items = []
        for _, row in df.iterrows():
            self.append("items", {
                "item_code": row["item_code"],
                "item_name": row.get("item_name", ""),
                "uom": row.get("uom", "Nos"),
                "selling_rate": row["selling_rate"],
                "lead_time_days": row["lead_time_days"],
                "safety_days": row["safety_days"],
                "lead_time_demand": row["lead_time_demand"],
                "safety_stock": row["safety_stock"],
                "target_stock": row["target_stock"],
                "current_onhand": row["current_onhand"],
                "usable_intransit": row["usable_intransit"],
                "effective_onhand": row["effective_onhand"],
                "forecasted_requirement": row["forecasted_requirement"],
                "supply_status": "Pending",
                "allocated_qty": 0,
                "shortage_qty": 0,
            })

        self.total_items = len(self.items)
        self.last_recalculated = now_datetime()
        self.save()
        frappe.msgprint(_("Forecast recalculated. {0} items require replenishment.").format(len(self.items)))


def has_permission(doc, ptype, user):
    """Custom permission check."""
    if user == "Administrator":
        return True
    if frappe.has_role("Stock Manager", user) or frappe.has_role("Stock User", user):
        return True
    return False


def _get_replenishment_config() -> dict:
    cfg = frappe.get_single("Replenishment Config")
    return {
        "demand_history_days": cfg.demand_history_days or 30,
        "safety_days": cfg.safety_days or 7,
        "internal_intransit_lead_time_days": cfg.internal_intransit_lead_time_days or 3,
        "protection_days": cfg.protection_days or 5,
        "central_warehouse": cfg.central_warehouse,
        "batch_size": cfg.batch_size or 500,
    }

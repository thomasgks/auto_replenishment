import frappe
from frappe.model.document import Document


class ReplenishmentConfig(Document):
    def validate(self):
        if self.demand_history_days < 7:
            frappe.throw("Demand History Days must be at least 7.")
        if self.safety_days < 0:
            frappe.throw("Safety Days cannot be negative.")
        if self.protection_days < 0:
            frappe.throw("Protection Days cannot be negative.")
        if self.internal_intransit_lead_time_days < 0:
            frappe.throw("Internal In-Transit Lead Time cannot be negative.")
        if self.batch_size < 100:
            frappe.throw("Batch size must be at least 100 for performance.")
        if not self.quantity_rounding:
            self.quantity_rounding = "Ceil (Round Up)"

"""
auto_replenishment/doctype/replenishment_store_plan/replenishment_store_plan.py
"""
import frappe
from frappe import _
from frappe.model.document import Document


class ReplenishmentStorePlan(Document):

    def before_submit(self):
        alloc_ok = self.allocation_status in ("Received", "Evaluation Complete", "Complete")
        eval_ok  = self.evaluation_status in ("Evaluation Complete",)
        if not alloc_ok and not eval_ok:
            frappe.throw(
                _("Please run 'Run Allocation' on the Replenishment Run before submitting.")
            )
        self.status = "Submitted"

    def on_submit(self):
        self.status = "Submitted"

    # ── Cancel ────────────────────────────────────────────────────────────────

    def before_cancel(self):
        """
        Block cancel if any linked MR is Submitted.
        Set ignore_linked_doctypes so Frappe skips the Replenishment Run
        link check — the back-link is kept intentionally for batch Delete Run.
        """
        submitted_mrs = frappe.get_all(
            "Material Request",
            filters={
                "custom_auto_replenishment_forecast": self.name,
                "docstatus": 1,
            },
            fields=["name"],
            limit=1,
        )
        if submitted_mrs:
            frappe.throw(
                _("Cannot cancel: Material Request {0} is still submitted. "
                  "Cancel the MR first.").format(submitted_mrs[0].name)
            )
        # Tell Frappe's check_if_doc_is_linked to ignore the Run back-link.
        # This list is read by frappe/model/delete_doc.py before raising LinkExistsError.
        self.ignore_linked_doctypes = ["Replenishment Run"]

    def on_cancel(self):
        self.status = "Draft"
        _delete_linked_mrs(self.name)

    @frappe.whitelist()
    def cancel_plan(self, *args, **kwargs):
        """
        Cancel a Store Plan without Frappe's link check blocking on replenishment_run.
        Uses raw SQL — same pattern as cancel_run on Replenishment Run.
        The replenishment_run link is preserved so Delete Run can batch-delete plans.
        """
        if self.docstatus != 1:
            frappe.throw(_("Only submitted plans can be cancelled."))

        # Block if any MR is still submitted
        submitted_mrs = frappe.get_all(
            "Material Request",
            filters={
                "custom_auto_replenishment_forecast": self.name,
                "docstatus": 1,
            },
            fields=["name"],
            limit=1,
        )
        if submitted_mrs:
            frappe.throw(
                _("Cannot cancel: Material Request {0} is still submitted. "
                  "Cancel the MR first.").format(submitted_mrs[0].name)
            )

        plan_name = self.name

        # Step 1: Delete linked MRs
        _delete_linked_mrs(plan_name)

        # Step 2: Cancel via raw SQL — bypasses Frappe link check entirely
        # replenishment_run is intentionally kept for batch Delete Run
        frappe.db.sql(
            "UPDATE `tabReplenishment Store Plan` "
            "SET docstatus=2, status='Draft', modified=NOW() "
            "WHERE name = %s",
            plan_name
        )
        frappe.db.commit()

        return {"cancelled": True, "message": f"Plan {plan_name} cancelled."}

    # ── Delete ────────────────────────────────────────────────────────────────

    def before_delete(self):
        """Clear MR back-links and the Run back-link before Frappe link check."""
        # Clear Run back-link
        frappe.db.sql(
            "UPDATE `tabReplenishment Store Plan` "
            "SET replenishment_run = '' WHERE name = %s",
            self.name
        )
        # Clear MR back-links
        frappe.db.sql(
            "UPDATE `tabMaterial Request` "
            "SET custom_auto_replenishment_forecast = '' "
            "WHERE custom_auto_replenishment_forecast = %s",
            self.name
        )
        frappe.db.commit()

    def on_trash(self):
        _delete_linked_mrs(self.name)
        frappe.db.sql(
            "DELETE FROM `tabReplenishment Store Plan Item` WHERE parent = %s", self.name
        )
        frappe.db.sql(
            "DELETE FROM `tabReplenishment Allocation` WHERE parent = %s", self.name
        )
        frappe.db.sql(
            "DELETE FROM `tabReplenishment MR Link` WHERE parent = %s", self.name
        )
        frappe.db.commit()

    # ── Whitelisted methods ───────────────────────────────────────────────────

    @frappe.whitelist()
    def create_transfer_mrs(self, *args, **kwargs):
        if self.docstatus != 1:
            frappe.throw(_("Submit the plan before creating Material Requests."))
        from auto_replenishment.utils.mr_creator import create_transfer_mrs
        return create_transfer_mrs(self.name, "Replenishment Store Plan")

    @frappe.whitelist()
    def create_purchase_mrs(self, *args, **kwargs):
        if self.docstatus != 1:
            frappe.throw(_("Submit the plan before creating Material Requests."))
        from auto_replenishment.utils.mr_creator import create_purchase_mrs
        return create_purchase_mrs(self.name, "Replenishment Store Plan")

    @frappe.whitelist()
    def close_forecast(self, *args, **kwargs):
        if self.docstatus != 1:
            frappe.throw(_("Only submitted plans can be closed."))
        self.db_set("status", "Closed")
        return {"message": "Plan closed."}

    @frappe.whitelist()
    def reopen_forecast(self, *args, **kwargs):
        if self.status != "Closed":
            frappe.throw(_("Only closed plans can be re-opened."))
        self.db_set("status", "Submitted")
        return {"message": "Re-opened."}


# ── MR helpers ────────────────────────────────────────────────────────────────

def _delete_linked_mrs(plan_name: str):
    """Cancel submitted MRs, then delete all Draft/Cancelled MRs linked to plan."""
    mrs = frappe.get_all(
        "Material Request",
        filters={"custom_auto_replenishment_forecast": plan_name},
        fields=["name", "docstatus"],
    )
    if not mrs:
        return

    names = [r.name for r in mrs]
    ph    = ", ".join(["%s"] * len(names))

    # Cancel submitted MRs
    for mr in mrs:
        if mr.docstatus == 1:
            try:
                frappe.get_doc("Material Request", mr.name).cancel()
            except Exception as e:
                frappe.log_error(str(e)[:200], "AR MR Cancel")

    frappe.db.commit()

    # Clear back-links
    frappe.db.sql(
        f"UPDATE `tabMaterial Request` "
        f"SET custom_auto_replenishment_forecast = '' "
        f"WHERE name IN ({ph})",
        names
    )
    frappe.db.commit()

    # Delete MR items + MRs
    frappe.db.sql(
        f"DELETE FROM `tabMaterial Request Item` WHERE parent IN ({ph})", names
    )
    frappe.db.sql(
        f"DELETE FROM `tabMaterial Request` WHERE name IN ({ph})", names
    )
    frappe.db.sql(
        "DELETE FROM `tabReplenishment MR Link` WHERE parent = %s", plan_name
    )
    frappe.db.commit()


def has_permission(doc, ptype, user):
    """
    Return None to defer to Frappe's standard role-based permission system.
    Add logic here ONLY if you need row-level restrictions.
    """
    return None
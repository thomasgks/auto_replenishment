"""
auto_replenishment/doctype/replenishment_run/replenishment_run.py
"""
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, today


class ReplenishmentRun(Document):

    def before_submit(self):
        if self.allocation_status != "Complete":
            frappe.throw(
                _("Please run 'Run Allocation' before submitting.")
            )
        self.status = "Submitted"

    def on_submit(self):
        """Submit all linked Draft Store Plans when the Run is submitted."""
        plans = frappe.get_all(
            "Replenishment Store Plan",
            filters={"replenishment_run": self.name, "docstatus": 0},
            fields=["name"]
        )
        for p in plans:
            try:
                plan_doc = frappe.get_doc("Replenishment Store Plan", p.name)
                plan_doc.submit()
            except Exception as e:
                frappe.log_error(
                    f"Could not submit Store Plan {p.name}: {e}",
                    "AR Store Plan Submit"
                )
        frappe.db.commit()

    def before_cancel(self):
        """
        Frappe v15 calls check_if_doc_is_linked before before_cancel,
        so we can't clear links here in time.
        Use the cancel_run whitelisted method instead (called from JS).
        """
        pass

    def on_cancel(self):
        self.status = "Draft"

    @frappe.whitelist()
    def cancel_run(self, *args, **kwargs):
        """
        Cancel a Replenishment Run.
        Rules:
        - Block if any linked Store Plan is Submitted (docstatus=1)
        - If all Store Plans are Draft/Cancelled → cancel Run and delete them
        """
        if self.docstatus != 1:
            frappe.throw(_("Only submitted runs can be cancelled."))

        run_name = self.name

        # Check: block if any Store Plan is still Submitted
        submitted = frappe.get_all(
            "Replenishment Store Plan",
            filters={"replenishment_run": run_name, "docstatus": 1},
            fields=["name"],
            limit=1,
        )
        if submitted:
            frappe.throw(
                _("Cannot cancel: Replenishment Store Plan {0} is still submitted. "
                  "Cancel all Store Plans first, or use 'Delete Run' from Actions.")
                .format(submitted[0].name)
            )

        # All Store Plans are Draft/Cancelled — safe to proceed
        # Step 1: Clear back-links via raw SQL before any Frappe check
        frappe.db.sql(
            "UPDATE `tabReplenishment Store Plan` "
            "SET replenishment_run = '' WHERE replenishment_run = %s",
            run_name
        )
        frappe.db.sql(
            "DELETE FROM `tabReplenishment Allocation` WHERE parent = %s", run_name
        )
        frappe.db.commit()

        # Step 2: Cancel Run via raw SQL (bypass Frappe link check)
        frappe.db.sql(
            "UPDATE `tabReplenishment Run` "
            "SET docstatus=2, status='Draft', modified=NOW() "
            "WHERE name = %s",
            run_name
        )
        frappe.db.commit()

        # Step 3: Delete Draft/Cancelled Store Plans and their MRs
        _delete_linked_store_plans(run_name)

        # Step 4: Clean up child rows
        frappe.db.sql(
            "DELETE FROM `tabReplenishment Run Store` WHERE parent = %s", run_name
        )
        frappe.db.sql(
            "DELETE FROM `tabReplenishment MR Link` WHERE parent = %s", run_name
        )
        frappe.db.commit()

        return {"cancelled": True, "message": f"Run {run_name} cancelled."}

    def before_delete(self):
        """
        Frappe v15 calls check_if_doc_is_linked BEFORE before_delete.
        So we clear links via raw SQL here — this runs inside the same
        request as the delete, before Frappe's link check re-runs.
        """
        # Clear Store Plan back-links via raw SQL
        frappe.db.sql(
            "UPDATE `tabReplenishment Store Plan` "
            "SET replenishment_run = '' "
            "WHERE replenishment_run = %s",
            self.name
        )
        frappe.db.commit()

    @frappe.whitelist()
    def delete_run(self, *args, **kwargs):
        """
        Delete a Replenishment Run.
        Rules:
        - Block if any linked Store Plan is Submitted (docstatus=1)
        - If all Store Plans are Draft/Cancelled → delete Run and all linked records
        """
        if self.docstatus == 1:
            frappe.throw(_("Cancel the run before deleting."))

        run_name = self.name

        # Check: block if any Store Plan is still Submitted
        submitted = frappe.get_all(
            "Replenishment Store Plan",
            filters={"replenishment_run": run_name, "docstatus": 1},
            fields=["name"],
            limit=1,
        )
        if submitted:
            frappe.throw(
                _("Cannot delete: Replenishment Store Plan {0} is still submitted. "
                  "Cancel all Store Plans first.").format(submitted[0].name)
            )

        # Step 1: Clear back-links
        frappe.db.sql(
            "UPDATE `tabReplenishment Store Plan` "
            "SET replenishment_run = '' WHERE replenishment_run = %s",
            run_name
        )
        frappe.db.commit()

        # Step 2: Delete Draft Store Plans and their MRs
        _delete_linked_store_plans(run_name)

        # Step 3: Delete all child rows on the Run
        for tbl in ["tabReplenishment Allocation", "tabReplenishment Run Store",
                    "tabReplenishment MR Link"]:
            frappe.db.sql(f"DELETE FROM `{tbl}` WHERE parent = %s", run_name)

        frappe.db.commit()

        # Step 4: Delete the Run
        frappe.db.sql(
            "DELETE FROM `tabReplenishment Run` WHERE name = %s", run_name
        )
        frappe.db.commit()

        return {"deleted": True, "message": f"Run {run_name} deleted."}

    @frappe.whitelist()
    def run_forecast(self, *args, **kwargs):
        if self.filters_locked:
            frappe.throw(_("Filters are locked. Use Reset to unlock and re-run."))
        config = _get_config()

        # Add this Run's item filters to config so forecast_engine applies them
        config["filter_suppliers"]      = [r.supplier for r in (self.filter_suppliers or [])
                                           if r.supplier]
        config["filter_item_groups_l4"] = [r.item_group for r in (self.filter_item_groups_l4 or [])
                                           if r.item_group]
        config["filter_item_groups_l5"] = [r.item_group for r in (self.filter_item_groups_l5 or [])
                                           if r.item_group]
                # Year/Season: Data field stores attribute_value directly (e.g. "2025", "SS")
        config["filter_year"]   = [r.attribute_value for r in (self.filter_year   or []) if r.attribute_value]
        config["filter_season"] = [r.attribute_value for r in (self.filter_season or []) if r.attribute_value]

        from auto_replenishment.utils.item_filter_engine import get_store_warehouses
        store_warehouses = get_store_warehouses(self)
        if not store_warehouses:
            frappe.throw(_("No stores found matching the selected filters."))
        self.db_set("filters_locked", 1)
        self.db_set("status", "Forecasting")
        self.db_set("run_type", self.run_type or "Manual")
        frappe.db.delete("Replenishment Run Store", {"parent": self.name})
        for wh in store_warehouses:
            row = frappe.new_doc("Replenishment Run Store")
            row.update({"parent": self.name, "parentfield": "store_logs",
                        "parenttype": "Replenishment Run", "store_warehouse": wh,
                        "status": "Queued", "queued_at": now_datetime()})
            row.db_insert()
        frappe.db.commit()
        self.reload()
        store_row_map = {r.store_warehouse: r.name for r in self.store_logs}
        run_date = str(self.run_date) if self.run_date else today()
        # Override demand_history_days from the run if explicitly set
        if self.demand_history_days:
            config["demand_history_days"] = int(self.demand_history_days)
        for wh in store_warehouses:
            frappe.enqueue(
                "auto_replenishment.tasks.scheduler.generate_forecast_for_store",
                queue="long", timeout=7200, is_async=True,
                warehouse=wh, config=config, log_name=self.name,
                store_row_name=store_row_map.get(wh, ""),
                run_date=run_date, replenishment_run=self.name,
            )
        frappe.db.commit()
        return {"stores_queued": len(store_warehouses)}

    @frappe.whitelist()
    def reset_run(self, *args, **kwargs):
        if self.status in ("Submitted", "Closed"):
            frappe.throw(_("Cannot reset a submitted or closed run."))
        _delete_linked_store_plans(self.name)
        frappe.db.delete("Replenishment Allocation", {"parent": self.name})
        frappe.db.delete("Replenishment Run Store",  {"parent": self.name})
        frappe.db.delete("Replenishment MR Link",    {"parent": self.name})
        frappe.db.set_value("Replenishment Run", self.name, {
            "filters_locked":            0,
            "status":                    "Draft",
            "allocation_status":         "Not Started",
            "full_supply_count":         0,
            "partial_supply_count":      0,
            "no_supply_count":           0,
            "total_stores":              0,
            "processed_stores":          0,
            "error_count":               0,
            "started_at":                None,
            "completed_at":              None,
            "total_duration_seconds":    0,
            "allocation_at":             None,
            "created_transfer_mr_count": 0,
            "created_purchase_mr_count": 0,
            "last_mr_creation":          None,
        })
        frappe.db.commit()
        return {"message": "Run reset. Filters unlocked."}

    @frappe.whitelist()
    def run_allocation(self, *args, **kwargs):
        """Enqueue cross-store allocation as a background job."""
        if self.status not in ("Forecast Complete", "Allocation Complete", "Forecasting"):
            frappe.throw(
                _("Status must be Forecast Complete. Current: {0}").format(self.status)
            )

        # Mark as Allocating so the UI shows the correct status
        frappe.db.set_value("Replenishment Run", self.name, {
            "status":            "Allocating",
            "allocation_status": "Running",
        })
        frappe.db.commit()

        frappe.enqueue(
            "auto_replenishment.tasks.scheduler.run_allocation_job",
            queue="long",
            timeout=7200,
            is_async=True,
            run_name=self.name,
        )

        return {
            "message": "Allocation job enqueued. Page will refresh automatically.",
            "queued":  True,
        }

    @frappe.whitelist()
    def create_transfer_mrs(self, *args, **kwargs):
        if self.docstatus != 1:
            frappe.throw(_("Submit the run before creating Material Requests."))
        frappe.db.set_value("Replenishment Run", self.name, "status", "Creating MRs")
        frappe.db.commit()
        frappe.enqueue(
            "auto_replenishment.tasks.scheduler.create_transfer_mrs_job",
            queue="long", timeout=7200, is_async=True,
            run_name=self.name,
        )
        return {"queued": True, "message": "Transfer MR creation enqueued."}

    @frappe.whitelist()
    def create_purchase_mrs(self, *args, **kwargs):
        if self.docstatus != 1:
            frappe.throw(_("Submit the run before creating Material Requests."))
        config = _get_config()
        if not config.get("create_purchase_mrs"):
            frappe.throw(_("Purchase MR creation is disabled in Replenishment Config."))
        frappe.enqueue(
            "auto_replenishment.tasks.scheduler.create_purchase_mrs_job",
            queue="long", timeout=7200, is_async=True,
            run_name=self.name,
        )
        return {"queued": True, "message": "Purchase MR creation enqueued."}

    @frappe.whitelist()
    def close_run(self, *args, **kwargs):
        if self.docstatus != 1:
            frappe.throw(_("Only submitted runs can be closed."))
        self.db_set("status", "Closed")
        return {"message": "Run closed."}

    @frappe.whitelist()
    def reopen_run(self, *args, **kwargs):
        if self.status != "Closed":
            frappe.throw(_("Only closed runs can be re-opened."))
        if self.docstatus == 1:
            self.cancel()
            amended = frappe.copy_doc(self)
            amended.docstatus = 0
            amended.status = "Draft"
            amended.filters_locked = 0
            amended.allocation_status = "Not Started"
            amended.amended_from = self.name
            amended.material_requests = []
            amended.insert(ignore_permissions=True)
            frappe.db.commit()
            return {"new_doc": amended.name}
        return {"message": "Re-opened."}


@frappe.whitelist()
def get_store_log_content(log_name: str, store_row_name: str,
                          last_line: int = 0, max_lines: int = 500):
    """Read store forecast log file lines. Called by the log viewer UI."""
    if not frappe.has_permission("Replenishment Run", "read", doc=log_name):
        frappe.throw("Insufficient permissions", frappe.PermissionError)
    row = frappe.db.get_value("Replenishment Run Store", store_row_name,
                              ["log_file_path", "status", "store_warehouse"], as_dict=True)
    if not row:
        return {"lines": ["[Error] Row not found."], "total_lines": 1,
                "log_file": "", "status": "Unknown", "store_warehouse": ""}
    log_file = (row.log_file_path or "").strip()
    if not log_file:
        return {"lines": ["[Info] Log not recorded yet."], "total_lines": 1,
                "log_file": "", "status": row.status, "store_warehouse": row.store_warehouse}
    import os
    if not os.path.exists(log_file):
        return {"lines": [f"[Info] File not found: {log_file}"], "total_lines": 1,
                "log_file": log_file, "status": row.status, "store_warehouse": row.store_warehouse}
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        total = len(all_lines)
        start = max(0, int(last_line))
        lines = [ln.rstrip("\n") for ln in all_lines[start:start + int(max_lines)]]
        return {"lines": lines, "total_lines": total, "log_file": log_file,
                "status": row.status, "store_warehouse": row.store_warehouse}
    except Exception as exc:
        return {"lines": [f"[Error] {exc}"], "total_lines": 1,
                "log_file": log_file, "status": row.status, "store_warehouse": row.store_warehouse}


def _unlink_store_plans(run_name: str):
    """
    Clear the replenishment_run back-link on all Store Plans.
    Must be called before deleting the Run to satisfy ERPNext link constraints.
    """
    plans = frappe.get_all(
        "Replenishment Store Plan",
        filters={"replenishment_run": run_name},
        fields=["name"]
    )
    for p in plans:
        try:
            frappe.db.set_value(
                "Replenishment Store Plan", p.name,
                "replenishment_run", ""
            )
        except Exception:
            pass
    frappe.db.commit()


def _delete_linked_store_plans(run_name: str):
    """
    Delete all Draft/Cancelled Store Plans linked to this Run.
    Submitted plans are NOT touched — caller must block if any are submitted.
    """
    plans = frappe.get_all(
        "Replenishment Store Plan",
        filters={"replenishment_run": run_name, "docstatus": ["!=", 1]},
        fields=["name"]
    )
    if not plans:
        return

    plan_names = [p.name for p in plans]
    ph = ", ".join(["%s"] * len(plan_names))

    # Delete MRs linked to each plan
    for plan_name in plan_names:
        try:
            from auto_replenishment.auto_replenishment.doctype.replenishment_store_plan                .replenishment_store_plan import _delete_linked_mrs
            _delete_linked_mrs(plan_name)
        except Exception as e:
            frappe.log_error(str(e)[:200], "AR Plan MR Delete")

    # Delete plan child rows
    for tbl in ["tabReplenishment Store Plan Item",
                "tabReplenishment Allocation",
                "tabReplenishment MR Link"]:
        frappe.db.sql(f"DELETE FROM `{tbl}` WHERE parent IN ({ph})", plan_names)

    # Delete the plans
    frappe.db.sql(
        f"DELETE FROM `tabReplenishment Store Plan` WHERE name IN ({ph})",
        plan_names
    )
    frappe.db.commit()


def _cancel_and_delete_linked_mrs(run_name: str):
    linked = frappe.get_all("Material Request",
                            filters={"custom_auto_replenishment_forecast": run_name},
                            fields=["name", "docstatus"])
    if not linked:
        return
    names = [r.name for r in linked]
    ph = ", ".join(["%s"] * len(names))
    frappe.db.sql(
        f"UPDATE `tabMaterial Request` SET custom_auto_replenishment_forecast='' WHERE name IN ({ph})",
        names)
    frappe.db.commit()
    for mr in linked:
        try:
            if mr.docstatus == 1:
                frappe.get_doc("Material Request", mr.name).cancel()
            frappe.db.sql("DELETE FROM `tabMaterial Request Item` WHERE parent=%s", mr.name)
            frappe.db.sql("DELETE FROM `tabMaterial Request` WHERE name=%s", mr.name)
        except Exception as e:
            frappe.log_error(str(e)[:200], "AR MR Delete")
    frappe.db.delete("Replenishment MR Link", {"parent": run_name})
    frappe.db.commit()


@frappe.whitelist()
def get_attribute_values(attribute_name: str) -> list:
    """Return list of attribute values for Year or Season — used in filter dropdowns."""
    return frappe.db.sql("""
        SELECT DISTINCT attribute_value
        FROM `tabItem Attribute Value`
        WHERE parent = %s
        ORDER BY attribute_value ASC
    """, attribute_name, as_list=True)


def _get_config() -> dict:
    cfg = frappe.get_single("Replenishment Config")
    return {
        "demand_history_days":               cfg.demand_history_days or 30,
        "safety_days":                       cfg.safety_days or 7,
        "internal_intransit_lead_time_days": cfg.internal_intransit_lead_time_days or 3,
        "protection_days":                   cfg.protection_days or 5,
        "central_warehouse":                 cfg.central_warehouse,
        "batch_size":                        cfg.batch_size or 500,
        "quantity_rounding":                 cfg.quantity_rounding or "Ceil (Round Up)",
        "create_purchase_mrs":               getattr(cfg, "create_purchase_mrs", 1),
        "allocation_algorithm":              getattr(cfg, "allocation_algorithm", "Pro-rata"),
        "_schedule_type":                    "Manual",
    }
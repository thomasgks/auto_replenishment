"""
auto_replenishment/utils/mr_creator.py

Unified MR creation reading from Replenishment Allocation rows.
- Respects excluded flag and override_qty
- Creates 1 MR per source_warehouse → store_warehouse pair
- Writes mr_name back to each Allocation row
- Works from both Replenishment Run and Replenishment Store Plan
"""

import frappe
from frappe import _
from frappe.utils import today, now_datetime


# ── Public API ────────────────────────────────────────────────────────────────

def create_transfer_mrs(source_doc_name: str, source_doctype: str,
                        store_filter: str = None) -> dict:
    config      = _get_config()
    allocations = _get_transfer_allocations(source_doc_name, source_doctype, store_filter)

    if not allocations:
        return {"created_mrs": [], "mr_count": 0, "skipped": 0}

    # Group by source_warehouse → store_warehouse
    batches = {}
    for alloc in allocations:
        qty = float(alloc.override_qty or 0) or float(alloc.suggested_qty or 0)
        if qty <= 0:
            continue
        key = (alloc.source_warehouse, alloc.store_warehouse)
        batches.setdefault(key, []).append({
            "item_code":    alloc.item_code,
            "qty":          qty,
            "uom":          _get_item_uom(alloc.item_code),
            "alloc_name":   alloc.name,
            "plan_name":    alloc.store_plan or "",
        })

    run_name    = _get_run_name(source_doc_name, source_doctype)
    created_mrs = []

    for (source_wh, target_wh), items in batches.items():
        mr_name = _create_transfer_mr(source_wh, target_wh, items, run_name, config)
        if mr_name:
            created_mrs.append(mr_name)
            # Write mr_name back to each allocation row
            plan_names_for_batch = set()
            for item in items:
                frappe.db.set_value(
                    "Replenishment Allocation", item["alloc_name"],
                    "mr_name", mr_name
                )
                if item.get("plan_name"):
                    plan_names_for_batch.add(item["plan_name"])

            # Write MR link to Run and to each Store Plan in this batch
            _write_mr_link(mr_name, "Transfer", source_wh,
                           len(items), source_doc_name, source_doctype, run_name)
            for pname in plan_names_for_batch:
                pf = "transfer_mrs"
                _insert_mr_link_row(mr_name, "Transfer", source_wh,
                                    len(items), pname,
                                    "Replenishment Store Plan", pf)

    _update_mr_count(source_doc_name, source_doctype, "transfer", len(created_mrs))
    frappe.db.commit()
    return {"created_mrs": created_mrs, "mr_count": len(created_mrs), "skipped": 0}


def create_purchase_mrs(source_doc_name: str, source_doctype: str,
                        store_filter: str = None) -> dict:
    config           = _get_config()
    central_warehouse = config.get("central_warehouse") or _get_central_warehouse()
    shortages        = _get_shortage_items(source_doc_name, source_doctype, store_filter)

    if not shortages:
        return {"created_mrs": [], "mr_count": 0, "skipped": 0}

    run_name    = _get_run_name(source_doc_name, source_doctype)
    created_mrs = []
    skipped     = 0

    if config.get("mr_per_supplier", 1):
        # One MR per supplier across ALL stores — warehouse is always Central Warehouse
        batches = {}
        for s in shortages:
            supplier = _get_primary_supplier(s.item_code)
            if not supplier:
                skipped += 1
                continue
            batches.setdefault(supplier, []).append(s)

        for supplier, items in batches.items():
            mr_name = _create_purchase_mr(supplier, central_warehouse, items, run_name, config)
            if mr_name:
                created_mrs.append(mr_name)
                # Write link to Run only — NOT to Store Plan
                _write_mr_link_run_only(mr_name, "Purchase", supplier,
                                        len(items), source_doc_name, source_doctype, run_name)
    else:
        # Single MR for ALL shortage items across all stores — warehouse is Central Warehouse
        all_items = list(shortages)
        if all_items:
            mr_name = _create_purchase_mr(None, central_warehouse, all_items, run_name, config)
            if mr_name:
                created_mrs.append(mr_name)
                _write_mr_link_run_only(mr_name, "Purchase", "All Suppliers",
                                        len(all_items), source_doc_name, source_doctype, run_name)

    _update_mr_count(source_doc_name, source_doctype, "purchase", len(created_mrs))
    frappe.db.commit()
    return {"created_mrs": created_mrs, "mr_count": len(created_mrs), "skipped": skipped}


# ── Data loaders ──────────────────────────────────────────────────────────────

def _get_transfer_allocations(source_doc_name, source_doctype, store_filter):
    """Get Replenishment Allocation rows for Transfer MR creation."""
    if source_doctype == "Replenishment Run":
        filters = {
            "parent":      source_doc_name,
            "parenttype":  "Replenishment Run",
            "excluded":    0,
            "source_type": ["in", ["Central Warehouse", "Donor Store"]],
        }
        if store_filter:
            filters["store_warehouse"] = store_filter
    else:
        filters = {
            "parent":      source_doc_name,
            "parenttype":  "Replenishment Store Plan",
            "excluded":    0,
            "source_type": ["in", ["Central Warehouse", "Donor Store"]],
        }

    return frappe.get_all(
        "Replenishment Allocation",
        filters=filters,
        fields=["name", "item_code", "item_name", "source_warehouse",
                "store_warehouse", "suggested_qty", "override_qty",
                "store_plan", "forecast_item_ref"],
        order_by="source_warehouse asc, store_warehouse asc",
    )


def _get_shortage_items(source_doc_name, source_doctype, store_filter):
    """Get shortage rows for Purchase MR creation."""
    if source_doctype == "Replenishment Run":
        filters = {
            "parent":      source_doc_name,
            "parenttype":  "Replenishment Run",
            "source_type": "Shortage",
            "excluded":    0,
        }
        if store_filter:
            filters["store_warehouse"] = store_filter
    else:
        filters = {
            "parent":      source_doc_name,
            "parenttype":  "Replenishment Store Plan",
            "source_type": "Shortage",
            "excluded":    0,
        }

    rows = frappe.get_all(
        "Replenishment Allocation",
        filters=filters,
        fields=["name", "item_code", "item_name",
                "store_warehouse", "store_plan",
                "suggested_qty", "override_qty"],
        order_by="store_warehouse asc",
    )

    for r in rows:
        r["shortage_qty"] = float(r.override_qty or 0) or float(r.suggested_qty or 0)
        r["uom"]          = _get_item_uom(r.item_code)

    return [r for r in rows if r["shortage_qty"] > 0]


# ── MR creation helpers ───────────────────────────────────────────────────────

def _create_transfer_mr(source_wh, target_wh, items, run_name, config):
    if not items:
        return None
    company  = _get_company()
    src_abbr = source_wh.split(" - ")[0][:20]
    tgt_abbr = target_wh.split(" - ")[0][:20]

    mr = frappe.new_doc("Material Request")
    mr.material_request_type = "Material Transfer"
    mr.transaction_date      = today()
    mr.schedule_date         = today()
    mr.company               = company
    mr.set_from_warehouse    = source_wh
    mr.set_warehouse         = target_wh
    mr.title                 = f"AR Transfer: {src_abbr} → {tgt_abbr}"

    for item in items:
        mr.append("items", {
            "item_code":      item["item_code"],
            "qty":            item["qty"],
            "uom":            item.get("uom", "Nos"),
            "warehouse":      target_wh,
            "from_warehouse": source_wh,
        })

    mr.insert(ignore_permissions=True)
    if config.get("auto_submit_mrs"):
        mr.submit()
    return mr.name


def _create_purchase_mr(supplier, central_warehouse, items, run_name, config):
    if not items:
        return None
    company = _get_company()
    if supplier:
        title = f"AR Purchase: {run_name} / {supplier}"[:140]
    else:
        title = f"AR Purchase: {run_name}"[:140]

    mr = frappe.new_doc("Material Request")
    mr.material_request_type  = "Purchase"
    mr.transaction_date       = today()
    mr.schedule_date          = today()
    mr.company                = company
    mr.set_warehouse          = central_warehouse
    mr.title                  = title
    # Store supplier on MR for auto-fill when creating Purchase Order
    if supplier:
        mr.custom_supplier = supplier

    for s in items:
        mr.append("items", {
            "item_code": s.item_code,
            "qty":       s["shortage_qty"],
            "uom":       s.get("uom", "Nos"),
            "warehouse": central_warehouse,
        })

    mr.insert(ignore_permissions=True)
    return mr.name


# ── Utilities ─────────────────────────────────────────────────────────────────

def _get_central_warehouse():
    """Fallback: read Central Warehouse directly from Replenishment Config."""
    return frappe.db.get_single_value("Replenishment Config", "central_warehouse") or ""


def _write_mr_link_run_only(mr_name, mr_type, source, total_items,
                             source_doc, source_dt, run_name):
    """Write MR link to Run level only — never to Store Plan for Purchase MRs."""
    if source_dt == "Replenishment Run":
        _insert_mr_link_row(mr_name, mr_type, source, total_items,
                            source_doc, "Replenishment Run", "material_requests")
    else:
        # Called from Store Plan context — write to Run only
        if run_name and run_name != source_doc:
            _insert_mr_link_row(mr_name, mr_type, source, total_items,
                                run_name, "Replenishment Run", "material_requests")


_uom_cache = {}

def _get_item_uom(item_code):
    if item_code not in _uom_cache:
        _uom_cache[item_code] = (
            frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
        )
    return _uom_cache[item_code]


def _get_primary_supplier(item_code):
    return frappe.db.get_value(
        "Item Supplier", {"parent": item_code, "idx": 1}, "supplier"
    ) or ""


def _get_run_name(source_doc_name, source_doctype):
    if source_doctype == "Replenishment Run":
        return source_doc_name
    return frappe.db.get_value(
        "Replenishment Store Plan", source_doc_name, "replenishment_run"
    ) or source_doc_name


def _write_mr_link(mr_name, mr_type, source, total_items,
                   source_doc, source_dt, run_name, store_plan_name=""):
    def insert_link(parent, parentfield, parenttype):
        _insert_mr_link_row(mr_name, mr_type, source, total_items,
                            parent, parenttype, parentfield)

    # Run-level parentfield: Transfer → transfer_mrs, Purchase → material_requests
    run_pf = "transfer_mrs" if mr_type == "Transfer" else "material_requests"

    if source_dt == "Replenishment Store Plan":
        pf = "transfer_mrs" if mr_type == "Transfer" else "purchase_mrs"
        insert_link(source_doc, pf, "Replenishment Store Plan")
        if run_name and run_name != source_doc:
            insert_link(run_name, run_pf, "Replenishment Run")
    else:
        # Writing from Run level — also write to the relevant Store Plan
        insert_link(source_doc, run_pf, "Replenishment Run")
        # Find which store plan owns this MR and write to it too
        if store_plan_name:
            pf = "transfer_mrs" if mr_type == "Transfer" else "purchase_mrs"
            insert_link(store_plan_name, pf, "Replenishment Store Plan")


def _insert_mr_link_row(mr_name, mr_type, source, total_items,
                        parent, parenttype, parentfield):
    """Insert a single Replenishment MR Link row."""
    row = frappe.new_doc("Replenishment MR Link")
    row.update({
        "parent":           parent,
        "parentfield":      parentfield,
        "parenttype":       parenttype,
        "material_request": mr_name,
        "source_warehouse": source if mr_type == "Transfer" else "",
        "supplier":         source if mr_type == "Purchase"  else "",
        "mr_type":          mr_type,
        "total_items":      total_items,
        "status":           "Draft",
        "creation_date":    today(),
    })
    row.db_insert()


def _update_mr_count(source_doc_name, source_doctype, mr_type, count):
    field    = ("created_transfer_mr_count" if mr_type == "transfer"
                else "created_purchase_mr_count")
    existing = frappe.db.get_value(source_doctype, source_doc_name, field) or 0
    frappe.db.set_value(source_doctype, source_doc_name, {
        field:              existing + count,
        "last_mr_creation": now_datetime(),
    })


def _get_company():
    return (
        frappe.defaults.get_user_default("Company") or
        frappe.db.get_single_value("Global Defaults", "default_company")
    )


def _get_config():
    try:
        cfg = frappe.get_single("Replenishment Config")
        return {
            "quantity_rounding":   cfg.quantity_rounding or "Floor (Round Down)",
            "auto_submit_mrs":     cfg.auto_submit_mrs or 0,
            "create_purchase_mrs": cfg.create_purchase_mrs or 1,
            "mr_per_supplier":     cfg.mr_per_supplier or 1,
            "shortage_mr_naming":  cfg.shortage_mr_naming or "Run Reference + Supplier",
            "central_warehouse":   cfg.central_warehouse or "",
        }
    except Exception:
        frappe.throw(_("Replenishment Config is not configured."))
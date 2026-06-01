"""
auto_replenishment/report/replenishment_allocation_results/replenishment_allocation_results.py

Replenishment Allocation Results Report
Shows allocation outcome for all items across all stores in a Replenishment Run.
Filterable by Run, store, supply status, and item group.
Exportable to Excel.
"""

import frappe
from frappe import _


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data    = get_data(filters)
    chart   = get_chart(data)
    summary = get_summary(data)
    return columns, data, None, chart, summary


# ── Columns ───────────────────────────────────────────────────────────────────

def get_columns():
    return [
        {
            "label":     _("Replenishment Run"),
            "fieldname": "replenishment_run",
            "fieldtype": "Link",
            "options":   "Replenishment Run",
            "width":     160,
        },
        {
            "label":     _("Store"),
            "fieldname": "store_warehouse",
            "fieldtype": "Link",
            "options":   "Warehouse",
            "width":     200,
        },
        {
            "label":     _("Store Plan"),
            "fieldname": "store_plan",
            "fieldtype": "Link",
            "options":   "Replenishment Store Plan",
            "width":     160,
        },
        {
            "label":     _("Item Code"),
            "fieldname": "item_code",
            "fieldtype": "Link",
            "options":   "Item",
            "width":     130,
        },
        {
            "label":     _("Item Name"),
            "fieldname": "item_name",
            "fieldtype": "Data",
            "width":     200,
        },
        {
            "label":     _("Item Group"),
            "fieldname": "item_group",
            "fieldtype": "Link",
            "options":   "Item Group",
            "width":     130,
        },
        {
            "label":     _("UOM"),
            "fieldname": "uom",
            "fieldtype": "Data",
            "width":     60,
        },
        {
            "label":     _("Selling Rate"),
            "fieldname": "selling_rate",
            "fieldtype": "Float",
            "width":     100,
            "precision": 4,
        },
        {
            "label":     _("Current OnHand"),
            "fieldname": "current_onhand",
            "fieldtype": "Float",
            "width":     120,
            "precision": 2,
        },
        {
            "label":     _("Target Stock"),
            "fieldname": "target_stock",
            "fieldtype": "Float",
            "width":     100,
            "precision": 2,
        },
        {
            "label":     _("Required Qty"),
            "fieldname": "forecasted_requirement",
            "fieldtype": "Float",
            "width":     110,
            "precision": 2,
        },
        {
            "label":     _("Allocated Qty"),
            "fieldname": "allocated_qty",
            "fieldtype": "Float",
            "width":     110,
            "precision": 2,
        },
        {
            "label":     _("Shortage Qty"),
            "fieldname": "shortage_qty",
            "fieldtype": "Float",
            "width":     110,
            "precision": 2,
        },
        {
            "label":     _("Supply Status"),
            "fieldname": "supply_status",
            "fieldtype": "Data",
            "width":     120,
        },
        {
            "label":     _("Fill Rate %"),
            "fieldname": "fill_rate",
            "fieldtype": "Percent",
            "width":     90,
        },
        {
            "label":     _("Allocation Status"),
            "fieldname": "allocation_status",
            "fieldtype": "Data",
            "width":     130,
        },
        {
            "label":     _("Run Type"),
            "fieldname": "run_type",
            "fieldtype": "Data",
            "width":     90,
        },
        {
            "label":     _("Run Date"),
            "fieldname": "run_date",
            "fieldtype": "Date",
            "width":     100,
        },
    ]


# ── Data ──────────────────────────────────────────────────────────────────────

def get_data(filters):
    conditions, values = _build_conditions(filters)

    rows = frappe.db.sql(f"""
        SELECT
            p.replenishment_run,
            p.warehouse          AS store_warehouse,
            p.name               AS store_plan,
            p.run_type,
            p.forecast_date      AS run_date,
            p.allocation_status,
            i.item_code,
            i.item_name,
            it.item_group,
            i.uom,
            i.selling_rate,
            i.current_onhand,
            i.target_stock,
            i.forecasted_requirement,
            i.allocated_qty,
            i.shortage_qty,
            i.supply_status,
            CASE
                WHEN i.forecasted_requirement > 0
                THEN ROUND(i.allocated_qty / i.forecasted_requirement * 100, 1)
                ELSE 100
            END AS fill_rate
        FROM `tabReplenishment Store Plan Item` i
        JOIN `tabReplenishment Store Plan` p ON p.name = i.parent
        LEFT JOIN `tabItem` it ON it.name = i.item_code
        WHERE i.forecasted_requirement > 0
          {conditions}
        ORDER BY
            p.replenishment_run,
            p.warehouse,
            i.supply_status,
            i.item_code
    """, values, as_dict=True)

    # Colour-code supply status
    for row in rows:
        status = row.supply_status or ""
        if status == "Full Supply":
            row["_css_class"] = "success"
        elif status == "Partial Supply":
            row["_css_class"] = "warning"
        elif status == "No Supply":
            row["_css_class"] = "danger"

    return rows


def _build_conditions(filters):
    conditions = []
    values     = {}

    if filters.get("replenishment_run"):
        conditions.append("AND p.replenishment_run = %(replenishment_run)s")
        values["replenishment_run"] = filters["replenishment_run"]

    if filters.get("store_warehouse"):
        conditions.append("AND p.warehouse = %(store_warehouse)s")
        values["store_warehouse"] = filters["store_warehouse"]

    if filters.get("supply_status"):
        conditions.append("AND i.supply_status = %(supply_status)s")
        values["supply_status"] = filters["supply_status"]

    if filters.get("item_group"):
        # Include child groups
        from auto_replenishment.utils.item_filter_engine import _get_child_groups
        groups = _get_child_groups([filters["item_group"]])
        placeholders = ", ".join([f"%(ig_{i})s" for i in range(len(groups))])
        for idx, g in enumerate(groups):
            values[f"ig_{idx}"] = g
        conditions.append(f"AND it.item_group IN ({placeholders})")

    if filters.get("run_type"):
        conditions.append("AND p.run_type = %(run_type)s")
        values["run_type"] = filters["run_type"]

    if filters.get("from_date"):
        conditions.append("AND p.forecast_date >= %(from_date)s")
        values["from_date"] = filters["from_date"]

    if filters.get("to_date"):
        conditions.append("AND p.forecast_date <= %(to_date)s")
        values["to_date"] = filters["to_date"]

    return " ".join(conditions), values


# ── Chart ─────────────────────────────────────────────────────────────────────

def get_chart(data):
    if not data:
        return None

    full    = sum(1 for r in data if r.get("supply_status") == "Full Supply")
    partial = sum(1 for r in data if r.get("supply_status") == "Partial Supply")
    no_sup  = sum(1 for r in data if r.get("supply_status") == "No Supply")

    return {
        "data": {
            "labels":   ["Full Supply", "Partial Supply", "No Supply"],
            "datasets": [{"values": [full, partial, no_sup]}],
        },
        "type":   "donut",
        "colors": ["#16a34a", "#f59e0b", "#ef4444"],
        "height": 280,
    }


# ── Summary ───────────────────────────────────────────────────────────────────

def get_summary(data):
    if not data:
        return []

    total    = len(data)
    full     = sum(1 for r in data if r.get("supply_status") == "Full Supply")
    partial  = sum(1 for r in data if r.get("supply_status") == "Partial Supply")
    no_sup   = sum(1 for r in data if r.get("supply_status") == "No Supply")
    total_req  = sum(float(r.get("forecasted_requirement") or 0) for r in data)
    total_alloc = sum(float(r.get("allocated_qty") or 0) for r in data)
    total_short = sum(float(r.get("shortage_qty") or 0) for r in data)
    fill_rate   = round(total_alloc / total_req * 100, 1) if total_req else 100

    return [
        {
            "value":       total,
            "label":       _("Total Items"),
            "datatype":    "Int",
            "indicator":   "blue",
        },
        {
            "value":       full,
            "label":       _("Full Supply"),
            "datatype":    "Int",
            "indicator":   "green",
        },
        {
            "value":       partial,
            "label":       _("Partial Supply"),
            "datatype":    "Int",
            "indicator":   "orange",
        },
        {
            "value":       no_sup,
            "label":       _("No Supply"),
            "datatype":    "Int",
            "indicator":   "red",
        },
        {
            "value":       fill_rate,
            "label":       _("Overall Fill Rate %"),
            "datatype":    "Percent",
            "indicator":   "green" if fill_rate >= 90 else "orange" if fill_rate >= 70 else "red",
        },
        {
            "value":       total_short,
            "label":       _("Total Shortage Qty"),
            "datatype":    "Float",
            "indicator":   "red" if total_short > 0 else "green",
        },
    ]

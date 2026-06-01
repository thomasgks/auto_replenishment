"""
auto_replenishment/utils/item_filter_engine.py

Resolves which stores and items to include in a Replenishment Run
based on the filter settings on the Run document or Replenishment Config.

Warehouse roles (custom_replenishment_role field on Warehouse):
  Retail Store       → included in forecasting + replenishment
  Central Warehouse  → source only, never forecasted or replenished
  Goods In Transit   → stock counted as usable in-transit, not forecasted
  Exclude            → ignored completely (damaged, sample, online, etc.)
  (blank)            → treated as Retail Store for backward compatibility
"""

import frappe
from frappe import _


def get_store_warehouses(source_doc) -> list:
    """
    Get list of store warehouses for a Replenishment Run.
    Respects both the run's store filter AND warehouse replenishment roles.
    """
    mode = getattr(source_doc, "store_filter_mode", None) or "All Stores"
    selected = getattr(source_doc, "filter_stores", None) or []

    if mode == "Selected Stores" and selected:
        wh_list = [r.warehouse for r in selected if r.warehouse]
        # Still filter out excluded warehouses from selected list
        return _filter_by_role(wh_list)

    return get_all_retail_stores()


def get_store_warehouses_from_config(config_doc) -> list:
    """Get store warehouses using scheduler filter settings from Config."""
    mode = getattr(config_doc, "scheduler_store_filter_mode", "All Stores")
    selected = getattr(config_doc, "scheduler_stores", []) or []

    if mode == "Selected Stores" and selected:
        wh_list = [r.warehouse for r in selected if r.warehouse]
        return _filter_by_role(wh_list)

    return get_all_retail_stores()


def get_all_retail_stores() -> list:
    """
    Get all warehouses with role = 'Retail Store' OR no role set (backward compat).
    Excludes Central Warehouse, Goods In Transit, and Exclude roles.
    """
    config = frappe.get_single("Replenishment Config")
    central_wh = config.central_warehouse or ""

    warehouses = frappe.db.sql("""
        SELECT w.name
        FROM `tabWarehouse` w
        WHERE w.is_group = 0
          AND w.disabled = 0
          AND w.name != %s
          AND (
              w.custom_replenishment_role NOT IN
                  ('Central Warehouse', 'Goods In Transit', 'Exclude')
              OR w.custom_replenishment_role IS NULL
              OR w.custom_replenishment_role = ''
          )
        ORDER BY w.name
    """, (central_wh,), as_list=True)

    return [w[0] for w in warehouses]


def get_intransit_warehouses() -> list:
    """
    Get warehouses marked as Goods In Transit.
    Stock in these warehouses counts as usable_intransit in forecasting.
    """
    return frappe.db.sql("""
        SELECT name FROM `tabWarehouse`
        WHERE is_group = 0
          AND disabled = 0
          AND custom_replenishment_role = 'Goods In Transit'
        ORDER BY name
    """, as_list=True)


def get_donor_warehouses(exclude_stores: list = None) -> list:
    """
    Get warehouses eligible to donate surplus stock.
    Only Retail Stores can be donors — not Central WH, Transit, or Excluded.
    """
    config = frappe.get_single("Replenishment Config")
    central_wh = config.central_warehouse or ""
    exclude = set(exclude_stores or []) | {central_wh}

    warehouses = frappe.db.sql("""
        SELECT name FROM `tabWarehouse`
        WHERE is_group = 0
          AND disabled = 0
          AND (
              custom_replenishment_role NOT IN
                  ('Exclude', 'Goods In Transit', 'Central Warehouse')
              OR custom_replenishment_role IS NULL
              OR custom_replenishment_role = ''
          )
        ORDER BY name
    """, as_list=True)

    return [w[0] for w in warehouses if w[0] not in exclude]


def is_excluded_warehouse(warehouse_name: str) -> bool:
    """Return True if warehouse should be completely ignored."""
    role = frappe.db.get_value("Warehouse", warehouse_name,
                               "custom_replenishment_role")
    return role == "Exclude"


def _filter_by_role(wh_list: list) -> list:
    """Remove excluded warehouses from a list."""
    if not wh_list:
        return []
    result = []
    for wh in wh_list:
        role = frappe.db.get_value("Warehouse", wh, "custom_replenishment_role") or ""
        if role not in ("Exclude", "Central Warehouse", "Goods In Transit"):
            result.append(wh)
    return result


def get_item_filters(source_doc) -> dict:
    """Build item filter conditions from run/config filter settings."""
    filters = {}
    conditions = []
    values = []

    suppliers = (
        getattr(source_doc, "filter_suppliers", None) or
        getattr(source_doc, "scheduler_suppliers", []) or []
    )
    if suppliers:
        supplier_list = [r.supplier for r in suppliers if r.supplier]
        if supplier_list:
            placeholders = ", ".join(["%s"] * len(supplier_list))
            conditions.append(
                f"EXISTS (SELECT 1 FROM `tabItem Supplier` s "
                f"WHERE s.parent = i.name AND s.supplier IN ({placeholders}))"
            )
            values.extend(supplier_list)
            filters["suppliers"] = supplier_list

    groups_l4 = (
        getattr(source_doc, "filter_item_groups_l4", None) or
        getattr(source_doc, "scheduler_item_groups_l4", []) or []
    )
    if groups_l4:
        group_list = [r.item_group for r in groups_l4 if r.item_group]
        if group_list:
            all_groups = _get_child_groups(group_list)
            placeholders = ", ".join(["%s"] * len(all_groups))
            conditions.append(f"i.item_group IN ({placeholders})")
            values.extend(all_groups)
            filters["item_groups_l4"] = group_list

    groups_l5 = (
        getattr(source_doc, "filter_item_groups_l5", None) or
        getattr(source_doc, "scheduler_item_groups_l5", []) or []
    )
    if groups_l5:
        group_list = [r.item_group for r in groups_l5 if r.item_group]
        if group_list:
            placeholders = ", ".join(["%s"] * len(group_list))
            conditions.append(f"i.item_group IN ({placeholders})")
            values.extend(group_list)
            filters["item_groups_l5"] = group_list

    year = (
        getattr(source_doc, "filter_year", None) or
        getattr(source_doc, "scheduler_year", None) or ""
    )
    if year:
        conditions.append(
            "EXISTS (SELECT 1 FROM `tabItem Variant Attribute` va "
            "WHERE va.parent = i.name "
            "AND va.attribute = 'Year' AND va.attribute_value = %s)"
        )
        values.append(year)
        filters["year"] = year

    season = (
        getattr(source_doc, "filter_season", None) or
        getattr(source_doc, "scheduler_season", None) or ""
    )
    if season:
        conditions.append(
            "EXISTS (SELECT 1 FROM `tabItem Variant Attribute` va "
            "WHERE va.parent = i.name "
            "AND va.attribute = 'Season' AND va.attribute_value = %s)"
        )
        values.append(season)
        filters["season"] = season

    filters["_conditions"] = " AND ".join(conditions) if conditions else ""
    filters["_values"]     = values
    return filters


def _get_child_groups(group_names: list) -> list:
    all_groups = set(group_names)
    queue = list(group_names)
    while queue:
        children = frappe.db.get_all(
            "Item Group",
            filters={"parent_item_group": ["in", queue]},
            fields=["name"],
        )
        new = [c.name for c in children if c.name not in all_groups]
        all_groups.update(new)
        queue = new
    return list(all_groups)

"""
auto_replenishment/utils/forecast_engine.py

High-performance Material Forecast Engine for ERPNext.
Designed for 300K+ items × 35 stores using:
  - Bulk SQL queries (no per-item frappe.get_doc loops)
  - pandas DataFrames for vectorized calculations
  - Redis caching of item master data
  - Chunked batch processing to control memory
  - Only two lead time types:
      1. Supplier Lead Time  (for PO-based warehouse replenishment — info only)
      2. Internal In-Transit Lead Time  (warehouse→store / store→store, config-level avg)
"""

import frappe
import pandas as pd
from datetime import date, timedelta
import json
import hashlib


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_forecast_for_store(warehouse: str, config: dict, as_of: date = None) -> pd.DataFrame:
    """
    Calculate material forecast for a single store warehouse.

    Returns a DataFrame with columns:
        item_code, item_name, uom,
        sales_30d, selling_rate, lead_time_days, safety_days,
        lead_time_demand, safety_stock, target_stock,
        current_onhand, usable_intransit, effective_onhand,
        forecasted_requirement, supply_status
    Only rows where forecasted_requirement > 0 are returned.
    """
    if as_of is None:
        as_of = date.today()

    history_days = config.get("demand_history_days", 30)
    safety_days_default = config.get("safety_days", 7)
    lead_time_days = config.get("internal_intransit_lead_time_days", 3)
    central_warehouse = config.get("central_warehouse")

    # ── Step 1: Get eligible items (not excluded, has supply potential) ────
    items_df = _get_eligible_items(warehouse, central_warehouse, as_of, config)
    if items_df.empty:
        return pd.DataFrame()

    item_codes = items_df["item_code"].tolist()

    # ── Step 2: Bulk fetch 30-day sales ────────────────────────────────────
    sales_df = _get_bulk_sales(item_codes, warehouse, as_of, history_days)

    # ── Step 3: Bulk fetch current OnHand ─────────────────────────────────
    onhand_df = _get_bulk_onhand(item_codes, warehouse)

    # ── Step 4: Bulk fetch usable in-transit (internal transfers) ─────────
    intransit_df = _get_bulk_intransit(item_codes, warehouse, as_of, lead_time_days)

    # ── Step 5: Merge all data ─────────────────────────────────────────────
    df = items_df.merge(sales_df, on="item_code", how="left")
    df = df.merge(onhand_df, on="item_code", how="left")
    df = df.merge(intransit_df, on="item_code", how="left")

    # Fill NaN with 0 for numeric columns
    numeric_cols = ["sales_30d", "current_onhand", "usable_intransit"]
    df[numeric_cols] = df[numeric_cols].fillna(0)

    # ── Step 6: Vectorized calculations ────────────────────────────────────
    df["selling_rate"] = (df["sales_30d"] / history_days).round(4)
    df["lead_time_days"] = lead_time_days
    df["safety_days"] = df.get("custom_safety_days", safety_days_default)
    if "custom_safety_days" not in df.columns:
        df["safety_days"] = safety_days_default

    df["lead_time_demand"] = (df["selling_rate"] * df["lead_time_days"]).round(2)
    df["safety_stock"] = (df["selling_rate"] * df["safety_days"]).round(2)
    df["target_stock"] = (df["lead_time_demand"] + df["safety_stock"]).round(2)

    # Effective OnHand = current + in-transit arriving before stockout
    df["effective_onhand"] = (df["current_onhand"] + df["usable_intransit"]).round(2)

    # Forecasted requirement (floor at 0)
    df["forecasted_requirement"] = (df["target_stock"] - df["effective_onhand"]).round(2)
    df["forecasted_requirement"] = df["forecasted_requirement"].clip(lower=0)

    # ── Step 7: Filter — only items actually needed ─────────────────────────
    df = df[df["forecasted_requirement"] > 0].copy()

    # ── Step 8: Supply status placeholder (filled during allocation) ───────
    df["supply_status"] = "Pending"

    return df.reset_index(drop=True)


def run_forecast_all_stores(config: dict, as_of: date = None) -> dict:
    """
    Run forecast for ALL store warehouses.
    Returns {warehouse_name: DataFrame}

    Uses chunked processing to handle 300K items × 35 stores efficiently.
    """
    if as_of is None:
        as_of = date.today()

    store_warehouses = _get_store_warehouses(config.get("central_warehouse"))
    results = {}
    for wh in store_warehouses:
        try:
            df = run_forecast_for_store(wh, config, as_of)
            results[wh] = df
            frappe.logger().info(f"[AutoReplenishment] Forecast complete for {wh}: {len(df)} items need replenishment")
        except Exception as e:
            frappe.log_error(f"Forecast failed for warehouse {wh}: {str(e)}", "Auto Replenishment Forecast Error")
            results[wh] = pd.DataFrame()
    return results


# ---------------------------------------------------------------------------
# Donor store evaluation (for Allocator Agent)
# ---------------------------------------------------------------------------

def evaluate_donor_stores(
    item_code: str,
    requesting_store: str,
    gap_qty: float,
    config: dict,
    as_of: date = None
) -> list:
    """
    Evaluate all potential donor stores for a single item.
    Returns list of dicts sorted by DOS descending (best donors first).

    Each dict:
        warehouse, effective_onhand, selling_rate, protected_stock,
        transferable_qty, dos, dos_after_transfer,
        requesting_dos_after_transfer, fairness_pass
    """
    if as_of is None:
        as_of = date.today()

    protection_days = config.get("protection_days", 5)
    lead_time_days = config.get("internal_intransit_lead_time_days", 3)
    history_days = config.get("demand_history_days", 30)
    central_warehouse = config.get("central_warehouse")

    # Get all store warehouses excluding the requesting store
    all_stores = _get_store_warehouses(central_warehouse)
    donor_candidates = [s for s in all_stores if s != requesting_store]

    if not donor_candidates:
        return []

    # Bulk fetch sales + onhand + intransit for all donors at once
    sales_data = _get_bulk_sales_multi_warehouse(item_code, donor_candidates, as_of, history_days)
    onhand_data = _get_bulk_onhand_multi_warehouse(item_code, donor_candidates)
    intransit_data = _get_bulk_intransit_multi_warehouse(item_code, donor_candidates, as_of, lead_time_days)

    # Get requesting store DOS after receiving transfer
    req_sales = _get_single_item_sales(item_code, requesting_store, as_of, history_days)
    req_onhand = _get_single_item_onhand(item_code, requesting_store)
    req_intransit = _get_single_item_intransit(item_code, requesting_store, as_of, lead_time_days)
    req_effective_onhand = req_onhand + req_intransit
    req_selling_rate = req_sales / history_days if history_days > 0 else 0

    donors = []
    for wh in donor_candidates:
        donor_sales = sales_data.get(wh, 0)
        donor_onhand = onhand_data.get(wh, 0)
        donor_intransit = intransit_data.get(wh, 0)

        donor_effective_onhand = donor_onhand + donor_intransit
        donor_selling_rate = donor_sales / history_days if history_days > 0 else 0

        # Current DOS
        donor_dos = (donor_effective_onhand / donor_selling_rate) if donor_selling_rate > 0 else 9999

        # Protected stock: must keep at least protection_days worth
        protected_stock = donor_selling_rate * protection_days

        # Transferable quantity
        transferable_qty = max(0, donor_effective_onhand - protected_stock)
        if transferable_qty <= 0:
            continue  # Donor has nothing to give

        # How much can we actually take (cap at gap)
        actual_transfer = min(transferable_qty, gap_qty)

        # DOS after transfer
        donor_remaining = donor_effective_onhand - actual_transfer
        donor_dos_after = (donor_remaining / donor_selling_rate) if donor_selling_rate > 0 else 9999

        # Requesting store DOS after receiving
        req_onhand_after = req_effective_onhand + actual_transfer
        req_dos_after = (req_onhand_after / req_selling_rate) if req_selling_rate > 0 else 9999

        # Fairness check: requesting store DOS after >= donor DOS after
        fairness_pass = req_dos_after >= donor_dos_after

        donors.append({
            "warehouse": wh,
            "effective_onhand": donor_effective_onhand,
            "selling_rate": donor_selling_rate,
            "protected_stock": protected_stock,
            "transferable_qty": transferable_qty,
            "dos": donor_dos,
            "dos_after_transfer": donor_dos_after,
            "requesting_dos_after_transfer": req_dos_after,
            "fairness_pass": fairness_pass,
            "actual_transfer_qty": actual_transfer if fairness_pass else 0
        })

    # Sort by DOS descending (most surplus first), fairness-passing donors first
    donors.sort(key=lambda d: (-int(d["fairness_pass"]), -d["dos"]))
    return donors


# ---------------------------------------------------------------------------
# Internal helpers — all use bulk SQL for performance
# ---------------------------------------------------------------------------

def _get_store_warehouses(central_warehouse: str) -> list:
    """Get all non-central warehouses that are stores."""
    cache_key = f"ar_store_warehouses_{central_warehouse}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    warehouses = frappe.db.sql("""
        SELECT name FROM `tabWarehouse`
        WHERE is_group = 0
          AND disabled = 0
          AND name != %(central)s
          AND (warehouse_type = 'Transit' OR warehouse_type IS NULL OR warehouse_type = 'Store')
        ORDER BY name
    """, {"central": central_warehouse}, as_dict=False)

    result = [w[0] for w in warehouses]
    frappe.cache().set_value(cache_key, result, expires_in_sec=3600)
    return result


def _get_eligible_items(warehouse: str, central_warehouse: str, as_of: date, config: dict) -> pd.DataFrame:
    """
    Get items eligible for forecasting in a store.
    Conditions:
      1. Item not excluded from auto-replenishment
      2. Item has supply potential (stock exists somewhere in company OR open PO arriving today)
    Uses bulk SQL — single query returning all eligible items.
    """
    history_days = config.get("demand_history_days", 30)
    from_date = as_of - timedelta(days=history_days)

    # Items that sold recently in this store (likely to sell again)
    # UNION with items that have current stock in this store (to catch near-zero situations)
    rows = frappe.db.sql("""
        SELECT DISTINCT
            i.item_code,
            i.item_name,
            i.stock_uom AS uom,
            IFNULL(i.custom_safety_days, 0) AS custom_safety_days,
            IFNULL(i.custom_exclude_from_replenishment, 0) AS excluded
        FROM `tabItem` i
        WHERE i.disabled = 0
          AND IFNULL(i.custom_exclude_from_replenishment, 0) = 0
          AND (
            -- Has been sold in this store recently
            EXISTS (
                SELECT 1 FROM `tabStock Ledger Entry` sle
                WHERE sle.item_code = i.item_code
                  AND sle.warehouse = %(warehouse)s
                  AND sle.voucher_type IN ('Delivery Note', 'Sales Invoice', 'POS Invoice')
                  AND sle.posting_date BETWEEN %(from_date)s AND %(as_of)s
                  AND sle.actual_qty < 0
                LIMIT 1
            )
            OR
            -- Has current stock in the store
            EXISTS (
                SELECT 1 FROM `tabBin` b
                WHERE b.item_code = i.item_code
                  AND b.warehouse = %(warehouse)s
                  AND b.actual_qty > 0
                LIMIT 1
            )
          )
          AND (
            -- Supply potential: stock exists in company
            EXISTS (
                SELECT 1 FROM `tabBin` b2
                WHERE b2.item_code = i.item_code
                  AND b2.actual_qty > 0
                LIMIT 1
            )
            OR
            -- Or open PO expected today or earlier
            EXISTS (
                SELECT 1 FROM `tabPurchase Order Item` poi
                JOIN `tabPurchase Order` po ON po.name = poi.parent
                WHERE poi.item_code = i.item_code
                  AND po.docstatus = 1
                  AND po.status NOT IN ('Closed', 'Cancelled')
                  AND poi.schedule_date <= %(as_of)s
                LIMIT 1
            )
          )
        ORDER BY i.item_code
    """, {
        "warehouse": warehouse,
        "from_date": str(from_date),
        "as_of": str(as_of)
    }, as_dict=True)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df[df["excluded"] == 0].drop(columns=["excluded"])
    return df


def _get_bulk_sales(item_codes: list, warehouse: str, as_of: date, history_days: int) -> pd.DataFrame:
    """Bulk fetch 30-day outbound qty for all items in one query."""
    if not item_codes:
        return pd.DataFrame(columns=["item_code", "sales_30d"])

    from_date = as_of - timedelta(days=history_days)

    # Process in batches to avoid MySQL IN clause limits
    all_rows = []
    batch_size = 1000
    for i in range(0, len(item_codes), batch_size):
        batch = item_codes[i:i + batch_size]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = frappe.db.sql(f"""
            SELECT
                item_code,
                ABS(SUM(actual_qty)) AS sales_30d
            FROM `tabStock Ledger Entry`
            WHERE item_code IN ({placeholders})
              AND warehouse = %s
              AND voucher_type IN ('Delivery Note', 'Sales Invoice', 'POS Invoice')
              AND posting_date BETWEEN %s AND %s
              AND actual_qty < 0
            GROUP BY item_code
        """, tuple(batch) + (warehouse, str(from_date), str(as_of)), as_dict=True)
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=["item_code", "sales_30d"])

    df = pd.DataFrame(all_rows)
    return df


def _get_bulk_onhand(item_codes: list, warehouse: str) -> pd.DataFrame:
    """Bulk fetch current on-hand quantity from Bin."""
    if not item_codes:
        return pd.DataFrame(columns=["item_code", "current_onhand"])

    all_rows = []
    batch_size = 1000
    for i in range(0, len(item_codes), batch_size):
        batch = item_codes[i:i + batch_size]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = frappe.db.sql(f"""
            SELECT item_code, actual_qty AS current_onhand
            FROM `tabBin`
            WHERE item_code IN ({placeholders})
              AND warehouse = %s
        """, tuple(batch) + (warehouse,), as_dict=True)
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=["item_code", "current_onhand"])

    return pd.DataFrame(all_rows)


def _get_bulk_intransit(item_codes: list, warehouse: str, as_of: date, lead_time_days: int) -> pd.DataFrame:
    """
    Bulk fetch usable in-transit stock for internal transfers.
    Only counts stock from Stock Entry (Material Transfer) where:
      - Status is Draft/In-Transit
      - Expected arrival within lead_time_days
    """
    if not item_codes:
        return pd.DataFrame(columns=["item_code", "usable_intransit"])

    cutoff_date = as_of + timedelta(days=lead_time_days)
    all_rows = []
    batch_size = 1000
    for i in range(0, len(item_codes), batch_size):
        batch = item_codes[i:i + batch_size]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = frappe.db.sql(f"""
            SELECT
                seid.item_code,
                SUM(seid.qty) AS usable_intransit
            FROM `tabStock Entry Detail` seid
            JOIN `tabStock Entry` se ON se.name = seid.parent
            WHERE seid.item_code IN ({placeholders})
              AND seid.t_warehouse = %s
              AND se.stock_entry_type = 'Material Transfer'
              AND se.docstatus = 1
              AND se.per_transferred < 100
              AND (se.schedule_date IS NULL OR se.schedule_date <= %s)
            GROUP BY seid.item_code
        """, tuple(batch) + (warehouse, str(cutoff_date)), as_dict=True)
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=["item_code", "usable_intransit"])

    return pd.DataFrame(all_rows)


# ── Multi-warehouse helpers (for donor evaluation) ────────────────────────

def _get_bulk_sales_multi_warehouse(item_code: str, warehouses: list, as_of: date, history_days: int) -> dict:
    """Returns {warehouse: sales_qty}"""
    if not warehouses:
        return {}
    from_date = as_of - timedelta(days=history_days)
    placeholders = ", ".join(["%s"] * len(warehouses))
    rows = frappe.db.sql(f"""
        SELECT warehouse, ABS(SUM(actual_qty)) AS sales_30d
        FROM `tabStock Ledger Entry`
        WHERE item_code = %s
          AND warehouse IN ({placeholders})
          AND voucher_type IN ('Delivery Note', 'Sales Invoice', 'POS Invoice')
          AND posting_date BETWEEN %s AND %s
          AND actual_qty < 0
        GROUP BY warehouse
    """, (item_code,) + tuple(warehouses) + (str(from_date), str(as_of)), as_dict=True)
    return {r["warehouse"]: r["sales_30d"] for r in rows}


def _get_bulk_onhand_multi_warehouse(item_code: str, warehouses: list) -> dict:
    if not warehouses:
        return {}
    placeholders = ", ".join(["%s"] * len(warehouses))
    rows = frappe.db.sql(f"""
        SELECT warehouse, actual_qty
        FROM `tabBin`
        WHERE item_code = %s AND warehouse IN ({placeholders})
    """, (item_code,) + tuple(warehouses), as_dict=True)
    return {r["warehouse"]: r["actual_qty"] for r in rows}


def _get_bulk_intransit_multi_warehouse(item_code: str, warehouses: list, as_of: date, lead_time_days: int) -> dict:
    if not warehouses:
        return {}
    cutoff_date = as_of + timedelta(days=lead_time_days)
    placeholders = ", ".join(["%s"] * len(warehouses))
    rows = frappe.db.sql(f"""
        SELECT seid.t_warehouse AS warehouse, SUM(seid.qty) AS qty
        FROM `tabStock Entry Detail` seid
        JOIN `tabStock Entry` se ON se.name = seid.parent
        WHERE seid.item_code = %s
          AND seid.t_warehouse IN ({placeholders})
          AND se.stock_entry_type = 'Material Transfer'
          AND se.docstatus = 1
          AND se.per_transferred < 100
          AND (se.schedule_date IS NULL OR se.schedule_date <= %s)
        GROUP BY seid.t_warehouse
    """, (item_code,) + tuple(warehouses) + (str(cutoff_date),), as_dict=True)
    return {r["warehouse"]: r["qty"] for r in rows}


def _get_single_item_sales(item_code: str, warehouse: str, as_of: date, history_days: int) -> float:
    from_date = as_of - timedelta(days=history_days)
    result = frappe.db.sql("""
        SELECT ABS(SUM(actual_qty)) AS qty
        FROM `tabStock Ledger Entry`
        WHERE item_code = %s AND warehouse = %s
          AND voucher_type IN ('Delivery Note', 'Sales Invoice', 'POS Invoice')
          AND posting_date BETWEEN %s AND %s
          AND actual_qty < 0
    """, (item_code, warehouse, str(from_date), str(as_of)))
    return float(result[0][0] or 0)


def _get_single_item_onhand(item_code: str, warehouse: str) -> float:
    result = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
    return float(result or 0)


def _get_single_item_intransit(item_code: str, warehouse: str, as_of: date, lead_time_days: int) -> float:
    cutoff_date = as_of + timedelta(days=lead_time_days)
    result = frappe.db.sql("""
        SELECT SUM(seid.qty)
        FROM `tabStock Entry Detail` seid
        JOIN `tabStock Entry` se ON se.name = seid.parent
        WHERE seid.item_code = %s AND seid.t_warehouse = %s
          AND se.stock_entry_type = 'Material Transfer'
          AND se.docstatus = 1
          AND se.per_transferred < 100
          AND (se.schedule_date IS NULL OR se.schedule_date <= %s)
    """, (item_code, warehouse, str(cutoff_date)))
    return float(result[0][0] or 0)

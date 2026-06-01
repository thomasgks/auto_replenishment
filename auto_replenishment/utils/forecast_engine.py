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
import math
import numpy as np
from datetime import date, timedelta
import json
import hashlib

# ---------------------------------------------------------------------------
# Helper — convert frappe._dict rows to plain dicts for pandas
# ---------------------------------------------------------------------------
# frappe.db.sql(..., as_dict=True) returns frappe._dict objects which
# contain internal C-extension metadata that confuses pd.DataFrame()
# on certain pandas/numpy versions, raising "invalid __array_struct__".
# Always pass rows through _to_records() before constructing a DataFrame.


def _to_records(rows) -> list:
    """Convert a list of frappe._dict (or any mapping) to plain Python dicts."""
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_forecast_for_store(
    warehouse: str, config: dict, as_of: date = None
) -> pd.DataFrame:
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
    quantity_rounding = config.get("quantity_rounding", "Ceil (Round Up)")

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

    # Preserve raw onhand for display, but cap at 0 for calculations.
    # Negative stock occurs in ERPNext from backdated entries or opening
    # balances.  Using a negative value as "available stock" would inflate
    # the forecasted requirement (e.g. target=1, onhand=-2 → req=3 instead of 1).
    df["current_onhand_display"] = df["current_onhand"]  # keep raw for UI
    df["current_onhand"] = df["current_onhand"].clip(lower=0)  # floor at 0 for calc
    df["usable_intransit"] = df["usable_intransit"].clip(lower=0)

    # ── Step 6: Vectorized calculations ────────────────────────────────────
    df["selling_rate"] = (df["sales_30d"] / history_days).round(4)
    df["lead_time_days"] = lead_time_days

    # Safety Days: use per-item override only when it is explicitly > 0.
    # A value of 0 means "not configured on this item" — fall back to the
    # system-wide default from Replenishment Config.
    if "custom_safety_days" in df.columns:
        # Replace 0 / NaN with NaN, then fill with system default
        custom = pd.to_numeric(df["custom_safety_days"], errors="coerce")
        custom = custom.replace(0, pd.NA)  # 0 = not set
        df["safety_days"] = custom.fillna(safety_days_default).astype(int)
    else:
        df["safety_days"] = safety_days_default

    # ── Compute raw (pre-rounding) intermediate values ──────────────────────
    df["lead_time_demand"] = (df["selling_rate"] * df["lead_time_days"]).round(4)
    df["safety_stock"] = (df["selling_rate"] * df["safety_days"]).round(4)

    # ── Apply quantity rounding to components BEFORE summing ────────────────
    # IMPORTANT: rounding must happen per-component first, then sum.
    # Reason: each component is an independent discrete quantity.
    # ceil(LTD_raw + SS_raw) would give 1 for 0.025+0.058=0.083  ← WRONG
    # ceil(LTD_raw) + ceil(SS_raw) gives 1+1 = 2                 ← CORRECT
    if quantity_rounding == "Ceil (Round Up)":
        df["lead_time_demand"] = np.ceil(df["lead_time_demand"])
        df["safety_stock"] = np.ceil(df["safety_stock"])
    elif quantity_rounding == "Round (Nearest)":
        df["lead_time_demand"] = df["lead_time_demand"].round(0)
        df["safety_stock"] = df["safety_stock"].round(0)
    # else "None (Keep Decimals)": keep 4dp values as-is

    # Target stock = sum of already-rounded components
    df["target_stock"] = df["lead_time_demand"] + df["safety_stock"]

    # Effective OnHand = current + in-transit (both already floored at 0)
    df["effective_onhand"] = (df["current_onhand"] + df["usable_intransit"]).round(4)

    # Forecasted requirement = how much to order (never negative)
    df["forecasted_requirement"] = (df["target_stock"] - df["effective_onhand"]).round(
        4
    )
    df["forecasted_requirement"] = df["forecasted_requirement"].clip(lower=0)

    # Final rounding on requirement itself
    if quantity_rounding == "Ceil (Round Up)":
        df["forecasted_requirement"] = np.ceil(df["forecasted_requirement"]).astype(int)
    elif quantity_rounding == "Round (Nearest)":
        df["forecasted_requirement"] = df["forecasted_requirement"].round(0).astype(int)
    # else: keep decimals

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
            frappe.logger().info(
                f"[AutoReplenishment] Forecast complete for {wh}: {len(df)} items need replenishment"
            )
        except Exception as e:
            frappe.log_error(
                f"Forecast failed for warehouse {wh}: {str(e)}",
                "Auto Replenishment Forecast Error",
            )
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
    as_of: date = None,
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
    sales_data = _get_bulk_sales_multi_warehouse(
        item_code, donor_candidates, as_of, history_days
    )
    onhand_data = _get_bulk_onhand_multi_warehouse(item_code, donor_candidates)
    intransit_data = _get_bulk_intransit_multi_warehouse(
        item_code, donor_candidates, as_of, lead_time_days
    )

    # Get requesting store DOS after receiving transfer
    req_sales = _get_single_item_sales(item_code, requesting_store, as_of, history_days)
    req_onhand = _get_single_item_onhand(item_code, requesting_store)
    req_intransit = _get_single_item_intransit(
        item_code, requesting_store, as_of, lead_time_days
    )
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
        donor_dos = (
            (donor_effective_onhand / donor_selling_rate)
            if donor_selling_rate > 0
            else 9999
        )

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
        donor_dos_after = (
            (donor_remaining / donor_selling_rate) if donor_selling_rate > 0 else 9999
        )

        # Requesting store DOS after receiving
        req_onhand_after = req_effective_onhand + actual_transfer
        req_dos_after = (
            (req_onhand_after / req_selling_rate) if req_selling_rate > 0 else 9999
        )

        # Fairness check: requesting store DOS after >= donor DOS after
        fairness_pass = req_dos_after >= donor_dos_after

        donors.append(
            {
                "warehouse": wh,
                "effective_onhand": donor_effective_onhand,
                "selling_rate": donor_selling_rate,
                "protected_stock": protected_stock,
                "transferable_qty": transferable_qty,
                "dos": donor_dos,
                "dos_after_transfer": donor_dos_after,
                "requesting_dos_after_transfer": req_dos_after,
                "fairness_pass": fairness_pass,
                "actual_transfer_qty": actual_transfer if fairness_pass else 0,
            }
        )

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

    warehouses = frappe.db.sql(
        """
        SELECT name FROM `tabWarehouse`
        WHERE is_group = 0
          AND disabled = 0
          AND name != %(central)s
          AND (warehouse_type = 'Transit' OR warehouse_type IS NULL OR warehouse_type = 'Store')
        ORDER BY name
    """,
        {"central": central_warehouse},
        as_dict=False,
    )

    result = [w[0] for w in warehouses]
    frappe.cache().set_value(cache_key, result, expires_in_sec=3600)
    return result


def _get_eligible_items(
    warehouse: str, central_warehouse: str, as_of: date, config: dict
) -> pd.DataFrame:
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
    rows = frappe.db.sql(
        """
        SELECT DISTINCT
            i.item_code,
            i.item_name,
            i.stock_uom AS uom,
            i.custom_safety_days AS custom_safety_days,  -- NULL = use system default
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
    """,
        {"warehouse": warehouse, "from_date": str(from_date), "as_of": str(as_of)},
        as_dict=True,
    )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(_to_records(rows))
    df = df[df["excluded"] == 0].drop(columns=["excluded"])
    return df


def _get_bulk_sales(
    item_codes: list, warehouse: str, as_of: date, history_days: int
) -> pd.DataFrame:
    """Bulk fetch 30-day outbound qty for all items in one query."""
    if not item_codes:
        return pd.DataFrame(columns=["item_code", "sales_30d"])

    from_date = as_of - timedelta(days=history_days)

    # Process in batches to avoid MySQL IN clause limits
    all_rows = []
    batch_size = 1000
    for i in range(0, len(item_codes), batch_size):
        batch = item_codes[i : i + batch_size]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = frappe.db.sql(
            f"""
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
        """,
            tuple(batch) + (warehouse, str(from_date), str(as_of)),
            as_dict=True,
        )
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=["item_code", "sales_30d"])

    df = pd.DataFrame(_to_records(all_rows))
    return df


def _get_bulk_onhand(item_codes: list, warehouse: str) -> pd.DataFrame:
    """Bulk fetch current on-hand quantity from Bin."""
    if not item_codes:
        return pd.DataFrame(columns=["item_code", "current_onhand"])

    all_rows = []
    batch_size = 1000
    for i in range(0, len(item_codes), batch_size):
        batch = item_codes[i : i + batch_size]
        placeholders = ", ".join(["%s"] * len(batch))
        rows = frappe.db.sql(
            f"""
            SELECT item_code, actual_qty AS current_onhand
            FROM `tabBin`
            WHERE item_code IN ({placeholders})
              AND warehouse = %s
        """,
            tuple(batch) + (warehouse,),
            as_dict=True,
        )
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=["item_code", "current_onhand"])

    return pd.DataFrame(_to_records(all_rows))


def _get_bulk_intransit(
    item_codes: list, warehouse: str, as_of: date, lead_time_days: int
) -> pd.DataFrame:
    """
    Bulk fetch usable in-transit stock heading TO this store warehouse.

    ERPNext multi-step transit flow (Add to Transit / End Transit):
      SE-Add  : Source W1  → t_warehouse = 'Goods In Transit'  (NOT the store)
      SE-End  : Source GIT → t_warehouse = store S1             (receipt at store)

    Because SE-Add targets the GIT warehouse (not S1), filtering on
    seid.t_warehouse = S1 misses all physically-in-transit stock.

    CORRECT FORMULA
    ───────────────
    usable_intransit(item, S1) =
        MR_total_qty                         -- what the MR originally asked for
      - already_received_at_S1               -- End-Transit SEs already submitted to S1
      - qty_that_wont_come (MR cancelled)    -- ignored: cancelled MRs excluded

    This single formula is correct at EVERY step of the workflow:

      Step 0  MR raised, nothing dispatched         : 5 - 0      = 5  ✓
      Step 1  SE-Add dispatches 3 to GIT            : 5 - 0      = 5  ✓  (3 in GIT + 2 unshipped)
      Step 2  SE-End receives 2 at S1               : 5 - 2      = 3  ✓  (1 in GIT + 2 unshipped)
      Step 3a SE-Add dispatches balance 2 to GIT    : 5 - 2      = 3  ✓  (3 in GIT + 0 unshipped)
      Step 3b SE-End receives 1 at S1               : 5 - 3      = 2  ✓
      Step 4  SE-End receives final 2 at S1         : 5 - 5      = 0  ✓  (MR auto-closed)

    'already_received_at_S1' = SUM of submitted End-Transit Stock Entry qtys
    where t_warehouse = S1.  These are docstatus=1 SE of type Material Transfer
    whose s_warehouse is the Goods In Transit warehouse.
    """
    if not item_codes:
        return pd.DataFrame(columns=["item_code", "usable_intransit"])

    all_rows = []
    batch_size = 1000
    for i in range(0, len(item_codes), batch_size):
        batch = item_codes[i : i + batch_size]
        placeholders = ", ".join(["%s"] * len(batch))

        rows = frappe.db.sql(
            f"""
            SELECT
                mri.item_code,
                GREATEST(
                    SUM(mri.qty)
                    - IFNULL((
                        -- Qty already received at this store via End-Transit SEs
                        SELECT SUM(seid2.qty)
                        FROM `tabStock Entry Detail` seid2
                        JOIN `tabStock Entry` se2 ON se2.name = seid2.parent
                        WHERE seid2.item_code = mri.item_code
                          AND seid2.t_warehouse = %s
                          AND se2.stock_entry_type = 'Material Transfer'
                          AND se2.docstatus = 1
                          AND se2.add_to_transit = 0
                          AND se2.name IN (
                              SELECT se3.name
                              FROM `tabStock Entry` se3
                              JOIN `tabStock Entry Detail` seid3 ON seid3.parent = se3.name
                              WHERE seid3.item_code = mri.item_code
                                AND seid3.t_warehouse = %s
                                AND se3.docstatus = 1
                          )
                    ), 0),
                    0
                ) AS usable_intransit
            FROM `tabMaterial Request Item` mri
            JOIN `tabMaterial Request` mr ON mr.name = mri.parent
            WHERE mri.item_code IN ({placeholders})
              AND mri.warehouse = %s
              AND mr.material_request_type = 'Material Transfer'
              AND mr.docstatus = 1
              AND mr.status NOT IN ('Cancelled', 'Stopped', 'Ordered')
            GROUP BY mri.item_code
        """,
            (warehouse, warehouse) + tuple(batch) + (warehouse,),
            as_dict=True,
        )
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=["item_code", "usable_intransit"])

    return pd.DataFrame(_to_records(all_rows))


# ── Multi-warehouse helpers (for donor evaluation) ────────────────────────


def _get_bulk_sales_multi_warehouse(
    item_code: str, warehouses: list, as_of: date, history_days: int
) -> dict:
    """Returns {warehouse: sales_qty}"""
    if not warehouses:
        return {}
    from_date = as_of - timedelta(days=history_days)
    placeholders = ", ".join(["%s"] * len(warehouses))
    rows = frappe.db.sql(
        f"""
        SELECT warehouse, ABS(SUM(actual_qty)) AS sales_30d
        FROM `tabStock Ledger Entry`
        WHERE item_code = %s
          AND warehouse IN ({placeholders})
          AND voucher_type IN ('Delivery Note', 'Sales Invoice', 'POS Invoice')
          AND posting_date BETWEEN %s AND %s
          AND actual_qty < 0
        GROUP BY warehouse
    """,
        (item_code,) + tuple(warehouses) + (str(from_date), str(as_of)),
        as_dict=True,
    )
    return {r["warehouse"]: r["sales_30d"] for r in rows}


def _get_bulk_onhand_multi_warehouse(item_code: str, warehouses: list) -> dict:
    if not warehouses:
        return {}
    placeholders = ", ".join(["%s"] * len(warehouses))
    rows = frappe.db.sql(
        f"""
        SELECT warehouse, actual_qty
        FROM `tabBin`
        WHERE item_code = %s AND warehouse IN ({placeholders})
    """,
        (item_code,) + tuple(warehouses),
        as_dict=True,
    )
    return {r["warehouse"]: r["actual_qty"] for r in rows}


def _get_bulk_intransit_multi_warehouse(
    item_code: str, warehouses: list, as_of: date, lead_time_days: int
) -> dict:
    """
    Same MR-minus-received formula as _get_bulk_intransit, for multiple warehouses.
    Used during donor store evaluation.
    """
    if not warehouses:
        return {}
    placeholders = ", ".join(["%s"] * len(warehouses))
    rows = frappe.db.sql(
        f"""
        SELECT
            mri.warehouse,
            GREATEST(
                SUM(mri.qty)
                - IFNULL((
                    SELECT SUM(seid2.qty)
                    FROM `tabStock Entry Detail` seid2
                    JOIN `tabStock Entry` se2 ON se2.name = seid2.parent
                    WHERE seid2.item_code = %s
                      AND seid2.t_warehouse = mri.warehouse
                      AND se2.stock_entry_type = 'Material Transfer'
                      AND se2.docstatus = 1
                      AND se2.add_to_transit = 0
                ), 0),
                0
            ) AS qty
        FROM `tabMaterial Request Item` mri
        JOIN `tabMaterial Request` mr ON mr.name = mri.parent
        WHERE mri.item_code = %s
          AND mri.warehouse IN ({placeholders})
          AND mr.material_request_type = 'Material Transfer'
          AND mr.docstatus = 1
          AND mr.status NOT IN ('Cancelled', 'Stopped', 'Ordered')
        GROUP BY mri.warehouse
    """,
        (item_code, item_code) + tuple(warehouses),
        as_dict=True,
    )
    return {r["warehouse"]: float(r["qty"] or 0) for r in rows}


def _get_single_item_sales(
    item_code: str, warehouse: str, as_of: date, history_days: int
) -> float:
    from_date = as_of - timedelta(days=history_days)
    result = frappe.db.sql(
        """
        SELECT ABS(SUM(actual_qty)) AS qty
        FROM `tabStock Ledger Entry`
        WHERE item_code = %s AND warehouse = %s
          AND voucher_type IN ('Delivery Note', 'Sales Invoice', 'POS Invoice')
          AND posting_date BETWEEN %s AND %s
          AND actual_qty < 0
    """,
        (item_code, warehouse, str(from_date), str(as_of)),
    )
    return float(result[0][0] or 0)


def _get_single_item_onhand(item_code: str, warehouse: str) -> float:
    result = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
    )
    return float(result or 0)


def _get_single_item_intransit(
    item_code: str, warehouse: str, as_of: date, lead_time_days: int
) -> float:
    """Same MR-minus-received formula for a single item/warehouse (used by allocator agent)."""
    result = frappe.db.sql(
        """
        SELECT GREATEST(
            SUM(mri.qty)
            - IFNULL((
                SELECT SUM(seid2.qty)
                FROM `tabStock Entry Detail` seid2
                JOIN `tabStock Entry` se2 ON se2.name = seid2.parent
                WHERE seid2.item_code = %s
                  AND seid2.t_warehouse = %s
                  AND se2.stock_entry_type = 'Material Transfer'
                  AND se2.docstatus = 1
                  AND se2.add_to_transit = 0
            ), 0),
            0
        )
        FROM `tabMaterial Request Item` mri
        JOIN `tabMaterial Request` mr ON mr.name = mri.parent
        WHERE mri.item_code = %s
          AND mri.warehouse = %s
          AND mr.material_request_type = 'Material Transfer'
          AND mr.docstatus = 1
          AND mr.status NOT IN ('Cancelled', 'Stopped', 'Ordered')
    """,
        (item_code, warehouse, item_code, warehouse),
    )
    return float(result[0][0] or 0)

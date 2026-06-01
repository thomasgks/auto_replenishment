"""
auto_replenishment/utils/allocation_engine.py

Cross-store allocation engine — Phase 6.

For each item × store:
  1. Determine available stock (Central WH + donor store surplus)
  2. Rank requesting stores by selling_rate DESC
  3. Apply algorithm (Winner Takes All / Pro-rata / DOS Equalisation)
  4. Write one Replenishment Allocation row per source warehouse per store
  5. Update Store Plan Item totals
  6. Update Run allocation summary
"""

import math
import frappe
from frappe import _
from frappe.utils import now_datetime


# ── Public entry point ────────────────────────────────────────────────────────

def run_allocation(run_name: str) -> dict:
    run_doc = frappe.get_doc("Replenishment Run", run_name)
    config  = _get_config()

    algo = run_doc.allocation_algorithm or "Use Config Default"
    if algo == "Use Config Default":
        algo = config.get("allocation_algorithm", "Pro-rata")

    frappe.logger().info(f"[AR Allocation] Run={run_name}  Algorithm={algo}")

    # Load all store plans for this run
    store_plans = frappe.get_all(
        "Replenishment Store Plan",
        filters={"replenishment_run": run_name},
        fields=["name", "warehouse"],
    )
    if not store_plans:
        frappe.throw(_("No Store Plans found for Run {0}").format(run_name))

    plan_names = [p.name for p in store_plans]

    # Clear existing allocation rows on the Run (re-run support)
    frappe.db.sql(
        "DELETE FROM `tabReplenishment Allocation` WHERE parent = %s",
        run_name
    )
    frappe.db.commit()

    # Also clear allocation rows on each Store Plan
    for plan in store_plans:
        frappe.db.sql(
            "DELETE FROM `tabReplenishment Allocation` WHERE parent = %s",
            plan.name
        )
    frappe.db.commit()

    # Build demand matrix
    demand_matrix = _build_demand_matrix(plan_names)

    summary = {
        "full_supply":     0,
        "partial_supply":  0,
        "no_supply":       0,
        "items_processed": 0,
    }

    for item_code, demand_rows in demand_matrix.items():
        _allocate_item(
            item_code   = item_code,
            demand_rows = demand_rows,
            algo        = algo,
            config      = config,
            run_name    = run_name,
            summary     = summary,
        )
        summary["items_processed"] += 1

    _update_store_plan_summaries(plan_names)

    frappe.db.set_value("Replenishment Run", run_name, {
        "allocation_status":   "Complete",
        "allocation_at":       now_datetime(),
        "full_supply_count":   summary["full_supply"],
        "partial_supply_count": summary["partial_supply"],
        "no_supply_count":     summary["no_supply"],
    })

    for p in store_plans:
        frappe.db.set_value("Replenishment Store Plan", p.name, {
            "allocation_status": "Received",
            "status": "Allocation Complete",
        })
    frappe.db.commit()

    return summary


# ── Demand matrix ─────────────────────────────────────────────────────────────

def _build_demand_matrix(plan_names: list) -> dict:
    if not plan_names:
        return {}
    placeholders = ", ".join(["%s"] * len(plan_names))
    rows = frappe.db.sql(f"""
        SELECT
            i.name            AS item_row_name,
            i.parent          AS plan_name,
            i.item_code,
            i.item_name,
            i.uom,
            i.selling_rate,
            i.target_stock,
            i.current_onhand,
            i.effective_onhand,
            i.forecasted_requirement  AS required_qty,
            p.warehouse       AS store_warehouse
        FROM `tabReplenishment Store Plan Item` i
        JOIN `tabReplenishment Store Plan` p ON p.name = i.parent
        WHERE i.parent IN ({placeholders})
          AND i.forecasted_requirement > 0
        ORDER BY i.item_code, i.selling_rate DESC
    """, plan_names, as_dict=True)

    matrix = {}
    for row in rows:
        matrix.setdefault(row.item_code, []).append(row)
    return matrix


# ── Per-item allocation ───────────────────────────────────────────────────────

def _allocate_item(item_code, demand_rows, algo, config, run_name, summary):
    central_wh    = config["central_warehouse"]
    central_qty   = _get_available_qty(item_code, central_wh)
    donor_details = _get_donor_store_details(item_code, demand_rows, config)
    donor_surplus = sum(d["surplus"] for d in donor_details)
    total_available = max(0, central_qty) + donor_surplus

    ranked = sorted(demand_rows, key=lambda r: float(r.selling_rate or 0), reverse=True)
    total_available_snapshot = total_available  # snapshot before allocation

    # Build allocation log
    def fmt(n):
        return str(int(n)) if n == int(n) else f"{n:.1f}"

    log = []
    log.append("═" * 64)
    log.append(f"ITEM: {item_code}  {(demand_rows[0].item_name or '')}")
    log.append("═" * 64)
    log.append(f"  Central WH Stock  : {fmt(central_qty)}  ({central_wh})")
    log.append(f"  Donor Surplus     : {fmt(donor_surplus)}")
    for ds in donor_details:
        log.append(f"    ↳ {ds['warehouse']:<36} surplus={fmt(ds['surplus'])}")
    log.append(f"  Total Available   : {fmt(total_available)}")
    log.append(f"  Algorithm         : {algo}")
    log.append(f"  Competing Stores  : {len(ranked)}")
    log.append("")
    log.append(f"  {'Store':<35} {'Rate':>6} {'Need':>5}")
    log.append("  " + "─" * 50)
    for r in ranked:
        log.append(f"  {r.store_warehouse:<35} {float(r.selling_rate or 0):>6.4f} {fmt(float(r.required_qty or 0)):>5}")
    log.append("")

    # Run algorithm
    if algo == "Winner Takes All":
        allocations = _winner_takes_all(ranked, total_available, config)
    elif algo == "DOS Equalisation":
        allocations = _dos_equalisation(ranked, total_available, config)
    else:
        allocations = _pro_rata(ranked, total_available, config)

    # Calculate total allocated for source breakdown
    total_allocated = sum(a["allocated_qty"] for a in allocations)

    # Build source breakdown per allocation
    for alloc in allocations:
        alloc["sources"] = _split_sources(
            alloc["allocated_qty"], central_qty, central_wh, donor_details
        )

    # Build result log
    log.append("▶ ALLOCATION RESULT")
    for alloc in allocations:
        store  = alloc["store_warehouse"]
        aqty   = alloc["allocated_qty"]
        short  = alloc["shortage_qty"]
        status = alloc["supply_status"]
        log.append(f"  Store: {store}")
        log.append(f"    Allocated={fmt(aqty)}  Shortage={fmt(short)}  Status={status}")
        for src in alloc["sources"]:
            log.append(f"    ↳ {src['warehouse']:<36} qty={fmt(src['qty'])}")

        if status == "Full Supply":    summary["full_supply"]    += 1
        elif status == "Partial Supply": summary["partial_supply"] += 1
        else:                           summary["no_supply"]      += 1

    log_text = "\n".join(log)

    # Write Replenishment Allocation rows to Run + Store Plan
    # Also update Store Plan Item totals
    for alloc in allocations:
        plan_name = alloc["plan_name"]

        # Update Store Plan Item
        item_total_alloc = alloc["allocated_qty"]
        item_total_short = alloc["shortage_qty"]
        # Primary source for Store Plan Item display
        primary_src = alloc["sources"][0]["warehouse"] if alloc["sources"] else central_wh

        frappe.db.set_value(
            "Replenishment Store Plan Item",
            alloc["item_row_name"],
            {
                "allocated_qty":    item_total_alloc,
                "shortage_qty":     item_total_short,
                "supply_status":    alloc["supply_status"],
                "source_warehouse": primary_src,
                "evaluation_log":   log_text,
            },
            update_modified=False,
        )

        # Write one Replenishment Allocation row per source (only if qty > 0)
        for src in alloc["sources"]:
            if src["qty"] <= 0:
                continue
            _insert_allocation_row(
                parent        = run_name,
                parenttype    = "Replenishment Run",
                parentfield   = "allocations",
                item_code     = item_code,
                item_name     = demand_rows[0].item_name or "",
                store_warehouse = alloc["store_warehouse"],
                store_plan    = plan_name,
                source_warehouse = src["warehouse"],
                source_type   = src["source_type"],
                available_qty = src["available"],
                suggested_qty = src["qty"],
                plan_item_ref = alloc["item_row_name"],
            )
            # Also write to Store Plan (transfer rows only, not shortage)
            _insert_allocation_row(
                parent        = plan_name,
                parenttype    = "Replenishment Store Plan",
                parentfield   = "allocations",
                item_code     = item_code,
                item_name     = demand_rows[0].item_name or "",
                store_warehouse = alloc["store_warehouse"],
                store_plan    = plan_name,
                source_warehouse = src["warehouse"],
                source_type   = src["source_type"],
                available_qty = src["available"],
                suggested_qty = src["qty"],
                plan_item_ref = alloc["item_row_name"],
            )

        # Write shortage row to Run only (not duplicated to Store Plan)
        if alloc["shortage_qty"] > 0:
            # Determine shortage reason
            if total_available_snapshot <= 0:
                # No stock anywhere from the start
                reason = "Zero Stock Available"
            elif alloc["allocated_qty"] == 0:
                # Had stock but this store got none — another store outranked it
                reason = "Stock Exhausted by Higher Priority Store"
            elif alloc["allocated_qty"] < alloc["required_qty"]:
                # Store got SOME stock but total available was just not enough
                # (not a ranking issue — store got everything available)
                # Distinguish: did other competing stores also receive allocation?
                competing_allocated = sum(
                    a["allocated_qty"] for a in allocations
                    if a["store_warehouse"] != alloc["store_warehouse"]
                    and a["allocated_qty"] > 0
                )
                if competing_allocated > 0:
                    # Other stores took some stock before this store
                    reason = "Stock Exhausted by Higher Priority Store"
                else:
                    # This store got all available stock — just not enough
                    reason = "Insufficient Stock Available"
            else:
                reason = "Zero Stock Available"

            _insert_allocation_row(
                parent          = run_name,
                parenttype      = "Replenishment Run",
                parentfield     = "allocations",
                item_code       = item_code,
                item_name       = demand_rows[0].item_name or "",
                store_warehouse = alloc["store_warehouse"],
                store_plan      = plan_name,
                source_warehouse = "",
                source_type     = "Shortage",
                available_qty   = 0,
                suggested_qty   = alloc["shortage_qty"],
                plan_item_ref   = alloc["item_row_name"],
                shortage_reason = reason,
            )

    frappe.db.commit()


def _insert_allocation_row(parent, parenttype, parentfield,
                            item_code, item_name, store_warehouse,
                            store_plan, source_warehouse, source_type,
                            available_qty, suggested_qty, plan_item_ref,
                            shortage_reason=""):
    """Insert one row into tabReplenishment Allocation."""
    name = frappe.generate_hash(length=10)
    frappe.db.sql("""
        INSERT INTO `tabReplenishment Allocation`
            (name, creation, modified, modified_by, owner,
             parent, parentfield, parenttype, idx,
             item_code, item_name, forecast_item_ref,
             store_warehouse, store_plan,
             source_warehouse, source_type,
             available_qty, suggested_qty, override_qty,
             fairness_pass, excluded, exclusion_reason, shortage_reason)
        VALUES
            (%s, NOW(), NOW(), 'Administrator', 'Administrator',
             %s, %s, %s,
             (SELECT IFNULL(MAX(idx),0)+1 FROM `tabReplenishment Allocation`
              WHERE parent = %s),
             %s, %s, %s,
             %s, %s,
             %s, %s,
             %s, %s, 0,
             1, 0, %s, %s)
    """, (name, parent, parentfield, parenttype, parent,
          item_code, item_name, plan_item_ref,
          store_warehouse, store_plan,
          source_warehouse, source_type,
          available_qty, suggested_qty,
          shortage_reason, shortage_reason))


def _split_sources(needed, central_qty, central_wh, donor_details):
    """
    Split an allocation across Central WH and donor stores.
    Returns list of {warehouse, qty, source_type, available}.
    """
    sources = []
    remaining = needed

    if central_qty > 0 and remaining > 0:
        take = min(central_qty, remaining)
        if take > 0:
            sources.append({
                "warehouse":   central_wh,
                "qty":         take,
                "source_type": "Central Warehouse",
                "available":   central_qty,
            })
            remaining -= take

    for donor in donor_details:
        if remaining <= 0:
            break
        take = min(donor["surplus"], remaining)
        if take > 0:
            sources.append({
                "warehouse":   donor["warehouse"],
                "qty":         take,
                "source_type": "Donor Store",
                "available":   donor["surplus"],
            })
            remaining -= take

    # Only return rows with actual qty > 0
    # If nothing allocated (zero stock), return empty list
    # The shortage row handles the "nothing available" case separately
    return [s for s in sources if s["qty"] > 0]


# ── Algorithms ────────────────────────────────────────────────────────────────

def _winner_takes_all(ranked, available, config):
    rounding = config.get("quantity_rounding", "Floor (Round Down)")
    results = []
    remaining = available
    for row in ranked:
        need     = float(row.required_qty or 0)
        give_raw = min(need, remaining)
        max_give = math.floor(remaining) if remaining >= 1 else remaining
        give     = min(_round_qty(give_raw, rounding), max_give)
        remaining = max(0, remaining - give)
        results.append(_alloc_row(row, give, need))
    return results


def _pro_rata(ranked, available, config):
    rounding   = config.get("quantity_rounding", "Floor (Round Down)")
    total_rate = sum(float(r.selling_rate or 0) for r in ranked) or 1
    results    = []
    for row in ranked:
        need     = float(row.required_qty or 0)
        weight   = float(row.selling_rate or 0) / total_rate
        fair     = available * weight
        max_give = math.floor(available) if available >= 1 else available
        give     = min(_round_qty(fair, rounding), max_give, need)
        results.append(_alloc_row(row, give, need))
    return results


def _dos_equalisation(ranked, available, config):
    rounding = config.get("quantity_rounding", "Floor (Round Down)")
    stores = [{
        "item_row_name":   r.item_row_name,
        "plan_name":       r.plan_name,
        "store_warehouse": r.store_warehouse,
        "selling_rate":    float(r.selling_rate or 0.001),
        "required_qty":    float(r.required_qty or 0),
        "current_dos":     float(r.effective_onhand or 0) / max(float(r.selling_rate or 0.001), 0.001),
        "effective_onhand": float(r.effective_onhand or 0),
    } for r in ranked]

    min_dos = min(s["current_dos"] for s in stores)
    max_dos = min_dos + available / (sum(s["selling_rate"] for s in stores) or 1) + 1

    for _ in range(50):
        mid = (min_dos + max_dos) / 2
        total_needed = sum(max(0, (mid - s["current_dos"]) * s["selling_rate"]) for s in stores)
        if abs(total_needed - available) < 0.01:
            break
        if total_needed > available:
            max_dos = mid
        else:
            min_dos = mid

    results = []
    for s in stores:
        ideal    = max(0, (mid - s["current_dos"]) * s["selling_rate"])
        max_give = math.floor(available) if available >= 1 else available
        give     = min(_round_qty(ideal, rounding), max_give, s["required_qty"])
        results.append({
            "item_row_name":   s["item_row_name"],
            "plan_name":       s["plan_name"],
            "store_warehouse": s["store_warehouse"],
            "required_qty":    s["required_qty"],
            "allocated_qty":   give,
            "shortage_qty":    max(0, s["required_qty"] - give),
            "supply_status":   _supply_status(give, s["required_qty"]),
            "effective_onhand": s["effective_onhand"],
            "sources":         [],
        })
    return results


def _alloc_row(row, give, need):
    return {
        "item_row_name":   row.item_row_name,
        "plan_name":       row.plan_name,
        "store_warehouse": row.store_warehouse,
        "required_qty":    need,
        "allocated_qty":   give,
        "shortage_qty":    max(0, need - give),
        "supply_status":   _supply_status(give, need),
        "effective_onhand": float(row.effective_onhand or 0),
        "sources":         [],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_available_qty(item_code, warehouse):
    result = frappe.db.get_value("Bin",
        {"item_code": item_code, "warehouse": warehouse},
        ["actual_qty", "reserved_qty"], as_dict=True)
    if not result:
        return 0.0
    return float(math.floor(max(0, float(result.actual_qty or 0) -
                                   float(result.reserved_qty or 0))))


def _get_donor_store_details(item_code, demand_rows, config):
    requesting = {r.store_warehouse for r in demand_rows}
    central_wh = config["central_warehouse"]
    protection = config.get("protection_days", 5)

    rows = frappe.db.sql("""
        SELECT b.warehouse, b.actual_qty, b.reserved_qty
        FROM `tabBin` b
        JOIN `tabWarehouse` w ON w.name = b.warehouse
        WHERE b.item_code = %s
          AND b.warehouse != %s
          AND b.actual_qty > b.reserved_qty
          AND (
              w.custom_replenishment_role NOT IN
                  ('Exclude', 'Goods In Transit', 'Central Warehouse')
              OR w.custom_replenishment_role IS NULL
              OR w.custom_replenishment_role = ''
          )
    """, (item_code, central_wh), as_dict=True)

    result = []
    for s in rows:
        if s.warehouse in requesting:
            continue
        available = max(0, float(s.actual_qty or 0) - float(s.reserved_qty or 0))
        rate = frappe.db.get_value(
            "Replenishment Store Plan Item",
            {"item_code": item_code, "parent": ["like", "RSP%"]},
            "selling_rate") or 0
        protection_qty = float(rate or 0) * protection
        surplus = float(math.floor(max(0, available - protection_qty)))
        if surplus > 0:
            result.append({"warehouse": s.warehouse, "surplus": surplus})

    return sorted(result, key=lambda x: x["surplus"], reverse=True)


def _update_store_plan_summaries(plan_names):
    for plan_name in plan_names:
        counts = frappe.db.sql("""
            SELECT
                SUM(CASE WHEN supply_status='Full Supply'    THEN 1 ELSE 0 END) full_s,
                SUM(CASE WHEN supply_status='Partial Supply' THEN 1 ELSE 0 END) partial_s,
                SUM(CASE WHEN supply_status='No Supply'      THEN 1 ELSE 0 END) no_s
            FROM `tabReplenishment Store Plan Item`
            WHERE parent = %s AND forecasted_requirement > 0
        """, plan_name, as_dict=True)[0]
        frappe.db.set_value("Replenishment Store Plan", plan_name, {
            "full_supply_count":     counts.full_s    or 0,
            "partial_supply_count":  counts.partial_s or 0,
            "no_supply_count":       counts.no_s      or 0,
            "allocated_items_count": (counts.full_s or 0) + (counts.partial_s or 0),
            "shortage_items_count":  (counts.partial_s or 0) + (counts.no_s or 0),
        })


def _supply_status(allocated, required):
    if allocated <= 0:          return "No Supply"
    if allocated < required * 0.999: return "Partial Supply"
    return "Full Supply"


def _round_qty(qty, rounding):
    if qty <= 0: return 0.0
    if rounding == "Ceil (Round Up)":    return float(math.ceil(qty))
    elif rounding == "Round (Nearest)":  return float(round(qty, 0))
    elif rounding == "No Rounding":      return round(qty, 3)
    return float(math.floor(qty))  # Floor (default)


def _get_config():
    cfg = frappe.get_single("Replenishment Config")
    return {
        "demand_history_days":               cfg.demand_history_days or 30,
        "safety_days":                       cfg.safety_days or 7,
        "internal_intransit_lead_time_days": cfg.internal_intransit_lead_time_days or 3,
        "protection_days":                   cfg.protection_days or 5,
        "central_warehouse":                 cfg.central_warehouse,
        "quantity_rounding":                 cfg.quantity_rounding or "Floor (Round Down)",
        "allocation_algorithm":              getattr(cfg, "allocation_algorithm", "Pro-rata"),
        "enable_auto_allocation":            getattr(cfg, "enable_auto_allocation", 0),
        "auto_submit_mrs":                   getattr(cfg, "auto_submit_mrs", 0),
        "create_purchase_mrs":               getattr(cfg, "create_purchase_mrs", 1),
        "mr_per_supplier":                   getattr(cfg, "mr_per_supplier", 1),
    }
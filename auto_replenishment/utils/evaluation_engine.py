"""
auto_replenishment/utils/evaluation_engine.py

Step 1 — Evaluate Allocation
Runs the full allocation logic (warehouse + donor stores) for every item
in a Forecast but does NOT create any Material Requests.

Results are written back to the Forecast Item child rows:
  - fi.allocations   : child table of AR Forecast Allocation rows
  - fi.evaluation_log: plain-text trace of the evaluation logic
  - fi.supply_status : Pending → Full Supply / Partial Supply / No Supply
  - fi.allocated_qty / fi.shortage_qty

The Allocator then reviews the allocation plan, can:
  - Override qty on any source row
  - Exclude a source row entirely
  - Remove items that don't need replenishment now

Then clicks "Create Material Requests" (Step 2).
"""

import frappe
from frappe import _
from frappe.utils import now_datetime
from datetime import date
import math


def evaluate_allocation_for_forecast(forecast_name: str) -> dict:
    """
    Step 1: Evaluate allocation sources for all items in a forecast.
    Populates the allocation rows on the parent forecast document.
    Does NOT create any Material Requests.

    Returns summary dict for the UI.
    """
    # Lazy imports — avoids circular import since forecast_engine imports nothing
    # from this module, but importing at module level can cause partial-init errors
    from auto_replenishment.utils.forecast_engine import (
        evaluate_donor_stores,
        _get_single_item_onhand,
        _get_single_item_intransit,
    )

    forecast_doc = frappe.get_doc("Replenishment Store Plan", forecast_name)
    config = _get_config()
    as_of = date.today()

    summary = {
        "full_supply": 0,
        "partial_supply": 0,
        "no_supply": 0,
        "no_longer_required": 0,
        "items_evaluated": 0,
        "total_sources": 0,
    }

    for fi in forecast_doc.items:
        # Skip items already excluded or having no requirement
        if fi.forecasted_requirement <= 0:
            continue

        item_code = fi.item_code
        log_lines = []
        log_lines.append(f"{'═'*60}")
        log_lines.append(f"ITEM: {item_code}  {fi.item_name or ''}")
        log_lines.append(f"{'═'*60}")

        # ── Real-time stock snapshot ─────────────────────────────────────
        log_lines.append("▶ LIVE STOCK SNAPSHOT")
        live_onhand = _get_single_item_onhand(item_code, forecast_doc.warehouse)
        live_intransit = _get_single_item_intransit(
            item_code,
            forecast_doc.warehouse,
            as_of,
            config.get("internal_intransit_lead_time_days", 3),
        )
        live_effective = max(0, live_onhand) + max(0, live_intransit)
        live_target = float(fi.target_stock or 0)
        live_requirement = max(0, live_target - live_effective)

        log_lines.append(f"  Current OnHand    : {live_onhand}")
        log_lines.append(f"  Usable In-Transit : {live_intransit}")
        log_lines.append(f"  Effective OnHand  : {live_effective}")
        log_lines.append(f"  Target Stock      : {live_target}")
        log_lines.append(f"  Live Requirement  : {live_requirement}")

        if live_requirement <= 0:
            log_lines.append("  → No longer required (stock covers target). Skipping.")
            _save_item_evaluation(
                fi, [], "No Longer Required", 0, 0, "\n".join(log_lines)
            )
            summary["no_longer_required"] += 1
            continue

        gap = live_requirement
        allocation_rows = []
        log_lines.append("")

        # ── Central Warehouse ────────────────────────────────────────────
        log_lines.append("▶ STEP 1 — CENTRAL WAREHOUSE CHECK")
        wh = config["central_warehouse"]
        wh_result = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": wh},
            ["actual_qty", "reserved_qty"],
            as_dict=True,
        )
        wh_actual = float(wh_result.get("actual_qty", 0)) if wh_result else 0
        wh_reserved = float(wh_result.get("reserved_qty", 0)) if wh_result else 0
        wh_available = max(0, wh_actual - wh_reserved)

        log_lines.append(f"  Warehouse         : {wh}")
        log_lines.append(f"  Actual Qty        : {wh_actual}")
        log_lines.append(f"  Reserved Qty      : {wh_reserved}")
        log_lines.append(f"  Available Qty     : {wh_available}")

        if wh_available > 0:
            wh_alloc = _apply_rounding(min(wh_available, gap), config)
            gap = max(0, gap - wh_alloc)
            log_lines.append(
                f"  Allocating        : {wh_alloc}  (gap remaining: {gap})"
            )
            allocation_rows.append(
                {
                    "source_type": "Central Warehouse",
                    "source_warehouse": wh,
                    "available_qty": wh_available,
                    "dos_before": 9999,
                    "dos_after": 9999,
                    "suggested_qty": wh_alloc,
                    "override_qty": 0,
                    "fairness_pass": 1,
                    "excluded": 0,
                }
            )
        else:
            log_lines.append("  No available stock in central warehouse.")

        # ── Donor Stores ─────────────────────────────────────────────────
        if gap > 0:
            log_lines.append("")
            log_lines.append(f"▶ STEP 2 — DONOR STORE EVALUATION  (gap = {gap})")
            donors = evaluate_donor_stores(
                item_code, forecast_doc.warehouse, gap, config, as_of
            )

            if not donors:
                log_lines.append("  No eligible donor stores found.")
            else:
                log_lines.append(f"  Evaluated {len(donors)} candidate stores:")
                log_lines.append(
                    f"  {'Store':<35} {'Avail':>7} {'DOS-B':>6} {'DOS-A':>6} "
                    f"{'Suggest':>8} {'Fair':>5}"
                )
                log_lines.append("  " + "─" * 72)

                for donor in donors:
                    fair_str = "✓ YES" if donor["fairness_pass"] else "✗ NO"
                    log_lines.append(
                        f"  {donor['warehouse']:<35} "
                        f"{donor['effective_onhand']:>7.1f} "
                        f"{donor['dos']:>6.1f} "
                        f"{donor['dos_after_transfer']:>6.1f} "
                        f"{donor['actual_transfer_qty']:>8.1f} "
                        f"{fair_str:>5}"
                    )

                    # Only allocate from donors that pass fairness
                    if (
                        donor["fairness_pass"]
                        and donor["actual_transfer_qty"] > 0
                        and gap > 0
                    ):
                        take = _apply_rounding(
                            min(donor["actual_transfer_qty"], gap), config
                        )
                        gap = max(0, gap - take)
                        allocation_rows.append(
                            {
                                "source_type": "Donor Store",
                                "source_warehouse": donor["warehouse"],
                                "available_qty": donor["effective_onhand"],
                                "dos_before": round(donor["dos"], 1),
                                "dos_after": round(donor["dos_after_transfer"], 1),
                                "suggested_qty": take,
                                "override_qty": 0,
                                "fairness_pass": 1,
                                "excluded": 0,
                            }
                        )
                    elif not donor["fairness_pass"]:
                        # Show failed donors as excluded rows so allocator can see why
                        allocation_rows.append(
                            {
                                "source_type": "Donor Store",
                                "source_warehouse": donor["warehouse"],
                                "available_qty": donor["effective_onhand"],
                                "dos_before": round(donor["dos"], 1),
                                "dos_after": round(donor["dos_after_transfer"], 1),
                                "suggested_qty": 0,
                                "override_qty": 0,
                                "fairness_pass": 0,
                                "excluded": 1,
                                "exclusion_reason": "DOS fairness check failed",
                            }
                        )

        # ── Supply status determination ──────────────────────────────────
        total_alloc = sum(
            r["override_qty"] if r["override_qty"] > 0 else r["suggested_qty"]
            for r in allocation_rows
            if not r["excluded"]
        )

        log_lines.append("")
        log_lines.append("▶ ALLOCATION SUMMARY")
        log_lines.append(f"  Total Required    : {live_requirement}")
        log_lines.append(f"  Total Allocated   : {total_alloc}")
        log_lines.append(f"  Remaining Gap     : {gap}")

        for row in allocation_rows:
            if not row["excluded"]:
                log_lines.append(
                    f"  Source: {row['source_warehouse']}  "
                    f"→ {row['suggested_qty']} units"
                )

        if total_alloc <= 0:
            supply_status = "No Supply"
            summary["no_supply"] += 1
            log_lines.append("  Status: NO SUPPLY")
        elif gap > 0.001:
            supply_status = "Partial Supply"
            summary["partial_supply"] += 1
            log_lines.append(f"  Status: PARTIAL SUPPLY  (shortage: {round(gap, 2)})")
        else:
            supply_status = "Full Supply"
            summary["full_supply"] += 1
            log_lines.append("  Status: FULL SUPPLY ✓")

        _save_item_evaluation(
            fi, allocation_rows, supply_status, total_alloc, gap, "\n".join(log_lines)
        )
        summary["items_evaluated"] += 1
        summary["total_sources"] += len(
            [r for r in allocation_rows if not r["excluded"]]
        )

    # Update forecast status
    forecast_doc.db_set("evaluation_status", "Evaluation Complete")
    forecast_doc.db_set("evaluation_at", now_datetime())
    forecast_doc.db_set("full_supply_count", summary["full_supply"])
    forecast_doc.db_set("partial_supply_count", summary["partial_supply"])
    forecast_doc.db_set("no_supply_count", summary["no_supply"])

    frappe.db.commit()
    return summary


def _save_item_evaluation(
    fi,
    allocation_rows: list,
    supply_status: str,
    allocated_qty: float,
    shortage_qty: float,
    log_text: str,
):
    """
    Save evaluation results.
    Allocation rows go to the PARENT Forecast document (not the child item)
    because Frappe does not support nested child tables.
    """
    # Clear existing allocation rows for this item on the parent forecast
    frappe.db.delete(
        "Replenishment Allocation",
        {
            "parent": fi.parent,
            "forecast_item_ref": fi.name,
        },
    )

    # Insert new allocation rows under the parent Forecast document
    for idx, row in enumerate(allocation_rows, 1):
        alloc = frappe.new_doc("Replenishment Allocation")
        alloc.update(
            {
                "parent": fi.parent,
                "parentfield": "allocations",
                "parenttype": "Replenishment Store Plan",
                "idx": idx,
                "item_code": fi.item_code,
                "item_name": fi.item_name or "",
                "forecast_item_ref": fi.name,
                "source_type": row["source_type"],
                "source_warehouse": row["source_warehouse"],
                "available_qty": row["available_qty"],
                "dos_before": row["dos_before"],
                "dos_after": row["dos_after"],
                "suggested_qty": row["suggested_qty"],
                "override_qty": row.get("override_qty", 0),
                "fairness_pass": row.get("fairness_pass", 1),
                "excluded": row.get("excluded", 0),
                "exclusion_reason": row.get("exclusion_reason", ""),
            }
        )
        alloc.db_insert()

    # Update forecast item fields (safe — no nested table)
    fi.db_set("supply_status", supply_status)
    fi.db_set("allocated_qty", allocated_qty)
    fi.db_set("shortage_qty", max(0, shortage_qty))
    # Store plain text log — the HTML field renders it via JS
    # Truncate to 10000 chars to stay within DB field limits
    fi.db_set("evaluation_log", log_text[:10000] if log_text else "")


def _apply_rounding(qty: float, config: dict) -> float:
    """Apply quantity rounding from config."""
    rounding = config.get("quantity_rounding", "Ceil (Round Up)")
    if rounding == "Ceil (Round Up)":
        return float(math.ceil(qty))
    elif rounding == "Round (Nearest)":
        return float(round(qty, 0))
    return round(qty, 2)


def _get_config() -> dict:
    try:
        cfg = frappe.get_single("Replenishment Config")
        return {
            "demand_history_days": cfg.demand_history_days or 30,
            "safety_days": cfg.safety_days or 7,
            "internal_intransit_lead_time_days": cfg.internal_intransit_lead_time_days
            or 3,
            "protection_days": cfg.protection_days or 5,
            "central_warehouse": cfg.central_warehouse,
            "batch_size": cfg.batch_size or 500,
            "quantity_rounding": cfg.quantity_rounding or "Ceil (Round Up)",
        }
    except Exception:
        frappe.throw(
            _("Replenishment Config is not set up. Please configure it first.")
        )

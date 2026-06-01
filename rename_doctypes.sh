#!/usr/bin/env bash
# =============================================================================
# rename_doctypes.sh  — place at apps/auto_replenishment/rename_doctypes.sh
# =============================================================================
# Verified against actual folder structure:
#   frappe-bench/apps/auto_replenishment/
#     auto_replenishment/          ← PKG_ROOT (hooks.py lives here)
#       auto_replenishment/
#         doctype/                 ← DOCTYPE_DIR
#           auto_replenishment_log/
#           auto_replenishment_log_store/
#           auto_replenishment_forecast/
#           auto_replenishment_forecast_item/
#           ar_forecast_allocation/
#           ar_forecast_material_request/
#           replenishment_config/
#       public/js/
#       tasks/
#       utils/
# =============================================================================

set -e

BENCH_DIR="/home/erpnext/frappe-bench"
PKG_ROOT="$BENCH_DIR/apps/auto_replenishment/auto_replenishment"
DOCTYPE_DIR="$PKG_ROOT/auto_replenishment/doctype"
PUBLIC_JS="$PKG_ROOT/public/js"

echo ""
echo "============================================================"
echo "  Replenishment Run — File System Rename"
echo "============================================================"
echo "  PKG_ROOT    : $PKG_ROOT"
echo "  DOCTYPE_DIR : $DOCTYPE_DIR"
echo ""

if [ ! -d "$DOCTYPE_DIR" ]; then
    echo "ERROR: $DOCTYPE_DIR not found. Check BENCH_DIR path in this script."
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# Helper: rename one doctype folder + files inside it
# ─────────────────────────────────────────────────────────────────────────────
rename_doctype() {
    local OLD="$1"  # folder name  e.g.  auto_replenishment_log
    local NEW="$2"  # folder name  e.g.  replenishment_run
    local OLDP="$DOCTYPE_DIR/$OLD"
    local NEWP="$DOCTYPE_DIR/$NEW"

    if [ -d "$NEWP" ] && [ ! -d "$OLDP" ]; then
        echo "  SKIP  $OLD (already renamed)"
        return
    fi
    if [ ! -d "$OLDP" ] && [ ! -d "$NEWP" ]; then
        echo "  MISS  $OLD (folder not found — skipping)"
        return
    fi

    # Rename folder
    [ -d "$OLDP" ] && mv "$OLDP" "$NEWP"

    # Rename .json and .py files
    [ -f "$NEWP/${OLD}.json" ] && mv "$NEWP/${OLD}.json" "$NEWP/${NEW}.json"
    [ -f "$NEWP/${OLD}.py"   ] && mv "$NEWP/${OLD}.py"   "$NEWP/${NEW}.py"

    # Ensure __init__.py (ar_forecast_material_request was missing it)
    [ ! -f "$NEWP/__init__.py" ] && touch "$NEWP/__init__.py"

    # Remove stale bytecode
    rm -rf "$NEWP/__pycache__"

    echo "  OK    $OLD  →  $NEW"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Rename doctype folders
# ─────────────────────────────────────────────────────────────────────────────
echo "[1/5] Renaming doctype folders..."
rename_doctype auto_replenishment_log          replenishment_run
rename_doctype auto_replenishment_log_store    replenishment_run_store
rename_doctype auto_replenishment_forecast     replenishment_store_plan
rename_doctype auto_replenishment_forecast_item replenishment_store_plan_item
rename_doctype ar_forecast_allocation          replenishment_allocation
rename_doctype ar_forecast_material_request    replenishment_mr_link

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Rename public JS files
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "[2/5] Renaming public JS files..."
rename_js() {
    local O="$PUBLIC_JS/$1" N="$PUBLIC_JS/$2"
    if   [ -f "$N" ] && [ ! -f "$O" ]; then echo "  SKIP  $1"
    elif [ -f "$O" ]; then mv "$O" "$N"; echo "  OK    $1  →  $2"
    else echo "  MISS  $1"
    fi
}
rename_js auto_replenishment_log.js      replenishment_run.js
rename_js auto_replenishment_forecast.js replenishment_store_plan.js

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Update Python content
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "[3/5] Updating Python file content..."

py_rep() {
    # Replace $1 with $2 in all .py files (skip __pycache__)
    find "$PKG_ROOT" -name "*.py" ! -path "*/__pycache__/*" | \
    xargs grep -rl "$1" 2>/dev/null | while IFS= read -r f; do
        sed -i "s|$1|$2|g" "$f"
    done
}

# DocType name string literals
py_rep '"Auto Replenishment Log"'             '"Replenishment Run"'
py_rep '"Auto Replenishment Log Store"'       '"Replenishment Run Store"'
py_rep '"Auto Replenishment Forecast"'        '"Replenishment Store Plan"'
py_rep '"Auto Replenishment Forecast Item"'   '"Replenishment Store Plan Item"'
py_rep '"AR Forecast Allocation"'             '"Replenishment Allocation"'
py_rep '"AR Forecast Material Request"'       '"Replenishment MR Link"'

# Module import paths
py_rep 'auto_replenishment.auto_replenishment.doctype.auto_replenishment_log.auto_replenishment_log' \
       'auto_replenishment.auto_replenishment.doctype.replenishment_run.replenishment_run'
py_rep 'auto_replenishment.doctype.auto_replenishment_log.auto_replenishment_log' \
       'auto_replenishment.doctype.replenishment_run.replenishment_run'
py_rep 'auto_replenishment.doctype.auto_replenishment_forecast.auto_replenishment_forecast' \
       'auto_replenishment.doctype.replenishment_store_plan.replenishment_store_plan'
py_rep 'auto_replenishment.doctype.auto_replenishment_log_store.auto_replenishment_log_store' \
       'auto_replenishment.doctype.replenishment_run_store.replenishment_run_store'
py_rep 'auto_replenishment.doctype.ar_forecast_allocation.ar_forecast_allocation' \
       'auto_replenishment.doctype.replenishment_allocation.replenishment_allocation'
py_rep 'auto_replenishment.doctype.ar_forecast_material_request.ar_forecast_material_request' \
       'auto_replenishment.doctype.replenishment_mr_link.replenishment_mr_link'

# Class names
py_rep 'class AutoReplenishmentLog('           'class ReplenishmentRun('
py_rep 'class AutoReplenishmentLogStore('      'class ReplenishmentRunStore('
py_rep 'class AutoReplenishmentForecast('      'class ReplenishmentStorePlan('
py_rep 'class AutoReplenishmentForecastItem('  'class ReplenishmentStorePlanItem('
py_rep 'class ARForecastAllocation('           'class ReplenishmentAllocation('
py_rep 'class ARForecastMaterialRequest('      'class ReplenishmentMRLink('

echo "  OK    Python updated"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Update JSON content
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "[4/5] Updating JSON file content..."

json_rep() {
    find "$PKG_ROOT" -name "*.json" | \
    xargs grep -rl "$1" 2>/dev/null | while IFS= read -r f; do
        sed -i "s|$1|$2|g" "$f"
    done
}

json_rep '"Auto Replenishment Log"'             '"Replenishment Run"'
json_rep '"Auto Replenishment Log Store"'       '"Replenishment Run Store"'
json_rep '"Auto Replenishment Forecast"'        '"Replenishment Store Plan"'
json_rep '"Auto Replenishment Forecast Item"'   '"Replenishment Store Plan Item"'
json_rep '"AR Forecast Allocation"'             '"Replenishment Allocation"'
json_rep '"AR Forecast Material Request"'       '"Replenishment MR Link"'

echo "  OK    JSON updated"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Update JS files + hooks.py
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "[5/5] Updating JS files and hooks.py..."

# JS files
find "$PUBLIC_JS" -name "*.js" 2>/dev/null | \
xargs grep -rl 'Auto Replenishment' 2>/dev/null | while IFS= read -r f; do
    sed -i \
        -e 's|Auto Replenishment Log|Replenishment Run|g' \
        -e 's|Auto Replenishment Forecast|Replenishment Store Plan|g' \
        "$f"
done

# hooks.py — targeted replacements
HOOKS="$PKG_ROOT/hooks.py"
sed -i \
    -e 's|"Auto Replenishment Forecast"|"Replenishment Store Plan"|g' \
    -e 's|"Auto Replenishment Log"|"Replenishment Run"|g' \
    -e 's|auto_replenishment_forecast\.js|replenishment_store_plan.js|g' \
    -e 's|auto_replenishment_log\.js|replenishment_run.js|g' \
    -e 's|\.auto_replenishment_forecast\.auto_replenishment_forecast\.has_permission|.replenishment_store_plan.replenishment_store_plan.has_permission|g' \
    -e 's|doctype\.auto_replenishment_forecast\.auto_replenishment_forecast|doctype.replenishment_store_plan.replenishment_store_plan|g' \
    "$HOOKS"

echo "  OK    JS and hooks.py updated"

# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP — remove all __pycache__ (stale bytecode will cause import errors)
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "[+] Removing __pycache__ directories..."
find "$PKG_ROOT" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
echo "  OK    __pycache__ cleared"

echo ""
echo "============================================================"
echo "  File rename complete. DB migration runs next."
echo "============================================================"
echo ""

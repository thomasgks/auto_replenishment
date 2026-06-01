#!/usr/bin/env bash
# =============================================================================
# build.sh  —  Single command to rename + migrate + build
# =============================================================================
# Copy these 3 files to: /home/erpnext/frappe-bench/apps/auto_replenishment/
#   build.sh
#   rename_doctypes.sh
#   migrate_db.sql
#
# Run from frappe-bench root:
#   cd /home/erpnext/frappe-bench
#   bash apps/auto_replenishment/build.sh
# =============================================================================

set -e

BENCH_DIR="/home/erpnext/frappe-bench"
SITE="moosa.test"
APP_SCRIPTS="$BENCH_DIR/apps/auto_replenishment"
PKG_ROOT="$BENCH_DIR/apps/auto_replenishment/auto_replenishment"

cd "$BENCH_DIR"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Replenishment Run — Full Rename & Build                ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Pre-flight checks ────────────────────────────────────────────────────────
echo "► Pre-flight checks..."

for f in \
    "$APP_SCRIPTS/rename_doctypes.sh" \
    "$APP_SCRIPTS/migrate_db.sql"
do
    if [ ! -f "$f" ]; then
        echo "  ERROR: Missing: $f"
        echo "         Copy build.sh, rename_doctypes.sh, migrate_db.sql"
        echo "         to $APP_SCRIPTS/ then re-run."
        exit 1
    fi
done

echo "  ✓ All required files present"
echo "  ✓ Site: $SITE"
echo ""

# ── Step 1: Stop workers ─────────────────────────────────────────────────────
echo "► [1/6] Stopping background workers..."
sudo supervisorctl stop all 2>/dev/null || bench stop 2>/dev/null || true
sleep 2
echo "  ✓ Workers stopped"
echo ""

# ── Step 2: File system rename ───────────────────────────────────────────────
echo "► [2/6] Renaming files and folders..."
bash "$APP_SCRIPTS/rename_doctypes.sh"

# ── Step 3: DB rename via bench mariadb ──────────────────────────────────────
echo "► [3/6] Running DB rename migration..."
echo "        Renaming MySQL tables and fixing all references..."
echo ""

bench --site "$SITE" mariadb < "$APP_SCRIPTS/migrate_db.sql"

echo ""
echo "  ✓ DB migration complete"
echo ""

# ── Step 4: bench migrate ────────────────────────────────────────────────────
echo "► [4/6] Running bench migrate..."
bench --site "$SITE" migrate
echo "  ✓ bench migrate complete"
echo ""

# ── Step 5: Build assets ─────────────────────────────────────────────────────
echo "► [5/6] Building assets..."
bench build --app auto_replenishment
echo "  ✓ Assets built"
echo ""

# ── Step 6: Clear cache + restart ────────────────────────────────────────────
echo "► [6/6] Clearing cache and restarting..."
bench --site "$SITE" clear-cache
bench --site "$SITE" clear-website-cache
sudo supervisorctl start all 2>/dev/null || bench start 2>/dev/null &
sleep 3
echo "  ✓ Cache cleared, bench restarted"
echo ""

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Build complete!                                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Renames complete:"
echo "    Auto Replenishment Log           → Replenishment Run"
echo "    Auto Replenishment Log Store     → Replenishment Run Store"
echo "    Auto Replenishment Forecast      → Replenishment Store Plan"
echo "    Auto Replenishment Forecast Item → Replenishment Store Plan Item"
echo "    AR Forecast Allocation           → Replenishment Allocation"
echo "    AR Forecast Material Request     → Replenishment MR Link"
echo ""
echo "  Verify:"
echo "    https://yoursite/app/replenishment-run"
echo "    https://yoursite/app/replenishment-store-plan"
echo ""

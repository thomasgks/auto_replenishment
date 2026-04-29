# Auto Replenishment — ERPNext Implementation Guide

**Version:** 1.0.0 | **Prepared by:** Thomas George | **Date:** 21-Feb-2026  
**Scope:** 1 Central Warehouse · 35 Stores · 300K+ Items

---

## Table of Contents
1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Installation Steps](#3-installation-steps)
4. [Configuration](#4-configuration)
5. [Database Indexes (Performance Critical)](#5-database-indexes)
6. [Custom Fields](#6-custom-fields)
7. [DocType Reference](#7-doctype-reference)
8. [Lead Time Design](#8-lead-time-design)
9. [Forecast Calculation Logic](#9-forecast-calculation-logic)
10. [Allocator Agent Logic](#10-allocator-agent-logic)
11. [Supply Status Flags](#11-supply-status-flags)
12. [Performance Optimizations](#12-performance-optimizations)
13. [Scheduler Setup](#13-scheduler-setup)
14. [User Workflow](#14-user-workflow)
15. [API Reference](#15-api-reference)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                     ERPNext (Frappe Framework)                   │
│                                                                  │
│  ┌─────────────┐    ┌──────────────────┐    ┌────────────────┐  │
│  │  Scheduler  │───▶│  Forecast Engine │───▶│ Forecast Docs  │  │
│  │ (Daily/Wkly)│    │  (pandas + SQL)  │    │ (1 per store)  │  │
│  └─────────────┘    └──────────────────┘    └────────┬───────┘  │
│                                                       │          │
│  ┌──────────────┐                                     ▼          │
│  │  Allocator   │◀────────────────── Allocator opens forecast    │
│  │  (User)      │                                     │          │
│  └──────┬───────┘                                     ▼          │
│         │ clicks button              ┌────────────────────────┐  │
│         └──────────────────────────▶│   Allocator Agent      │  │
│                                      │  1. Live stock snapshot │  │
│                                      │  2. Warehouse alloc    │  │
│                                      │  3. Donor eval + DOS   │  │
│                                      │  4. Create MRs         │  │
│                                      └────────────┬───────────┘  │
│                                                   ▼              │
│                                      ┌─────────────────────────┐ │
│                                      │   Material Requests      │ │
│                                      │  (1 per supply source)  │ │
│                                      └─────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions for Scale (300K items × 35 stores)

| Decision | Rationale |
|----------|-----------|
| Bulk SQL instead of per-item frappe.get_doc | 300K individual queries would take hours; one bulk query takes seconds |
| Pandas DataFrames for calculation | Vectorized math on 300K rows vs Python loops |
| One background job per store | 35 parallel workers vs sequential processing |
| Batched IN clauses (1000 items/batch) | MySQL IN clause limit and memory management |
| Redis caching for item master | Avoid re-reading 300K item records on every run |
| Chunked DB writes | Insert forecast items in batches, not one by one |

---

## 2. Prerequisites

### Server Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| ERPNext | v14+ | v15 |
| Python | 3.10+ | 3.11+ |
| MariaDB | 10.6+ | 10.11+ |
| Redis | 6+ | 7+ |
| RAM | 8 GB | 16 GB |
| CPU | 4 cores | 8+ cores |

### Python Packages

```bash
pip install pandas>=1.5.0 numpy>=1.23.0
```

### ERPNext Modules Required

- **Stock** module (active)
- **Buying** module (active)
- **Accounts / Selling** module (active)

---

## 3. Installation Steps

### Step 1 — Get the App

```bash
# On your Frappe/ERPNext bench server
cd /home/frappe/frappe-bench

# Option A: From git (recommended)
bench get-app https://github.com/your-org/auto_replenishment.git

# Option B: From local directory
bench get-app auto_replenishment /path/to/auto_replenishment
```

### Step 2 — Install on Site

```bash
bench --site your-site.com install-app auto_replenishment
```

### Step 3 — Run After-Install Script

This creates custom fields on Item Master and Material Request:

```bash
bench --site your-site.com execute auto_replenishment.setup.install.after_install
```

### Step 4 — Apply Database Indexes (CRITICAL for performance)

```bash
bench --site your-site.com execute auto_replenishment.setup.install.create_performance_indexes
```

Or run the SQL manually (see Section 5).

### Step 5 — Build Assets

```bash
bench build --app auto_replenishment
bench clear-cache
bench restart
```

### Step 6 — Verify Installation

```bash
bench --site your-site.com console
# In console:
import frappe
frappe.get_doc("Replenishment Config")  # Should not error
```

---

## 4. Configuration

### Initial Setup via UI

1. Go to **Auto Replenishment → Replenishment Config**
2. Fill in all required fields:

| Field | Description | Recommended Default |
|-------|-------------|---------------------|
| **Central Warehouse** | Your main distribution warehouse | (your warehouse name) |
| **Demand History Window** | Days of sales history for selling rate | 30 |
| **Default Safety Days** | Buffer days (overridable per item) | 7 |
| **Internal In-Transit Lead Time** | Avg days for warehouse→store or store→store transfer | 3 |
| **Donor Protection Days** | Min days of cover a donor must keep | 5 |
| **Forecast Schedule** | Daily / Weekly / Manual Only | Daily |
| **Auto Create Forecast** | Whether scheduler auto-runs | ✓ Enabled |
| **Batch Size** | Items per worker thread | 500 |
| **Parallel Workers** | Background workers | 4 |
| **Enable Redis Cache** | Cache item master data | ✓ Enabled |

> **Note on Lead Times**: This app handles **internal transfers only**  
> (Warehouse → Store and Store → Store). It uses the single  
> **Internal In-Transit Lead Time** value from config for all internal moves.  
> Supplier lead times (for Purchase Orders) are used only for reference  
> when evaluating in-transit PO stock — they are NOT used in the core  
> replenishment calculation here.

---

## 5. Database Indexes

These indexes are **critical** for performance with 300K+ items.  
Run after installation:

```sql
-- Stock Ledger Entry: primary lookup for selling rate calculation
CREATE INDEX IF NOT EXISTS idx_sle_ar_selling
ON `tabStock Ledger Entry` (warehouse, posting_date, voucher_type, actual_qty)
WHERE actual_qty < 0;

-- More compatible version (MariaDB syntax):
ALTER TABLE `tabStock Ledger Entry`
ADD INDEX `idx_ar_warehouse_date_type` (warehouse, posting_date, voucher_type);

-- Bin: current stock lookup
ALTER TABLE `tabBin`
ADD INDEX `idx_ar_bin_item_wh` (item_code, warehouse);

-- Stock Entry Detail: in-transit lookup
ALTER TABLE `tabStock Entry Detail`
ADD INDEX `idx_ar_sed_intransit` (item_code, t_warehouse);

-- Item Master: exclusion flag lookup
ALTER TABLE `tabItem`
ADD INDEX `idx_ar_item_exclusion` (custom_exclude_from_replenishment, disabled);

-- Forecast Item child: supply status reporting
ALTER TABLE `tabAuto Replenishment Forecast Item`
ADD INDEX `idx_ar_fi_status` (supply_status, parent);
```

### Verify Indexes

```bash
bench --site your-site.com mariadb
SHOW INDEX FROM `tabStock Ledger Entry` WHERE Key_name LIKE '%ar%';
```

---

## 6. Custom Fields

The `after_install` script creates these fields automatically:

### Item Master

| Field | Type | Purpose |
|-------|------|---------|
| `custom_exclude_from_replenishment` | Check | Exclude item from auto-replenishment |
| `custom_safety_days` | Int | Per-item safety days override (0 = use default) |
| `custom_replenishment_notes` | Small Text | Notes for the allocator |

### Material Request

| Field | Type | Purpose |
|-------|------|---------|
| `custom_auto_replenishment_forecast` | Link → Forecast | Tracks which forecast created this MR |
| `custom_source_warehouse` | Link → Warehouse | Source warehouse for this MR |

### Material Request Item

| Field | Type | Purpose |
|-------|------|---------|
| `custom_forecast_item` | Data | Reference to the forecast item row |

---

## 7. DocType Reference

### Auto Replenishment Forecast

One document per store per forecast run. Status flow:

```
Draft → Submitted → Material Requests Created → Closed
```

**Key Fields:**
- `warehouse` — the store being replenished
- `forecast_date` — date of this forecast
- `status` — current status
- `total_items` — items needing replenishment
- `items` — child table (Auto Replenishment Forecast Item)

**Custom Buttons (Submitted state):**
- **Create Material Requests** — triggers the Allocator Agent
- **Recalculate (Live Data)** — refreshes calculations
- **View Shortage Items** — shows partial/no-supply items

### Replenishment Config (Single DocType)

System-wide configuration. Only one record exists.

### Auto Replenishment Log

Audit trail of each forecast run. Shows progress, errors, completion status.

---

## 8. Lead Time Design

This app uses **two distinct lead time concepts**:

### A. Internal In-Transit Lead Time (used in core calculations)

- **What it is**: Average days for a stock transfer to move from warehouse→store or store→store
- **How set**: Single value in **Replenishment Config** → Internal In-Transit Lead Time
- **Used for**:
  - Calculating Lead Time Demand for all store forecasts
  - Determining whether in-transit stock arrives before stockout
  - Donor store evaluation (ETA check)

### B. Supplier Lead Time (reference only)

- **What it is**: Days from placing a Purchase Order to receiving stock at the warehouse
- **Where stored**: ERPNext Item Master → Reorder Rules → Lead Time Days
- **Used for**: Counting open Purchase Orders as "usable in-transit" to the central warehouse
- **NOT used** in the store-level replenishment calculation

```
Supplier → [Supplier Lead Time] → Central Warehouse → [Internal Lead Time] → Store
                ↑                                              ↑
           (from Item Master,                          (from Replenishment Config,
            used for PO counting only)                  used for all calculations)
```

---

## 9. Forecast Calculation Logic

For each item in each store, 6 calculations run in a vectorized batch:

```
Step 1:  Selling Rate    = Total Sales Last N Days ÷ N
         (from Stock Ledger Entry, Delivery Notes + Sales Invoices)

Step 2:  Lead Time Demand = Selling Rate × Internal In-Transit Lead Time Days
         (how much stock sells while waiting for transfer)

Step 3:  Safety Stock     = Selling Rate × Safety Days
         (buffer against demand spikes)

Step 4:  Target Stock     = Lead Time Demand + Safety Stock
         (what the store should have)

Step 5:  Effective OnHand = Current OnHand + Usable In-Transit
         where: Usable In-Transit = open Stock Entry transfers
                arriving within Internal Lead Time days

Step 6:  Forecasted Req  = max(0, Target Stock − Effective OnHand)
```

Items with Forecasted Req = 0 are excluded from the Forecast document.

### Inclusion Filter (3 conditions must ALL be true)

1. Item NOT flagged as `custom_exclude_from_replenishment`
2. Item has supply potential (stock exists anywhere OR open PO arriving today)
3. Forecasted Requirement > 0

---

## 10. Allocator Agent Logic

Triggered when Allocator clicks **"Create Material Requests"**:

```
For each item in forecast:
  1. Recalculate requirement using LIVE stock (real-time snapshot)
  2. Check Central Warehouse available qty (actual - reserved)
     → Allocate min(available, requirement) from warehouse
     → Gap = requirement - warehouse allocation
  3. If Gap > 0:
       For each donor store (sorted by DOS descending):
         - Calculate: Effective OnHand, Selling Rate, DOS
         - Protected Stock = Selling Rate × Protection Days
         - Transferable Qty = Effective OnHand − Protected Stock
         - Check Fairness: Requesting DOS after ≥ Donor DOS after
         - If fairness PASSES: Allocate min(transferable, gap)
         - Update Gap
         - STOP when Gap = 0
  4. Create one Material Request per supply source used
  5. Set supply_status on each forecast item
```

### DOS Fairness Rule

Transfer is only approved if:
```
Requesting Store DOS (after receiving transfer) ≥ Donor Store DOS (after giving transfer)
```

This prevents taking stock from a store that needs it equally or more.

---

## 11. Supply Status Flags

Each forecast item is flagged after MR creation:

| Status | Meaning | Action Required |
|--------|---------|-----------------|
| **Pending** | Not yet processed | Run Create Material Requests |
| **Full Supply** | 100% of requirement met | None |
| **Partial Supply** | Some stock allocated, gap remains | Manual follow-up |
| **No Supply** | Zero stock available anywhere | Manual follow-up / PO creation |
| **No Longer Required** | Live recalc shows 0 needed | None |

### Querying Partial and No Supply Items

```python
# In Python / bench console
import frappe
rows = frappe.db.sql("""
    SELECT f.warehouse, fi.item_code, fi.supply_status,
           fi.forecasted_requirement, fi.shortage_qty
    FROM `tabAuto Replenishment Forecast` f
    JOIN `tabAuto Replenishment Forecast Item` fi ON fi.parent = f.name
    WHERE fi.supply_status IN ('Partial Supply', 'No Supply')
      AND f.forecast_date = CURDATE()
    ORDER BY fi.supply_status, f.warehouse
""", as_dict=True)
```

Or use the API endpoint: `auto_replenishment.api.endpoints.get_supply_status_report`

---

## 12. Performance Optimizations

### Why This Matters

- 300,000 items × 35 stores = **10.5 million item-store combinations**
- Naive approach (frappe.get_doc per item): ~300K DB calls per store = **days to run**
- This implementation: **bulk SQL + pandas** = target < 15 min for all 35 stores

### Optimization Techniques Used

#### 1. Bulk SQL Queries (most important)
All data fetched in single queries with IN clauses, not per-item:
```python
# ✓ Correct: one query for all items
frappe.db.sql("SELECT item_code, SUM(actual_qty) FROM ... WHERE item_code IN (...) GROUP BY item_code")

# ✗ Wrong: per-item query loop
for item in items:
    frappe.db.get_value("Stock Ledger Entry", ...)
```

#### 2. Batched IN Clauses
MySQL has practical limits on IN clause size. Items are processed in batches of 1000:
```python
batch_size = 1000
for i in range(0, len(item_codes), batch_size):
    batch = item_codes[i:i + batch_size]
    # query with this batch
```

#### 3. Pandas Vectorization
All 6 forecast calculations run as column operations, not row loops:
```python
# ✓ Vectorized (fast)
df["selling_rate"] = df["sales_30d"] / history_days
df["lead_time_demand"] = df["selling_rate"] * df["lead_time_days"]

# ✗ Slow (avoid)
for _, row in df.iterrows():
    row["selling_rate"] = row["sales_30d"] / history_days
```

#### 4. Parallel Background Workers
One `frappe.enqueue` job per store means all 35 stores process simultaneously:
```python
for wh in store_warehouses:
    frappe.enqueue(
        "auto_replenishment.tasks.scheduler.generate_forecast_for_store",
        queue="long", timeout=3600,
        warehouse=wh, config=config, ...
    )
```

#### 5. Document Bulk Insert
Avoid saving the forecast document row by row:
```python
# Insert all items into the doc object first, THEN save once
for _, row in df.iterrows():
    doc.append("items", {...})
doc.insert()  # Single DB transaction
```

### Monitoring Performance

```bash
# Check background job queue
bench --site your-site.com execute frappe.utils.background_jobs.get_jobs

# Check auto replenishment log
bench --site your-site.com execute "frappe.get_list('Auto Replenishment Log', filters={'run_date': frappe.utils.today()}, fields=['*'])"
```

---

## 13. Scheduler Setup

### Verify Scheduler is Running

```bash
bench --site your-site.com scheduler enable
bench --site your-site.com scheduler status
```

### Crontab (supervisor/systemd based)

Frappe's scheduler runs automatically. The hooks in `hooks.py` register:

```python
scheduler_events = {
    "daily": [
        "auto_replenishment.tasks.scheduler.run_daily_forecast"
    ],
    "weekly": [
        "auto_replenishment.tasks.scheduler.run_weekly_forecast"
    ]
}
```

### Manual Trigger via Bench

```bash
bench --site your-site.com execute auto_replenishment.tasks.scheduler.trigger_manual_forecast
```

### Manual Trigger via UI

Go to **Auto Replenishment → Replenishment Config** → click **"Run Forecast Now"** button.

### Worker Configuration (for high volume)

In `Procfile` or `supervisor.conf`, ensure you have enough long-queue workers:

```
# Add to Procfile
worker_long=bench worker --queue long
worker_long_2=bench worker --queue long
worker_long_3=bench worker --queue long
worker_long_4=bench worker --queue long
```

Number of long workers should match **Parallel Workers** setting in config.

---

## 14. User Workflow

### Daily Allocator Workflow

```
1. Scheduler runs at midnight → creates Forecast docs for all 35 stores

2. Allocator logs into ERPNext morning
   → Goes to Auto Replenishment → Auto Replenishment Forecast
   → Sees list of today's forecasts (one per store)

3. Opens a store's forecast
   → Reviews items requiring replenishment
   → Can filter by supply_status = 'Pending'
   → Optionally clicks "View Shortage Items" to pre-check supply

4. Clicks "Create Material Requests"
   → System re-reads live stock
   → Dialog shows: total items, potential partial/no supply warning
   → Optionally overrides quantities
   → Clicks Proceed

5. System creates Material Requests (one per supply source)
   → Links appear in confirmation dialog
   → Forecast items updated with supply_status

6. Allocator reviews any Partial Supply / No Supply items
   → These stay visible in the forecast with shortage_qty shown
   → Allocator may:
       a. Create a Purchase Order for the shortage
       b. Wait for next forecast run
       c. Manually transfer from another source

7. Warehouse staff process Material Requests
   → Stock Entry created → inventory updated automatically
```

### Roles

| Role | Access |
|------|--------|
| **Stock Manager** | Full access: configure, create forecasts, create MRs, view logs |
| **Stock User** | View and edit forecasts; create MRs |

---

## 15. API Reference

All endpoints are `frappe.whitelist()` methods:

### Create Material Requests
```python
POST /api/method/auto_replenishment.api.endpoints.create_material_requests
Args:
  forecast_name: str   # Name of the forecast document
  override_qtys: str   # JSON dict {item_code: qty} (optional)
Returns:
  {created_mrs, summary, partial_items, no_supply_items, mr_count}
```

### Trigger Manual Forecast
```python
POST /api/method/auto_replenishment.api.endpoints.trigger_manual_forecast
Returns:
  {message}
```

### Generate Forecast for Single Store
```python
POST /api/method/auto_replenishment.api.endpoints.generate_forecast_single_store
Args:
  warehouse: str
Returns:
  {message}
```

### Get Donor Analysis (for UI)
```python
GET /api/method/auto_replenishment.api.endpoints.get_donor_analysis
Args:
  forecast_name: str
  item_code: str
Returns:
  {donors: [...], gap: float, item_code: str}
```

### Get Supply Status Report
```python
GET /api/method/auto_replenishment.api.endpoints.get_supply_status_report
Args:
  from_date: str (YYYY-MM-DD)
  to_date: str (YYYY-MM-DD)
Returns:
  List of {warehouse, item_code, supply_status, shortage_qty, ...}
```

---

## 16. Troubleshooting

### Issue: Forecast taking too long

**Diagnosis:**
```bash
bench --site your-site.com mariadb
EXPLAIN SELECT * FROM `tabStock Ledger Entry` WHERE warehouse='...' AND posting_date > '...' AND actual_qty < 0;
```
Look for `type: ALL` (full table scan) — means index is missing.

**Fix:** Re-run the index creation SQL from Section 5.

---

### Issue: No items appearing in forecast

**Check 1:** Items may be excluded:
```sql
SELECT COUNT(*) FROM `tabItem` WHERE custom_exclude_from_replenishment = 1;
```

**Check 2:** Items may have no sales history in the last 30 days and no current stock:
```sql
SELECT COUNT(DISTINCT item_code)
FROM `tabStock Ledger Entry`
WHERE warehouse = 'YOUR-STORE-WAREHOUSE'
  AND posting_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
  AND actual_qty < 0;
```

---

### Issue: Background jobs not running

```bash
# Check worker status
bench --site your-site.com doctor

# Check Redis
redis-cli ping

# Restart workers
sudo supervisorctl restart frappe-bench-workers:*
```

---

### Issue: "Replenishment Config not set up"

Go to **Auto Replenishment → Replenishment Config** and fill in all required fields, especially Central Warehouse.

---

### Issue: pandas not found

```bash
env/bin/pip install pandas numpy
bench restart
```

---

## Appendix A — File Structure

```
auto_replenishment/
├── auto_replenishment/
│   ├── __init__.py
│   ├── hooks.py
│   ├── api/
│   │   └── endpoints.py           # REST API endpoints
│   ├── doctype/
│   │   ├── auto_replenishment_forecast/
│   │   │   ├── *.py / *.json
│   │   ├── auto_replenishment_forecast_item/
│   │   │   ├── *.py / *.json
│   │   ├── auto_replenishment_log/
│   │   │   ├── *.py / *.json
│   │   └── replenishment_config/
│   │       ├── *.py / *.json
│   ├── public/
│   │   └── js/
│   │       └── auto_replenishment_forecast.js
│   ├── setup/
│   │   └── install.py             # Custom fields + indexes
│   ├── tasks/
│   │   └── scheduler.py           # Background job orchestration
│   └── utils/
│       ├── forecast_engine.py     # Core calculation engine
│       └── allocator_agent.py     # MR creation logic
├── requirements.txt
└── setup.py
```

---

## Appendix B — Key SQL Queries for Support

```sql
-- Today's forecast summary across all stores
SELECT
    f.warehouse,
    COUNT(fi.name) AS total_items,
    SUM(CASE WHEN fi.supply_status = 'Full Supply' THEN 1 ELSE 0 END) AS full,
    SUM(CASE WHEN fi.supply_status = 'Partial Supply' THEN 1 ELSE 0 END) AS partial,
    SUM(CASE WHEN fi.supply_status = 'No Supply' THEN 1 ELSE 0 END) AS no_supply,
    SUM(CASE WHEN fi.supply_status = 'Pending' THEN 1 ELSE 0 END) AS pending
FROM `tabAuto Replenishment Forecast` f
JOIN `tabAuto Replenishment Forecast Item` fi ON fi.parent = f.name
WHERE f.forecast_date = CURDATE()
GROUP BY f.warehouse
ORDER BY f.warehouse;

-- Items with persistent no-supply (last 7 days)
SELECT fi.item_code, fi.item_name, COUNT(*) as days_no_supply
FROM `tabAuto Replenishment Forecast` f
JOIN `tabAuto Replenishment Forecast Item` fi ON fi.parent = f.name
WHERE fi.supply_status = 'No Supply'
  AND f.forecast_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
GROUP BY fi.item_code, fi.item_name
HAVING days_no_supply >= 3
ORDER BY days_no_supply DESC;
```

---

*Document prepared by Thomas George | Printechs | v1.0.0*

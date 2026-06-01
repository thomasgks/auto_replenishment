// auto_replenishment/report/replenishment_allocation_results/replenishment_allocation_results.js

frappe.query_reports["Replenishment Allocation Results"] = {

    filters: [
        {
            fieldname:  "replenishment_run",
            label:      __("Replenishment Run"),
            fieldtype:  "Link",
            options:    "Replenishment Run",
            reqd:       0,
        },
        {
            fieldname:  "from_date",
            label:      __("From Date"),
            fieldtype:  "Date",
            default:    frappe.datetime.add_months(frappe.datetime.get_today(), -1),
        },
        {
            fieldname:  "to_date",
            label:      __("To Date"),
            fieldtype:  "Date",
            default:    frappe.datetime.get_today(),
        },
        {
            fieldname:  "store_warehouse",
            label:      __("Store"),
            fieldtype:  "Link",
            options:    "Warehouse",
        },
        {
            fieldname:  "supply_status",
            label:      __("Supply Status"),
            fieldtype:  "Select",
            options:    "\nFull Supply\nPartial Supply\nNo Supply",
        },
        {
            fieldname:  "item_group",
            label:      __("Item Group"),
            fieldtype:  "Link",
            options:    "Item Group",
        },
        {
            fieldname:  "run_type",
            label:      __("Run Type"),
            fieldtype:  "Select",
            options:    "\nManual\nScheduler",
        },
    ],

    formatter: function (value, row, column, data, default_formatter) {
        value = default_formatter(value, row, column, data);

        if (column.fieldname === "supply_status") {
            if (data.supply_status === "Full Supply") {
                value = `<span style="color:#16a34a;font-weight:600;">✓ ${value}</span>`;
            } else if (data.supply_status === "Partial Supply") {
                value = `<span style="color:#d97706;font-weight:600;">⚠ ${value}</span>`;
            } else if (data.supply_status === "No Supply") {
                value = `<span style="color:#dc2626;font-weight:600;">✗ ${value}</span>`;
            }
        }

        if (column.fieldname === "fill_rate") {
            const pct = parseFloat(data.fill_rate) || 0;
            const colour = pct >= 90 ? "#16a34a" : pct >= 70 ? "#d97706" : "#dc2626";
            value = `<span style="color:${colour};font-weight:600;">${pct}%</span>`;
        }

        if (column.fieldname === "shortage_qty" && parseFloat(data.shortage_qty) > 0) {
            value = `<span style="color:#dc2626;">${value}</span>`;
        }

        return value;
    },

    onload: function (report) {
        // Add "Open Run" button if a run is selected
        report.page.add_inner_button(__("Open Run"), function () {
            const run = report.get_filter_value("replenishment_run");
            if (run) {
                frappe.set_route("Form", "Replenishment Run", run);
            } else {
                frappe.msgprint(__("Select a Replenishment Run first."));
            }
        });

        // Add "Export to Excel" shortcut
        report.page.add_inner_button(__("Export Excel"), function () {
            report.export_report("Excel");
        });
    },
};

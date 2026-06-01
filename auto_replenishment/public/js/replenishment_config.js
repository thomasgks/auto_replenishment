// auto_replenishment/doctype/replenishment_config/replenishment_config.js

frappe.ui.form.on("Replenishment Config", {
    refresh: function(frm) {
        _set_config_filter_queries(frm);
    }
});

function _set_config_filter_queries(frm) {
    // Supplier filter
    frm.set_query("supplier", "scheduler_suppliers", function() {
        return { filters: [["Supplier", "disabled", "=", 0]] };
    });

    // Item Group L4: depth 4 -- e.g. SALEABLE.MEN.BOTTOM.JEANS
    frm.set_query("item_group", "scheduler_item_groups_l4", function() {
        return {
            filters: [
                ["Item Group", "is_group", "=", 1],
                ["Item Group", "name", "like", "%.%.%.%"],
                ["Item Group", "name", "not like", "%.%.%.%.%"]
            ]
        };
    });

    // Item Group L5: depth 5 -- e.g. SALEABLE.MEN.BOTTOM.JEANS.JEANS
    frm.set_query("item_group", "scheduler_item_groups_l5", function() {
        return {
            filters: [
                ["Item Group", "name", "like", "%.%.%.%.%"],
                ["Item Group", "name", "not like", "%.%.%.%.%.%"]
            ]
        };
    });

    // Year — use ERPNext get_item_attribute (same as Run form)
    var _loadAttr = function(attr, childDt, fieldname) {
        frappe.call({
            method: "erpnext.stock.doctype.item.item.get_item_attribute",
            args: { parent: attr, attribute_value: "" },
            callback: function(r) {
                if (!r.message || !r.message.length) return;
                var vals = r.message.map(function(d) { return d.attribute_value; });
                var options = "\n" + vals.join("\n");

                frappe.model.with_doctype(childDt, function() {
                    var meta = frappe.get_meta(childDt);
                    if (meta && meta.fields) {
                        meta.fields.forEach(function(f) {
                            if (f.fieldname === "attribute_value") {
                                f.fieldtype = "Select";
                                f.options   = options;
                            }
                        });
                    }
                    var gf = frm.fields_dict[fieldname];
                    if (gf && gf.grid) {
                        (gf.grid.docfields || []).forEach(function(f) {
                            if (f.fieldname === "attribute_value") {
                                f.fieldtype = "Select";
                                f.options   = options;
                            }
                        });
                        gf.grid.refresh();
                    }
                });
            }
        });
    };

    _loadAttr("Year",   "Replenishment Config Year",   "scheduler_year");
    _loadAttr("Season", "Replenishment Config Season", "scheduler_season");
}
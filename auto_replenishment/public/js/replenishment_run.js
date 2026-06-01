// auto_replenishment/public/js/replenishment_run.js

frappe.ui.form.on("Replenishment Run", {

    refresh: function (frm) {
        _inject_styles();
        _set_indicator(frm);
        _add_buttons(frm);
        _toggle_filter_lock(frm);
        _build_store_log_viewer(frm);
        _build_alloc_summary(frm);
        _schedule_auto_refresh(frm);

        // Colour shortage rows in allocation grid
        setTimeout(() => _colour_allocation_rows(frm), 600);

        // Set item group depth filters
        _set_filter_field_filters(frm);

        // Initialise date window for new/draft docs
        _init_date_window(frm);

        // Hide standard delete -- use Actions > Delete Run instead
        if (frm.doc.docstatus === 0) {
            setTimeout(() => {
                frm.page.wrapper.find(
                    ".menu-btn-group .dropdown-menu a[data-label='Delete']"
                ).closest("li").hide();
            }, 300);
        }

        // Override standard Cancel -- our cancel_run handles link clearing
        if (frm.doc.docstatus === 1) {
            frm.page.set_secondary_action(__("Cancel"), () => {
                frappe.confirm(
                    __("Cancel this Run? All linked Store Plans will be cancelled/deleted."),
                    () => {
                        frappe.call({
                            method: "run_doc_method",
                            args: {
                                dt: frm.doc.doctype,
                                dn: frm.doc.name,
                                method: "cancel_run",
                                args: "{}",
                            },
                            freeze: true,
                            freeze_message: __("Cancelling Run?"),
                            callback: r => {
                                if (r && r.message && r.message.cancelled) {
                                    frappe.show_alert({
                                        message: __("Run cancelled."),
                                        indicator: "green"
                                    });
                                    frm.reload_doc();
                                }
                            },
                        });
                    }
                );
            });
        }
    },

    before_unload: function (frm) {
        _stop_log_tail(frm);
        _clear_refresh_timer(frm);
    },

    // ── Date window handlers ──────────────────────────────────────────────
    run_date: function (frm) {
        // Run Date changed → recalculate From Date keeping same window size
        if (frm.doc.run_date && frm.doc.demand_history_days) {
            var d = frappe.datetime.add_days(frm.doc.run_date, -frm.doc.demand_history_days);
            frm.set_value("from_date", d);
        } else if (frm.doc.run_date) {
            _init_date_window(frm);
        }
    },

    from_date: function (frm) {
        // From Date changed → recalculate Demand History Days
        if (frm.doc.run_date && frm.doc.from_date) {
            var days = frappe.datetime.get_diff(frm.doc.run_date, frm.doc.from_date);
            if (days >= 0) {
                frm.set_value("demand_history_days", days);
            }
        }
    },

    demand_history_days: function (frm) {
        // Demand History Days changed → recalculate From Date
        if (frm.doc.run_date && frm.doc.demand_history_days >= 0) {
            var d = frappe.datetime.add_days(frm.doc.run_date, -frm.doc.demand_history_days);
            frm.set_value("from_date", d);
        }
    },
});


// ===============================================================
//  Date window initialisation
// ===============================================================

function _init_date_window(frm) {
    // Only auto-initialise on unlocked (Draft) docs
    if (frm.doc.filters_locked) return;

    var run_date = frm.doc.run_date || frappe.datetime.get_today();

    // Set Run Date to today if blank
    if (!frm.doc.run_date) {
        frm.set_value("run_date", run_date);
    }

    // If from_date/demand_history_days already set, leave them alone
    if (frm.doc.from_date && frm.doc.demand_history_days) return;

    // Fetch demand_history_days from Replenishment Config
    frappe.call({
        method: "frappe.client.get_value",
        args: {
            doctype: "Replenishment Config",
            filters: { name: "Replenishment Config" },
            fieldname: "demand_history_days"
        },
        callback: function (r) {
            var days = (r.message && r.message.demand_history_days) || 30;
            var from = frappe.datetime.add_days(run_date, -days);
            if (!frm.doc.demand_history_days) {
                frm.set_value("demand_history_days", days);
            }
            if (!frm.doc.from_date) {
                frm.set_value("from_date", from);
            }
        }
    });
}


// ===============================================================
//  Status indicator
// ===============================================================

function _set_indicator(frm) {
    const map = {
        "Draft":                    ["grey",   "Draft"],
        "Forecasting":              ["orange", "Forecasting?"],
        "Forecast Complete":        ["blue",   "Forecast Complete"],
        "Allocating":               ["orange", "Allocating?"],
        "Allocating":               ["orange", "Allocating -- please wait?"],
        "Allocation Complete":      ["cyan",   "Allocation Complete -- Ready to Submit"],
        "Submitted":                ["purple", "Submitted"],
        "Creating MRs":             ["orange", "Creating MRs -- please wait?"],
        "Material Requests Created":["green",  "MRs Created"],
        "Closed":                   ["grey",   "Closed"],
    };
    const [colour, label] = map[frm.doc.status] || ["grey", frm.doc.status];
    frm.page.set_indicator(label, colour);

    // Run Type badge
    if (frm.doc.run_type) {
        const rtColour = frm.doc.run_type === "Scheduler" ? "blue" : "green";
        frm.$wrapper.find(".ar-run-type-badge").remove();
        frm.page.wrapper.find(".title-area").append(
            `<span class="ar-run-type-badge badge badge-${rtColour}"
                   style="margin-left:8px;font-size:11px;">${frm.doc.run_type}</span>`
        );
    }
}


// ===============================================================
//  Buttons
// ===============================================================

function _add_buttons(frm) {
    const locked    = !!frm.doc.filters_locked;
    const status    = frm.doc.status;
    const submitted = frm.doc.docstatus === 1;
    const closed    = status === "Closed";

    // ?? Draft: Run Forecast + Reset ??????????????????????????????
    if (!submitted && !closed) {
        if (!locked) {
            frm.add_custom_button(__("Run Forecast"), () => _run_forecast(frm))
               .addClass("btn-primary");
        }
        if (locked && ["Forecast Complete", "Allocation Complete"].includes(status)) {
            frm.add_custom_button(__("Run Allocation"), () => _run_allocation(frm))
               .addClass("btn-primary");
        }
        if (locked) {
            frm.add_custom_button(__("Reset"), () => _reset_run(frm), __("Actions"));
        }
        if (!submitted) {
            frm.add_custom_button(__("Delete Run"), () => _delete_run(frm), __("Actions"));
        }
        if (status === "Allocation Complete") {
            frm.add_custom_button(__("Submit Run"), () => {
                frappe.confirm(
                    __("Submit this Replenishment Run? You can then create Material Requests."),
                    () => frm.savesubmit()
                );
            }).addClass("btn-success");
        }
    }

    // ?? Submitted: MR creation + Close ??????????????????????????
    if (submitted && !closed) {
        frm.add_custom_button(__("Create Transfer MRs"), () => _create_transfer_mrs(frm))
           .addClass("btn-primary");
        frm.add_custom_button(__("Create Purchase MRs"), () => _create_purchase_mrs(frm));
        frm.add_custom_button(__("Close Run"), () => {
            frappe.confirm(__("Close this run?"), () => {
                _call(frm, "close_run", {}, () => frm.reload_doc());
            });
        }, __("Actions"));
    }

    // ?? Closed: Re-open ??????????????????????????????????????????
    if (closed) {
        frm.add_custom_button(__("Re-Open"), () => {
            frappe.confirm(__("Re-open this run?"), () => {
                _call(frm, "reopen_run", {}, r => {
                    if (r && r.message && r.message.new_doc) {
                        frappe.set_route("Form", "Replenishment Run", r.message.new_doc);
                    } else {
                        frm.reload_doc();
                    }
                });
            });
        }).addClass("btn-primary");
    }

    // ?? Always: shortage summary ?????????????????????????????????
    if ((frm.doc.partial_supply_count || 0) + (frm.doc.no_supply_count || 0) > 0) {
        frm.add_custom_button(__("View Shortages"), () => _show_shortages(frm), __("Actions"));
        frm.add_custom_button(__("Shortage Analysis"), () => _show_shortage_analysis(frm), __("Actions"));
    }
}


// ===============================================================
//  Filter lock / unlock visual
// ===============================================================

function _toggle_filter_lock(frm) {
    const locked = !!frm.doc.filters_locked;
    const filterFields = [
        "filter_year", "filter_season", "store_filter_mode",
        "filter_suppliers", "filter_item_groups_l4",
        "filter_item_groups_l5", "filter_stores", "allocation_algorithm"
    ];

    filterFields.forEach(fn => {
        const fd = frm.fields_dict[fn];
        if (!fd) return;
        frm.set_df_property(fn, "read_only", locked ? 1 : 0);
    });

    // Show lock indicator in the filters section
    const $sec = frm.$wrapper.find(
        ".form-section"
    ).filter(function() {
        return $(this).find("[data-fieldname=\"filter_year\"]").length > 0;
    });
    $sec.find(".ar-lock-badge").remove();
    if (locked) {
        $sec.find(".section-head").append(
            `<span class="ar-lock-badge" style="font-size:11px;color:#dc2626;
             margin-left:10px;">[Locked] -- use Reset to change filters</span>`
        );
    }
}


// ===============================================================
//  Run Forecast
// ===============================================================

function _run_forecast(frm) {
    frappe.confirm(
        __("Run forecast for all matching stores? Filters will be locked after this."),
        () => {
            _call(frm, "run_forecast", {}, r => {
                if (r && r.message) {
                    frappe.show_alert({
                        message: __("{0} store jobs enqueued", [r.message.stores_queued]),
                        indicator: "blue"
                    });
                    frm.reload_doc();
                }
            });
        }
    );
}


// ===============================================================
//  Run Allocation
// ===============================================================

function _run_allocation(frm) {
    frappe.confirm(
        __("Run cross-store allocation? This will distribute available stock " +
           "across all stores based on selling rate priority. " +
           "This runs as a background job -- the page will auto-refresh."),
        () => {
            _call(frm, "run_allocation", {}, r => {
                if (r && r.message && r.message.queued) {
                    frappe.show_alert({
                        message: __("Allocation job enqueued. Page will refresh automatically."),
                        indicator: "blue"
                    });
                    // Start auto-refresh to show live progress
                    frm.reload_doc();
                    _schedule_auto_refresh(frm);
                } else if (r && r.message && r.message.full_supply !== undefined) {
                    // Synchronous result (fallback)
                    const s = r.message;
                    frappe.msgprint({
                        title: __("Allocation Complete"),
                        message: `
<table style="width:100%;font-size:14px;line-height:2.2">
  <tr style="color:#16a34a"><td>v Full Supply</td>
      <td style="text-align:right;font-weight:700">${s.full_supply}</td></tr>
  <tr style="color:#d97706"><td>! Partial Supply</td>
      <td style="text-align:right;font-weight:700">${s.partial_supply}</td></tr>
  <tr style="color:#dc2626"><td>x No Supply</td>
      <td style="text-align:right;font-weight:700">${s.no_supply}</td></tr>
</table>`,
                        indicator: s.no_supply > 0 ? "orange" : "green"
                    });
                    frm.reload_doc();
                }
            });
        }
    );
}


// ===============================================================
//  Reset
// ===============================================================

function _colour_allocation_rows(frm) {
    const $grid = frm.fields_dict.allocations &&
                  frm.fields_dict.allocations.grid &&
                  frm.fields_dict.allocations.grid.wrapper;
    if (!$grid) return;

    $grid.find(".grid-row[data-idx]").each(function () {
        const idx = parseInt($(this).attr("data-idx"), 10) - 1;
        const rows = frm.doc.allocations || [];
        const row  = rows[idx];
        if (!row) return;

        if (row.source_type === "Shortage") {
            $(this).css({
                "background": "#fff1f2",
                "border-left": "3px solid #dc2626"
            });
            // Add shortage badge to source_type cell
            $(this).find(".col[data-fieldname='source_type'] .static-area")
                .each(function () {
                    if (!$(this).find(".ar-shortage-badge").length) {
                        $(this).append('<span class="ar-shortage-badge">SHORTAGE</span>');
                    }
                });
        } else if (row.source_type === "Central Warehouse" ||
                   row.source_type === "Donor Store") {
            $(this).css({
                "background": "",
                "border-left": "3px solid #16a34a"
            });
        }
    });
}


function _set_filter_field_filters(frm) {
    // L4: depth 4 -- e.g. SALEABLE.MEN.BOTTOM.JEANS
    frm.set_query("item_group", "filter_item_groups_l4", function() {
        return {
            filters: [
                ["Item Group", "is_group", "=", 1],
                ["Item Group", "name", "like", "%.%.%.%"],
                ["Item Group", "name", "not like", "%.%.%.%.%"]
            ]
        };
    });

    // L5: depth 5 -- e.g. SALEABLE.MEN.BOTTOM.JEANS.JEANS
    frm.set_query("item_group", "filter_item_groups_l5", function() {
        return {
            filters: [
                ["Item Group", "name", "like", "%.%.%.%.%"],
                ["Item Group", "name", "not like", "%.%.%.%.%.%"]
            ]
        };
    });

    // Year / Season: use ERPNext get_item_attribute (same as variant dialog)
    // Patch frappe.meta directly so fieldtype = Select before any grid renders
    var _patchMeta = function(childDt, options) {
        // Patch meta fields array
        var meta = frappe.get_meta(childDt);
        if (meta && meta.fields) {
            for (var i = 0; i < meta.fields.length; i++) {
                if (meta.fields[i].fieldname === "attribute_value") {
                    meta.fields[i].fieldtype = "Select";
                    meta.fields[i].options   = options;
                    break;
                }
            }
        }
        // Patch grid column if grid already rendered
        var fieldname = childDt === "Replenishment Config Year"
            ? "filter_year" : "filter_season";
        var gf = frm.fields_dict[fieldname];
        if (gf && gf.grid) {
            // find column in grid.docfields
            (gf.grid.docfields || []).forEach(function(f) {
                if (f.fieldname === "attribute_value") {
                    f.fieldtype = "Select";
                    f.options   = options;
                }
            });
            gf.grid.refresh();
        }
    };

    var _loadAttr = function(attr, childDt) {
        frappe.call({
            method: "erpnext.stock.doctype.item.item.get_item_attribute",
            args: { parent: attr, attribute_value: "" },
            callback: function(r) {
                if (!r.message || !r.message.length) return;
                var vals = r.message.map(function(d) { return d.attribute_value; });
                var options = "\n" + vals.join("\n");
                // Ensure doctype meta loaded before patching
                frappe.model.with_doctype(childDt, function() {
                    _patchMeta(childDt, options);
                });
            }
        });
    };

    _loadAttr("Year",   "Replenishment Config Year");
    _loadAttr("Season", "Replenishment Config Season");
}


function _delete_run(frm) {
    frappe.confirm(
        __("Delete this Replenishment Run and ALL linked Store Plans (Draft only)? This cannot be undone."),
        () => {
            _call(frm, "delete_run", {}, r => {
                if (r && r.message && r.message.deleted) {
                    frappe.show_alert({
                        message: __("Run deleted successfully."),
                        indicator: "green"
                    });
                    frappe.set_route("List", "Replenishment Run");
                }
            });
        }
    );
}


function _reset_run(frm) {
    frappe.confirm(
        __("Reset this run? All existing store plans and allocation results will be cleared. Filters will become editable again."),
        () => {
            _call(frm, "reset_run", {}, () => {
                frappe.show_alert({ message: __("Run reset. Filters unlocked."), indicator: "blue" });
                frm.reload_doc();
            });
        }
    );
}


// ===============================================================
//  MR creation
// ===============================================================

function _create_transfer_mrs(frm) {
    frappe.confirm(__("Create Transfer Material Requests for all allocated stock? This runs as a background job."), () => {
        _call(frm, "create_transfer_mrs", {}, r => {
            if (r && r.message && r.message.queued) {
                frappe.show_alert({
                    message: __("Transfer MR creation started. Page will refresh automatically."),
                    indicator: "blue"
                });
                frm.reload_doc();
                _schedule_auto_refresh(frm);
            } else if (r && r.message && r.message.created_mrs) {
                _show_mr_result(r.message, "Transfer");
                frm.reload_doc();
            }
        });
    });
}

function _create_purchase_mrs(frm) {
    frappe.confirm(__("Create Purchase Material Requests for all shortage items? This runs as a background job."), () => {
        _call(frm, "create_purchase_mrs", {}, r => {
            if (r && r.message && r.message.queued) {
                frappe.show_alert({
                    message: __("Purchase MR creation started. Page will refresh automatically."),
                    indicator: "blue"
                });
                frm.reload_doc();
                _schedule_auto_refresh(frm);
            } else if (r && r.message && r.message.created_mrs) {
                _show_mr_result(r.message, "Purchase");
                frm.reload_doc();
            }
        });
    });
}

function _show_mr_result(result, type) {
    const links = (result.created_mrs || [])
        .map(mr => `<a href="/app/material-request/${mr}" target="_blank">${mr}</a>`)
        .join("<br>");
    frappe.msgprint({
        title: __("{0} MRs Created", [type]),
        message: `<b>${result.mr_count} MR(s):</b><br>${links || __("None")}`,
        indicator: result.mr_count > 0 ? "green" : "orange",
    });
}


// ===============================================================
//  Store log viewer (same terminal as before)
// ===============================================================

function _build_store_log_viewer(frm) {
    const fd = frm.fields_dict.log_viewer_html;
    if (!fd) return;
    const $wrap = $(fd.wrapper);
    $wrap.find("#ar-viewer-wrap").remove();

    const rows = frm.doc.store_logs || [];
    if (!rows.length) return;

    const $c = $("<div id='ar-viewer-wrap'></div>");
    $wrap.append($c);

    $c.html(`
<div style="display:flex;align-items:center;gap:12px;padding-bottom:14px;flex-wrap:wrap;">
  <span style="font-weight:600;font-size:14px;">Select Store</span>
  <select id="ar-store-select" style="flex:1;max-width:520px;padding:6px 10px;
    border:1px solid #d1d5db;border-radius:6px;font-size:13px;background:#fff;cursor:pointer;">
    <option value="">-- choose a store --</option>
  </select>
  <button id="ar-view-btn" class="btn btn-sm btn-primary" style="white-space:nowrap;">? View Log</button>
</div>
<div id="ar-log-panel" style="display:none;">
  <div id="ar-log-toolbar">
    <span id="ar-log-wh-label"></span>
    <span id="ar-log-status-badge" class="ar-badge"></span>
    <span id="ar-log-line-count"></span>
    <span style="flex:1"></span>
    <label style="font-size:12px;color:#a6adc8;cursor:pointer;white-space:nowrap;">
      <input type="checkbox" id="ar-autoscroll" checked> Auto-scroll
    </label>
    <input id="ar-search" type="text" placeholder="Filter lines?" class="ar-search-input">
    <button id="ar-btn-refresh" class="ar-btn">?</button>
    <button id="ar-btn-bottom"  class="ar-btn">?</button>
    <button id="ar-btn-stop"    class="ar-btn ar-btn-danger" style="display:none">? Stop</button>
    <button id="ar-btn-close"   class="ar-btn ar-btn-muted">?</button>
  </div>
  <div id="ar-terminal"><div id="ar-lines"></div></div>
  <div id="ar-status-bar">
    <span id="ar-status-msg">Ready</span>
    <span style="float:right;opacity:.5;font-size:10px" id="ar-log-path"></span>
  </div>
</div>`);

    const $sel = $c.find("#ar-store-select");
    rows.forEach(r => $sel.append($("<option>").val(r.name).text(
        r.store_warehouse + "  [" + r.status + "]"
    )));

    $c.find("#ar-view-btn").on("click", () => {
        const rowName = $sel.val();
        if (!rowName) { frappe.msgprint(__("Please select a store.")); return; }
        const rowDoc = rows.find(r => r.name === rowName);
        if (rowDoc) _open_log_viewer(frm, rowDoc, $c);
    });

    $c.find("#ar-btn-close").on("click", () => { _stop_log_tail(frm); $c.find("#ar-log-panel").slideUp(150); });
    $c.find("#ar-btn-bottom").on("click", () => { const $t=$c.find("#ar-terminal"); $t.scrollTop($t[0].scrollHeight); });
    $c.find("#ar-btn-refresh").on("click", () => { if (frm._ar_row) _load_log(frm, frm._ar_row, $c, 0, true); });
    $c.find("#ar-btn-stop").on("click", () => _stop_log_tail(frm));
    $c.find("#ar-search").on("input", function () {
        const q = $(this).val().toLowerCase();
        $c.find("#ar-lines .ar-line").each(function () {
            $(this).toggle(!q || $(this).text().toLowerCase().includes(q));
        });
    });
}

function _open_log_viewer(frm, rowDoc, $c) {
    _stop_log_tail(frm);
    $c.find("#ar-lines").empty();
    $c.find("#ar-log-line-count").text("");
    $c.find("#ar-status-msg").text("Loading?");
    $c.find("#ar-log-path").text("");
    $c.find("#ar-log-wh-label").text(rowDoc.store_warehouse || "Store");
    _set_log_badge($c, rowDoc.status);
    $c.find("#ar-log-panel").slideDown(200);
    frm._ar_row = rowDoc; frm._ar_c = $c; frm._ar_total_lines = 0;
    _load_log(frm, rowDoc, $c, 0, true);
    if (["Queued","Running"].includes(rowDoc.status)) _start_log_tail(frm, rowDoc, $c);
}

function _load_log(frm, rowDoc, $c, fromLine, replaceAll) {
    frappe.call({
        method: "auto_replenishment.auto_replenishment.doctype.replenishment_run.replenishment_run.get_store_log_content",
        args: { log_name: frm.doc.name, store_row_name: rowDoc.name, last_line: fromLine, max_lines: 1000 },
        callback: r => { if (r && r.message) _apply_log_content(frm, rowDoc, $c, r.message, replaceAll); },
    });
}

function _apply_log_content(frm, rowDoc, $c, data, replaceAll) {
    const $lines = $c.find("#ar-lines"), $term = $c.find("#ar-terminal");
    if (replaceAll) { $lines.empty(); frm._ar_total_lines = 0; }
    const frag = document.createDocumentFragment();
    (data.lines || []).forEach(raw => frag.appendChild(_make_log_line(raw)));
    $lines[0].appendChild(frag);
    frm._ar_total_lines = data.total_lines || (frm._ar_total_lines + (data.lines||[]).length);
    $c.find("#ar-log-line-count").text(frm._ar_total_lines + " lines");
    $c.find("#ar-log-path").text(data.log_file || "");
    $c.find("#ar-status-msg").text("Updated " + new Date().toLocaleTimeString());
    _set_log_badge($c, data.status || rowDoc.status);
    rowDoc.status = data.status || rowDoc.status;
    if ($c.find("#ar-autoscroll").prop("checked")) $term.scrollTop($term[0].scrollHeight);
    if (!["Queued","Running"].includes(rowDoc.status) && frm._ar_tail) {
        _stop_log_tail(frm);
        $c.find("#ar-status-msg").text("Finished ? " + new Date().toLocaleTimeString());
    }
}

function _start_log_tail(frm, rowDoc, $c) {
    $c.find("#ar-btn-stop").show();
    frm._ar_tail = setInterval(() => {
        if (!["Queued","Running"].includes((frm._ar_row||{}).status||"")) { _stop_log_tail(frm); return; }
        _load_log(frm, rowDoc, $c, frm._ar_total_lines || 0, false);
    }, 3000);
}

function _stop_log_tail(frm) {
    if (frm._ar_tail) { clearInterval(frm._ar_tail); frm._ar_tail = null; }
    if (frm._ar_c) frm._ar_c.find("#ar-btn-stop").hide();
}

function _make_log_line(raw) {
    const d = document.createElement("div"); d.className = "ar-line";
    if      (/\bERROR\b/.test(raw))   d.classList.add("ar-error");
    else if (/\bWARNING\b/.test(raw)) d.classList.add("ar-warning");
    else if (/\bSUCCESS\b/.test(raw)) d.classList.add("ar-success");
    else if (/\bSTEP\b/.test(raw))    d.classList.add("ar-step");
    else if (/[=?]{5,}/.test(raw))    d.classList.add("ar-sep");
    d.innerHTML = String(raw).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    return d;
}

const LOG_BADGE = {"Queued":"ar-badge-q","Running":"ar-badge-r","Completed":"ar-badge-ok",
    "Completed (No Items)":"ar-badge-ok","Failed":"ar-badge-fail"};
function _set_log_badge($c, s) {
    $c.find("#ar-log-status-badge").attr("class","ar-badge "+(LOG_BADGE[s]||"ar-badge-q")).text(s||"");
}


// ===============================================================
//  Allocation summary panel
// ===============================================================

function _build_alloc_summary(frm) {
    const fd = frm.fields_dict.allocation_summary_html;
    if (!fd) return;
    const $w = $(fd.wrapper);
    $w.find("#ar-alloc-summary").remove();

    const full    = frm.doc.full_supply_count    || 0;
    const partial = frm.doc.partial_supply_count || 0;
    const noSup   = frm.doc.no_supply_count      || 0;
    const total   = full + partial + noSup || 1;

    if (!full && !partial && !noSup) return;

    const pF = Math.round(full    / total * 100);
    const pP = Math.round(partial / total * 100);
    const pN = Math.round(noSup   / total * 100);

    $w.append(`
<div id="ar-alloc-summary" style="padding:10px 0;">
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px;">
    <div style="flex:1;min-width:90px;background:#f0fdf4;border:1px solid #86efac;
                border-radius:8px;padding:10px 14px;text-align:center;">
      <div style="font-size:24px;font-weight:700;color:#16a34a;">${full}</div>
      <div style="font-size:11px;color:#15803d;">Full Supply</div>
    </div>
    <div style="flex:1;min-width:90px;background:#fffbeb;border:1px solid #fcd34d;
                border-radius:8px;padding:10px 14px;text-align:center;">
      <div style="font-size:24px;font-weight:700;color:#d97706;">${partial}</div>
      <div style="font-size:11px;color:#b45309;">Partial</div>
    </div>
    <div style="flex:1;min-width:90px;background:#fef2f2;border:1px solid #fca5a5;
                border-radius:8px;padding:10px 14px;text-align:center;">
      <div style="font-size:24px;font-weight:700;color:#dc2626;">${noSup}</div>
      <div style="font-size:11px;color:#b91c1c;">No Supply</div>
    </div>
  </div>
  <div style="height:8px;border-radius:4px;overflow:hidden;background:#e5e7eb;display:flex;">
    <div style="width:${pF}%;background:#16a34a;"></div>
    <div style="width:${pP}%;background:#f59e0b;"></div>
    <div style="width:${pN}%;background:#ef4444;"></div>
  </div>
  <div style="font-size:11px;color:#9ca3af;margin-top:4px;">
    Allocated: ${frappe.datetime.str_to_user(frm.doc.allocation_at) || "--"}
  </div>
</div>`);
}


// ===============================================================
//  Shortages dialog
// ===============================================================

function _show_shortage_analysis(frm) {
    // Fetch shortage rows AND fulfilled rows for same items to show full picture
    frappe.call({
        method: "frappe.client.get_list",
        args: {
            doctype: "Replenishment Allocation",
            filters: [
                ["parent", "=", frm.doc.name],
                ["parenttype", "=", "Replenishment Run"],
                ["source_type", "=", "Shortage"],
            ],
            fields: ["item_code", "item_name", "store_warehouse",
                     "suggested_qty", "exclusion_reason"],
            limit: 1000,
            order_by: "item_code asc, suggested_qty desc",
        },
        callback: r => {
            if (!r.message || !r.message.length) {
                frappe.msgprint(__("No shortage items found. All stores fully allocated!"));
                return;
            }

            // Group by shortage reason
            const byReason = {
                "Stock Exhausted by Higher Priority Store": [],
                "Zero Stock Available": [],
                "Other": [],
            };

            const byItem = {};
            r.message.forEach(row => {
                const reason = row.exclusion_reason || "Other";
                const bucket = byReason[reason] !== undefined ? reason : "Other";
                if (!byItem[row.item_code]) {
                    byItem[row.item_code] = {
                        name: row.item_name,
                        exhausted: [],
                        zero: [],
                        other: []
                    };
                }
                const entry = {store: row.store_warehouse, qty: row.suggested_qty};
                if (reason === "Stock Exhausted by Higher Priority Store")
                    byItem[row.item_code].exhausted.push(entry);
                else if (reason === "Zero Stock Available")
                    byItem[row.item_code].zero.push(entry);
                else
                    byItem[row.item_code].other.push(entry);
            });

            const exhaustedItems = Object.entries(byItem)
                .filter(([,d]) => d.exhausted.length > 0);
            const zeroItems = Object.entries(byItem)
                .filter(([,d]) => d.zero.length > 0 && d.exhausted.length === 0);

            const mkRows = (items, reasonLabel, color) => {
                if (!items.length) return "";
                const header = `
<tr style="background:${color};">
  <td colspan="4" style="padding:8px;font-weight:700;font-size:12px;">
    ${reasonLabel} (${items.length} items)
  </td>
</tr>`;
                const rows = items.map(([code, data]) => {
                    const storeList = [...data.exhausted, ...data.zero, ...data.other]
                        .map(s => `${s.store.split(" - ")[0]} (short: ${s.qty})`)
                        .join("&nbsp; ? &nbsp;");
                    return `<tr style="border-bottom:1px solid #f3f4f6;">
                        <td style="padding:4px 8px;font-family:monospace;">${code}</td>
                        <td style="padding:4px 8px;">${data.name||""}</td>
                        <td style="padding:4px 8px;font-size:11px;color:#6b7280;">${storeList}</td>
                    </tr>`;
                }).join("");
                return header + rows;
            };

            frappe.msgprint({
                title: __("Shortage Analysis"),
                message: `
<p style="font-size:12px;color:#6b7280;margin-bottom:12px;">
  <b>${r.message.length}</b> shortage rows across
  <b>${Object.keys(byItem).length}</b> items.
  Shortage reasons explain <em>why</em> a store did not receive stock.
</p>
<table style="width:100%;border-collapse:collapse;font-size:12px;">
  <thead style="background:#1e293b;color:#f1f5f9;">
    <tr>
      <th style="padding:6px 8px;text-align:left;">Item Code</th>
      <th style="padding:6px 8px;text-align:left;">Item Name</th>
      <th style="padding:6px 8px;text-align:left;">Stores with Shortage ? Qty</th>
    </tr>
  </thead>
  <tbody>
    ${mkRows(exhaustedItems,
       "[!] Stock taken by higher selling-rate store -- raise priority or purchase more",
       "#fffbeb")}
    ${mkRows(zeroItems,
       "[Box] Zero stock available -- create Purchase MR",
       "#fef2f2")}
  </tbody>
</table>
<p style="font-size:11px;color:#9ca3af;margin-top:8px;">
  Use <b>Create Purchase MRs</b> to raise procurement for all shortage items.
</p>`,
                wide: true,
            });
        }
    });
}


function _show_shortages(frm) {
    frappe.call({
        method: "frappe.client.get_list",
        args: {
            doctype: "Replenishment Store Plan",
            filters: [["replenishment_run","=",frm.doc.name],
                      ["shortage_items_count",">",0]],
            fields: ["name","warehouse","shortage_items_count","no_supply_count"],
            limit: 100,
        },
        callback: r => {
            if (!r.message || !r.message.length) {
                frappe.msgprint(__("No shortage items found."));
                return;
            }
            const rows = r.message.map(p => `
<tr>
  <td style="padding:4px 8px;">
    <a href="/app/replenishment-store-plan/${p.name}" target="_blank">${p.name}</a>
  </td>
  <td style="padding:4px 8px;">${p.warehouse}</td>
  <td style="padding:4px 8px;text-align:right;color:#d97706;">${p.shortage_items_count}</td>
  <td style="padding:4px 8px;text-align:right;color:#dc2626;">${p.no_supply_count}</td>
</tr>`).join("");
            frappe.msgprint({
                title: __("Shortage Summary by Store"),
                message: `
<table style="width:100%;border-collapse:collapse;font-size:12px;">
  <thead style="background:#f3f4f6;">
    <tr>
      <th style="padding:6px 8px;text-align:left;">Store Plan</th>
      <th style="padding:6px 8px;text-align:left;">Store</th>
      <th style="padding:6px 8px;text-align:right;">Shortage Items</th>
      <th style="padding:6px 8px;text-align:right;">No Supply</th>
    </tr>
  </thead>
  <tbody>${rows}</tbody>
</table>`,
                wide: true,
            });
        }
    });
}


// ===============================================================
//  Auto-refresh while Running
// ===============================================================

function _schedule_auto_refresh(frm, reason) {
    _clear_refresh_timer(frm);
    const runningStatuses = ["Forecasting", "Allocating", "Creating MRs"];
    const allocRunning = frm.doc.allocation_status === "Running";

    if (runningStatuses.includes(frm.doc.status) || allocRunning) {
        // Track whether we started this timer because of an allocation job
        const trackingAllocation = (frm.doc.status === "Allocating" || allocRunning);
        frm._ar_refresh_timer = setInterval(() => {
            frappe.db.get_value("Replenishment Run", frm.doc.name,
                ["status", "allocation_status"],
                r => {
                    const isRunning = runningStatuses.includes(r.status) ||
                                      r.allocation_status === "Running";
                    if (isRunning) {
                        frm.reload_doc();
                    } else {
                        _clear_refresh_timer(frm);
                        frm.reload_doc();  // final refresh to show complete state
                        // Only show alert if we were actually tracking an allocation job
                        if (trackingAllocation && r.allocation_status === "Complete") {
                            frappe.show_alert({
                                message: __("Allocation complete!"),
                                indicator: "green"
                            });
                        } else if (!trackingAllocation) {
                            frappe.show_alert({
                                message: __("Material Request creation complete!"),
                                indicator: "green"
                            });
                        }
                    }
                }
            );
        }, 5000);
    }
}

function _clear_refresh_timer(frm) {
    if (frm._ar_refresh_timer) { clearInterval(frm._ar_refresh_timer); frm._ar_refresh_timer = null; }
}


// ===============================================================
//  Generic doc method caller
// ===============================================================

function _call(frm, method, args, callback) {
    const doCall = function () {
        frappe.call({
            method: "run_doc_method",
            args: { dt: frm.doc.doctype, dn: frm.doc.name, method, args: JSON.stringify(args || {}) },
            freeze: true,
            freeze_message: __("Processing?"),
            callback: r => { if (callback) callback(r); },
            error: () => frappe.msgprint({ title: __("Error"), message: __("Operation failed. Check Error Log."), indicator: "red" }),
        });
    };
    // Must be saved before calling a doc method
    if (frm.is_new() || frm.is_dirty()) {
        frm.save("Save", doCall);
    } else {
        doCall();
    }
}


// ===============================================================
//  CSS
// ===============================================================

function _inject_styles() {
    if (document.getElementById("ar-run-css")) return;
    $('<style id="ar-run-css">').text(`
.ar-lock-badge { font-size:11px; color:#dc2626; margin-left:10px; }
#ar-log-toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:5px;background:#1e1e2e;padding:8px 12px;border-radius:6px 6px 0 0;}
#ar-log-wh-label{font-weight:700;font-size:13px;color:#cdd6f4;}
#ar-log-line-count{font-size:11px;color:#6c7086;margin-left:8px;}
.ar-search-input{font-size:12px;padding:3px 8px;border:1px solid #45475a;border-radius:4px;background:#313244;color:#cdd6f4;width:160px;}
.ar-btn{padding:3px 10px;font-size:12px;border:1px solid #45475a;background:#313244;color:#cdd6f4;border-radius:4px;cursor:pointer;white-space:nowrap;}
.ar-btn:hover{background:#45475a;}.ar-btn-danger{border-color:#f38ba8;color:#f38ba8;}.ar-btn-muted{border-color:#585b70;color:#6c7086;}
#ar-terminal{background:#1e1e2e;color:#cdd6f4;font-family:'JetBrains Mono','Fira Code',Consolas,monospace;font-size:12px;line-height:1.65;padding:14px 16px;height:480px;overflow-y:auto;border:1px solid #313244;border-top:none;}
#ar-status-bar{background:#181825;border:1px solid #313244;border-top:none;border-radius:0 0 6px 6px;padding:3px 12px;font-size:11px;color:#6c7086;}
.ar-line{white-space:pre-wrap;word-break:break-all;padding:0 2px;border-radius:2px;}
.ar-error{color:#f38ba8;background:rgba(243,139,168,.07);}.ar-warning{color:#f9e2af;}.ar-success{color:#a6e3a1;font-weight:700;}
.ar-step{color:#89b4fa;font-weight:700;}.ar-sep{color:#313244;}
.ar-badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;margin-left:8px;}
.ar-badge-q{background:#313244;color:#6c7086;}.ar-badge-r{background:#1e3a5f;color:#89b4fa;animation:ar-pulse 1.4s infinite;}
.ar-badge-ok{background:#1e3a2a;color:#a6e3a1;}.ar-badge-fail{background:#3a1e2a;color:#f38ba8;}
@keyframes ar-pulse{0%,100%{opacity:1}50%{opacity:.4}}
.btn-success{background:#16a34a!important;border-color:#15803d!important;color:#fff!important;}
.btn-success:hover{background:#15803d!important;}
`).appendTo("head");
}
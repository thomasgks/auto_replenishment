// auto_replenishment/public/js/replenishment_store_plan.js

frappe.ui.form.on("Replenishment Store Plan", {

    refresh: function (frm) {
        _inject_styles();
        _set_indicator(frm);
        _add_buttons(frm);
        _build_store_log_viewer(frm);
        _build_alloc_summary(frm);
        setTimeout(() => _build_alloc_filter(frm), 400);
    },

});

// Render allocation log when a forecast item row dialog opens
frappe.ui.form.on("Replenishment Store Plan Item", {
    form_render: function (frm, cdt, cdn) {
        const tryRender = function (attempt) {
            const row = frappe.get_doc(cdt, cdn);
            if (!row) return;
            const $fw = frm.$wrapper.find("[data-fieldname='evaluation_log']")
                .add($("[data-fieldname='evaluation_log']").filter(":visible")).first();
            if (!$fw.length) {
                if (attempt < 15) setTimeout(() => tryRender(attempt + 1), 150);
                return;
            }
            if ($fw.find(".ar-log-rendered").length) return;
            $fw.find("textarea, .like-disabled-input, .control-input-wrapper").hide();
            $fw.find(".ar-log-injected").remove();
            const $inj = $("<div class='ar-log-injected'></div>");
            $fw.append($inj);
            const logText = (row.evaluation_log || "").trim();
            if (!logText) {
                $inj.html('<p style="color:#9ca3af;font-size:13px;padding:10px 0;">' +
                    __("No allocation log yet. Run Evaluate Allocation or Run Allocation on the Replenishment Run first.") +
                    "</p>");
                return;
            }
            _render_eval_log($inj, logText);
        };
        setTimeout(() => tryRender(0), 200);
    }
});


// ═══════════════════════════════════════════════════════════════
//  Status indicator
// ═══════════════════════════════════════════════════════════════

function _set_indicator(frm) {
    const map = {
        "Draft":                     ["grey",   "Draft"],
        "Evaluation Complete":        ["blue",   "Evaluated — Ready to Submit"],
        "Submitted":                  ["purple", "Submitted"],
        "Material Requests Created":  ["green",  "MRs Created"],
        "Closed":                     ["grey",   "Closed"],
        "Re-Opened":                  ["orange", "Re-Opened"],
    };
    const [colour, label] = map[frm.doc.status] || ["grey", frm.doc.status];
    frm.page.set_indicator(label, colour);

    // Allocation status badge
    if (frm.doc.allocation_status && frm.doc.allocation_status !== "Pending") {
        const aMap = { "Received": "green", "Failed": "red" };
        const $badge = $(`<span class="ar-alloc-badge badge" style="margin-left:8px;font-size:11px;
            background:${aMap[frm.doc.allocation_status] ? '#dcfce7' : '#e5e7eb'};
            color:${aMap[frm.doc.allocation_status] ? '#16a34a' : '#374151'};">
            Allocation: ${frm.doc.allocation_status}</span>`);
        frm.$wrapper.find(".ar-alloc-badge").remove();
        frm.page.wrapper.find(".title-area").append($badge);
    }
}


// ═══════════════════════════════════════════════════════════════
//  Buttons
// ═══════════════════════════════════════════════════════════════

function _add_buttons(frm) {
    const submitted  = frm.doc.docstatus === 1;
    const evaluated  = ["Evaluation Complete", "Material Requests Created", "Submitted"]
        .includes(frm.doc.evaluation_status);
    const mrs_created = frm.doc.status === "Material Requests Created";
    const closed      = frm.doc.status === "Closed";
    const alloc_received = ["Received", "Evaluation Complete"].includes(frm.doc.allocation_status);

    if (!submitted && !closed) {
        // Evaluate allocation (per-store, standalone)
        // Evaluate Allocation removed — allocation runs from Replenishment Run
        if (evaluated) {
            frm.add_custom_button(__("Submit Plan"), () => {
                frappe.confirm(
                    __("Submit this Store Plan? You can then create Material Requests."),
                    () => frm.savesubmit()
                );
            }).addClass("btn-success");
        }
        frm.add_custom_button(__("Recalculate (Live Data)"), () => {
            frappe.confirm(__("Recalculate? This clears the current allocation plan."), () => {
                _call(frm, "recalculate_forecast", {}, () => frm.reload_doc());
            });
        }, __("Actions"));
    }

    if (submitted && !closed) {
        // Transfer MRs
        if (alloc_received) {
            frm.add_custom_button(__("Create Transfer MRs"), () => _create_transfer_mrs(frm))
               .addClass("btn-primary");
        }
        frm.add_custom_button(__("Close Plan"), () => {
            frappe.confirm(__("Close this plan?"), () => {
                _call(frm, "close_forecast", {}, () => frm.reload_doc());
            });
        }, __("Actions"));
    }

    if (closed) {
        frm.add_custom_button(__("Re-Open"), () => {
            frappe.confirm(__("Re-open this plan?"), () => {
                _call(frm, "reopen_forecast", {}, r => {
                    if (r && r.message && r.message.new_doc) {
                        frappe.set_route("Form", "Replenishment Store Plan", r.message.new_doc);
                    } else {
                        frm.reload_doc();
                    }
                });
            });
        }).addClass("btn-primary");
    }

    // View Run link
    if (frm.doc.replenishment_run) {
        frm.add_custom_button(__("View Run"), () => {
            frappe.set_route("Form", "Replenishment Run", frm.doc.replenishment_run);
        }, __("Actions"));
    }

    // Shortage indicator button
    if (evaluated && (frm.doc.partial_supply_count > 0 || frm.doc.no_supply_count > 0)) {
        frm.add_custom_button(__("View Shortages"), () => _show_shortages(frm), __("Actions"));
    }
}


// ═══════════════════════════════════════════════════════════════
//  Evaluate Allocation (per-store legacy)
// ═══════════════════════════════════════════════════════════════

function _run_evaluate(frm) {
    frappe.confirm(
        __("Evaluate allocation for this store? For cross-store fairness, use Run Allocation on the Replenishment Run instead."),
        () => {
            _call(frm, "evaluate_allocation", {}, r => {
                if (r && r.message) {
                    const s = r.message;
                    frappe.msgprint({
                        title: __("Evaluation Complete"),
                        message: `<b>Full:</b> ${s.full_supply}  <b>Partial:</b> ${s.partial_supply}  <b>No Supply:</b> ${s.no_supply}`,
                        indicator: s.no_supply > 0 ? "orange" : "green"
                    });
                    frm.reload_doc();
                }
            });
        }
    );
}


// ═══════════════════════════════════════════════════════════════
//  MR creation
// ═══════════════════════════════════════════════════════════════

function _create_transfer_mrs(frm) {
    frappe.confirm(__("Create Transfer MRs for this store's allocated items?"), () => {
        _call(frm, "create_transfer_mrs", {}, r => {
            if (r && r.message) _show_mr_result(r.message, "Transfer");
            frm.reload_doc();
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


// ═══════════════════════════════════════════════════════════════
//  Store forecast log viewer
// ═══════════════════════════════════════════════════════════════

function _build_store_log_viewer(frm) {
    const fd = frm.fields_dict.log_viewer_html;
    if (!fd) return;
    const $wrap = $(fd.wrapper);
    $wrap.find("#ar-viewer-wrap").remove();

    const rows = frm.doc.store_logs || [];
    if (!rows.length) {
        $wrap.append('<p style="color:#9ca3af;font-size:13px;padding:8px 0;">' +
            __("No store log available yet.") + "</p>");
        return;
    }

    const $c = $("<div id='ar-viewer-wrap'></div>");
    $wrap.append($c);

    $c.html(`
<div style="display:flex;align-items:center;gap:12px;padding-bottom:14px;flex-wrap:wrap;">
  <span style="font-weight:600;font-size:14px;">Forecast Log</span>
  <select id="ar-store-select" style="flex:1;max-width:520px;padding:6px 10px;
    border:1px solid #d1d5db;border-radius:6px;font-size:13px;background:#fff;cursor:pointer;">
    <option value="">— choose a store —</option>
  </select>
  <button id="ar-view-btn" class="btn btn-sm btn-primary" style="white-space:nowrap;">📄 View Log</button>
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
    <input id="ar-search" type="text" placeholder="Filter lines…" class="ar-search-input">
    <button id="ar-btn-refresh" class="ar-btn">↻</button>
    <button id="ar-btn-bottom"  class="ar-btn">↓</button>
    <button id="ar-btn-stop"    class="ar-btn ar-btn-danger" style="display:none">■</button>
    <button id="ar-btn-close"   class="ar-btn ar-btn-muted">✕</button>
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

    // If single store, auto-select it
    if (rows.length === 1) {
        $sel.val(rows[0].name).hide();
        $c.find("span:first").hide();
    }

    $c.find("#ar-view-btn").on("click", () => {
        const rowName = $sel.val() || (rows.length === 1 ? rows[0].name : "");
        if (!rowName) { frappe.msgprint(__("Select a store first.")); return; }
        const rowDoc = rows.find(r => r.name === rowName);
        if (rowDoc) _open_log_viewer(frm, rowDoc, $c);
    });

    // Wire terminal controls
    $c.find("#ar-btn-close").on("click", () => { _stop_log_tail(frm); $c.find("#ar-log-panel").slideUp(); });
    $c.find("#ar-btn-bottom").on("click", () => { const $t=$c.find("#ar-terminal"); $t.scrollTop($t[0].scrollHeight); });
    $c.find("#ar-btn-refresh").on("click", () => { if (frm._ar_row) _load_log(frm, frm._ar_row, $c, 0, true); });
    $c.find("#ar-btn-stop").on("click", () => _stop_log_tail(frm));
    $c.find("#ar-search").on("input", function () {
        const q = $(this).val().toLowerCase();
        $c.find("#ar-lines .ar-line").each(function () {
            $(this).toggle(!q || $(this).text().toLowerCase().includes(q));
        });
    });

    // Auto-open if only one store
    if (rows.length === 1) {
        setTimeout(() => _open_log_viewer(frm, rows[0], $c), 500);
    }
}

function _open_log_viewer(frm, rowDoc, $c) {
    _stop_log_tail(frm);
    $c.find("#ar-lines").empty();
    $c.find("#ar-log-line-count").text("");
    $c.find("#ar-status-msg").text("Loading…");
    $c.find("#ar-log-path").text("");
    $c.find("#ar-log-wh-label").text(rowDoc.store_warehouse || "Store");
    _set_log_badge($c, rowDoc.status);
    $c.find("#ar-log-panel").slideDown(200);
    frm._ar_row = rowDoc; frm._ar_c = $c; frm._ar_total_lines = 0;
    _load_log(frm, rowDoc, $c, 0, true);
    if (["Queued","Running"].includes(rowDoc.status)) _start_log_tail(frm, rowDoc, $c);
}

function _load_log(frm, rowDoc, $c, fromLine, replaceAll) {
    // Determine run name — the log is stored on the Replenishment Run
    const logName = frm.doc.replenishment_run || frm.doc.name;
    frappe.call({
        method: "auto_replenishment.auto_replenishment.doctype.replenishment_run.replenishment_run.get_store_log_content",
        args: { log_name: logName, store_row_name: rowDoc.name, last_line: fromLine, max_lines: 1000 },
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
    else if (/[═─]{5,}/.test(raw))    d.classList.add("ar-sep");
    d.innerHTML = String(raw).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    return d;
}
const LB={"Queued":"ar-badge-q","Running":"ar-badge-r","Completed":"ar-badge-ok",
    "Completed (No Items)":"ar-badge-ok","Failed":"ar-badge-fail"};
function _set_log_badge($c,s){$c.find("#ar-log-status-badge").attr("class","ar-badge "+(LB[s]||"ar-badge-q")).text(s||"");}


// ═══════════════════════════════════════════════════════════════
//  Allocation summary
// ═══════════════════════════════════════════════════════════════

function _build_alloc_summary(frm) {
    const fd = frm.fields_dict.alloc_summary_html;
    if (!fd) return;
    const $w = $(fd.wrapper);
    $w.find("#ar-store-alloc-summary-panel").remove();

    const full    = frm.doc.full_supply_count    || 0;
    const partial = frm.doc.partial_supply_count || 0;
    const noSup   = frm.doc.no_supply_count      || 0;
    if (!full && !partial && !noSup) return;

    const total = full + partial + noSup || 1;
    const pF = Math.round(full / total * 100);
    const pP = Math.round(partial / total * 100);
    const pN = Math.round(noSup / total * 100);

    $w.append(`
<div id="ar-store-alloc-summary-panel" style="padding:8px 0 4px 0;">
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px;">
    <div style="flex:1;min-width:80px;background:#f0fdf4;border:1px solid #86efac;
                border-radius:6px;padding:8px 12px;text-align:center;">
      <div style="font-size:20px;font-weight:700;color:#16a34a;">${full}</div>
      <div style="font-size:11px;color:#15803d;">Full Supply</div>
    </div>
    <div style="flex:1;min-width:80px;background:#fffbeb;border:1px solid #fcd34d;
                border-radius:6px;padding:8px 12px;text-align:center;">
      <div style="font-size:20px;font-weight:700;color:#d97706;">${partial}</div>
      <div style="font-size:11px;color:#b45309;">Partial</div>
    </div>
    <div style="flex:1;min-width:80px;background:#fef2f2;border:1px solid #fca5a5;
                border-radius:6px;padding:8px 12px;text-align:center;">
      <div style="font-size:20px;font-weight:700;color:#dc2626;">${noSup}</div>
      <div style="font-size:11px;color:#b91c1c;">No Supply</div>
    </div>
  </div>
  <div style="height:6px;border-radius:3px;overflow:hidden;background:#e5e7eb;display:flex;">
    <div style="width:${pF}%;background:#16a34a;"></div>
    <div style="width:${pP}%;background:#f59e0b;"></div>
    <div style="width:${pN}%;background:#ef4444;"></div>
  </div>
</div>`);
}


// ═══════════════════════════════════════════════════════════════
//  Allocation filter (item filter above allocations table)
// ═══════════════════════════════════════════════════════════════

function _build_alloc_filter(frm) {
    // Force the allocations section to full width
    const allocFd = frm.fields_dict.allocations;
    if (allocFd) {
        $(allocFd.wrapper).closest(".form-column").css("width", "100%");
        $(allocFd.wrapper).closest(".row.form-section-row").find(".form-column")
            .first().css("width", "100%");
    }

    const fd = frm.fields_dict.allocation_filter_html;
    if (!fd) return;
    const $w = $(fd.wrapper);
    $w.find("#ar-alloc-filter").remove();

    const rows = frm.doc.allocations || [];
    if (!rows.length) return;

    const items = [...new Set(rows.map(r => r.item_code).filter(Boolean))].sort();
    const $panel = $(`
<div id="ar-alloc-filter" style="display:flex;align-items:center;gap:10px;padding:6px 0;flex-wrap:wrap;">
  <span style="font-size:13px;font-weight:600;">Filter by Item</span>
  <select id="ar-item-filter" style="padding:5px 10px;border:1px solid #d1d5db;
    border-radius:5px;font-size:13px;min-width:240px;background:#fff;">
    <option value="">— All Items (${items.length}) —</option>
  </select>
  <button id="ar-clear-filter" class="btn btn-xs btn-default">Clear</button>
  <span id="ar-alloc-count" style="font-size:12px;color:#6b7280;"></span>
</div>`);

    items.forEach(code => {
        const name = (rows.find(r => r.item_code === code) || {}).item_name || "";
        $panel.find("#ar-item-filter").append(
            $("<option>").val(code).text(code + (name ? "  " + name : ""))
        );
    });
    $w.append($panel);

    const applyFilter = (itemCode) => {
        const $grid = frm.fields_dict.allocations && frm.fields_dict.allocations.grid;
        if (!$grid) return;
        $grid.wrapper.find(".grid-row[data-idx]").each(function () {
            const idx = parseInt($(this).attr("data-idx"), 10) - 1;
            const row = rows[idx];
            $(this).toggle(!itemCode || (row && row.item_code === itemCode));
        });
        $panel.find("#ar-alloc-count").text(
            itemCode ? rows.filter(r => r.item_code === itemCode).length + " rows" :
            rows.length + " rows total"
        );
    };

    $panel.find("#ar-item-filter").on("change", function () { applyFilter($(this).val()); });
    $panel.find("#ar-clear-filter").on("click", function () {
        $panel.find("#ar-item-filter").val(""); applyFilter("");
    });
}


// ═══════════════════════════════════════════════════════════════
//  Allocation log renderer (for item row dialog)
// ═══════════════════════════════════════════════════════════════

function _render_eval_log($mount, rawText) {
    if (!rawText) {
        $mount.html('<p style="color:#9ca3af;font-size:13px;padding:8px 0;">No log available.</p>');
        return;
    }
    const lines = rawText.split("\n");
    const linesHtml = lines.map(line => {
        let cls = "ar-elog-line";
        if (/═{5,}/.test(line))             cls += " ar-elog-sep-heavy";
        else if (/─{5,}/.test(line))        cls += " ar-elog-sep-light";
        else if (/^▶/.test(line))           cls += " ar-elog-step";
        else if (/✓/.test(line))            cls += " ar-elog-success";
        else if (/✗|NO SUPPLY/.test(line))  cls += " ar-elog-error";
        else if (/⚠|PARTIAL/.test(line))   cls += " ar-elog-warning";
        else if (/Status:/.test(line))      cls += " ar-elog-status";
        else if (/^\s+[A-Z][\w\s]+\s*:/.test(line)) cls += " ar-elog-metric";
        const esc = line.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
        return `<div class="${cls}">${esc || "&nbsp;"}</div>`;
    }).join("");

    $mount.html(`
<div class="ar-log-rendered" style="background:#0f1117;border-radius:6px;overflow:hidden;
     margin:4px 0 12px;border:1px solid #21262d;">
  <div style="background:#161b22;padding:6px 14px;border-bottom:1px solid #21262d;
       display:flex;align-items:center;justify-content:space-between;">
    <span style="font-size:12px;font-weight:600;color:#8b949e;font-family:monospace;">
      ALLOCATION LOG
    </span>
    <button class="ar-elog-copy-btn" style="font-size:11px;padding:2px 8px;
        background:#21262d;color:#8b949e;border:1px solid #30363d;
        border-radius:4px;cursor:pointer;">Copy</button>
  </div>
  <div style="padding:12px 14px;max-height:420px;overflow-y:auto;
       font-family:'JetBrains Mono','Fira Code',Consolas,monospace;
       font-size:12px;line-height:1.7;">
    ${linesHtml}
  </div>
</div>`);

    $mount.find(".ar-elog-copy-btn").on("click", function () {
        navigator.clipboard.writeText(rawText).then(() => {
            const $b = $(this); $b.text("Copied!");
            setTimeout(() => $b.text("Copy"), 1500);
        });
    });
}


// ═══════════════════════════════════════════════════════════════
//  Shortage dialog
// ═══════════════════════════════════════════════════════════════

function _show_shortages(frm) {
    const items = (frm.doc.items || []).filter(
        i => ["Partial Supply","No Supply"].includes(i.supply_status)
    );
    const rows = items.map(i => `
<tr>
  <td style="padding:4px 8px;">${i.item_code}</td>
  <td style="padding:4px 8px;">${i.item_name||""}</td>
  <td style="padding:4px 8px;color:${i.supply_status==="No Supply"?"#dc2626":"#d97706"}">
    ${i.supply_status}</td>
  <td style="padding:4px 8px;text-align:right;">${i.forecasted_requirement}</td>
  <td style="padding:4px 8px;text-align:right;">${i.allocated_qty||0}</td>
  <td style="padding:4px 8px;text-align:right;color:#dc2626;">${i.shortage_qty||0}</td>
</tr>`).join("");
    frappe.msgprint({
        title: __("Shortage Items"),
        message: `
<table style="width:100%;border-collapse:collapse;font-size:12px;">
  <thead style="background:#f3f4f6;">
    <tr>
      <th style="padding:6px 8px;text-align:left;">Item Code</th>
      <th style="padding:6px 8px;text-align:left;">Item Name</th>
      <th style="padding:6px 8px;">Status</th>
      <th style="padding:6px 8px;text-align:right;">Required</th>
      <th style="padding:6px 8px;text-align:right;">Allocated</th>
      <th style="padding:6px 8px;text-align:right;">Shortage</th>
    </tr>
  </thead>
  <tbody>${rows}</tbody>
</table>`,
        wide: true,
    });
}


// ═══════════════════════════════════════════════════════════════
//  Generic doc method caller
// ═══════════════════════════════════════════════════════════════

function _call(frm, method, args, callback) {
    const doCall = function () {
        frappe.call({
            method: "run_doc_method",
            args: { dt: frm.doc.doctype, dn: frm.doc.name, method, args: JSON.stringify(args || {}) },
            freeze: true, freeze_message: __("Processing…"),
            callback: r => { if (callback) callback(r); },
            error: () => frappe.msgprint({title:__("Error"), message:__("Operation failed. Check Error Log."), indicator:"red"}),
        });
    };
    if (frm.is_new() || frm.is_dirty()) {
        frm.save("Save", doCall);
    } else {
        doCall();
    }
}


// ═══════════════════════════════════════════════════════════════
//  CSS (shared with replenishment_run.js — injected once)
// ═══════════════════════════════════════════════════════════════

function _inject_styles() {
    if (document.getElementById("ar-plan-css")) return;
    $('<style id="ar-plan-css">').text(`
#ar-log-toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:5px;background:#1e1e2e;padding:8px 12px;border-radius:6px 6px 0 0;}
#ar-log-wh-label{font-weight:700;font-size:13px;color:#cdd6f4;}
#ar-log-line-count{font-size:11px;color:#6c7086;margin-left:8px;}
.ar-search-input{font-size:12px;padding:3px 8px;border:1px solid #45475a;border-radius:4px;background:#313244;color:#cdd6f4;width:140px;}
.ar-btn{padding:3px 8px;font-size:12px;border:1px solid #45475a;background:#313244;color:#cdd6f4;border-radius:4px;cursor:pointer;white-space:nowrap;}
.ar-btn:hover{background:#45475a;}.ar-btn-danger{border-color:#f38ba8;color:#f38ba8;}.ar-btn-muted{border-color:#585b70;color:#6c7086;}
#ar-terminal{background:#1e1e2e;color:#cdd6f4;font-family:'JetBrains Mono','Fira Code',Consolas,monospace;font-size:12px;line-height:1.65;padding:14px 16px;height:460px;overflow-y:auto;border:1px solid #313244;border-top:none;}
#ar-status-bar{background:#181825;border:1px solid #313244;border-top:none;border-radius:0 0 6px 6px;padding:3px 12px;font-size:11px;color:#6c7086;}
.ar-line{white-space:pre-wrap;word-break:break-all;padding:0 2px;border-radius:2px;}
.ar-error{color:#f38ba8;background:rgba(243,139,168,.07);}.ar-warning{color:#f9e2af;}.ar-success{color:#a6e3a1;font-weight:700;}
.ar-step{color:#89b4fa;font-weight:700;}.ar-sep{color:#313244;}
.ar-badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;margin-left:8px;}
.ar-badge-q{background:#313244;color:#6c7086;}.ar-badge-r{background:#1e3a5f;color:#89b4fa;animation:ar-pulse 1.4s infinite;}
.ar-badge-ok{background:#1e3a2a;color:#a6e3a1;}.ar-badge-fail{background:#3a1e2a;color:#f38ba8;}
@keyframes ar-pulse{0%,100%{opacity:1}50%{opacity:.4}}
.ar-elog-line{color:#c9d1d9;white-space:pre-wrap;word-break:break-all;}
.ar-elog-sep-heavy{color:#21262d;border-top:1px solid #21262d;margin:4px 0;}
.ar-elog-sep-light{color:#30363d;}.ar-elog-step{color:#58a6ff;font-weight:700;margin-top:6px;}
.ar-elog-success{color:#3fb950;font-weight:600;}.ar-elog-error{color:#f85149;}
.ar-elog-warning{color:#d29922;}.ar-elog-status{color:#e3b341;font-weight:600;}.ar-elog-metric{color:#79c0ff;}
.btn-success{background:#16a34a!important;border-color:#15803d!important;color:#fff!important;}
.btn-success:hover{background:#15803d!important;}
`).appendTo("head");
}
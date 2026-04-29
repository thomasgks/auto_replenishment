// auto_replenishment/public/js/auto_replenishment_forecast.js
// Form controller for Auto Replenishment Forecast

frappe.ui.form.on('Auto Replenishment Forecast', {

    refresh: function(frm) {
        frm.disable_save();

        // ── Action Buttons ───────────────────────────────────────────────
        if (frm.doc.docstatus === 1 && frm.doc.status !== 'Closed') {

            // Primary: Create Material Requests
            frm.add_custom_button(__('Create Material Requests'), function() {
                _create_material_requests(frm);
            }, __('Actions')).addClass('btn-primary');

            // Secondary: Recalculate with live data
            frm.add_custom_button(__('Recalculate (Live Data)'), function() {
                frm.call('recalculate_forecast').then(() => frm.reload_doc());
            }, __('Actions'));

            // View partial / no supply items
            if (frm.doc.partial_supply_count > 0 || frm.doc.no_supply_count > 0) {
                frm.add_custom_button(__('View Shortage Items'), function() {
                    _show_shortage_dialog(frm);
                }, __('Actions'));
            }
        }

        // Submit button for draft
        if (frm.doc.docstatus === 0) {
            frm.add_custom_button(__('Submit Forecast'), function() {
                frm.savesubmit();
            }).addClass('btn-primary');
        }

        // ── Status Indicator ─────────────────────────────────────────────
        _set_status_indicator(frm);

        // ── Supply Status Summary Chart ──────────────────────────────────
        if (frm.doc.docstatus === 1) {
            _render_supply_summary(frm);
        }
    },

    // ── Child table row click: show donor analysis ──────────────────────
    items_on_form_rendered: function(frm) {
        // Add click handler for donor analysis button on each row
    }
});


frappe.ui.form.on('Auto Replenishment Forecast Item', {
    item_code: function(frm, cdt, cdn) {
        // Prevent manual item addition on submitted docs
        if (frm.doc.docstatus === 1) {
            frappe.model.set_value(cdt, cdn, 'item_code', '');
            frappe.msgprint(__('Items cannot be manually added to a submitted forecast.'));
        }
    }
});


// ── Private helpers ────────────────────────────────────────────────────────

function _create_material_requests(frm) {
    // Show override dialog or proceed directly
    let dialog = new frappe.ui.Dialog({
        title: __('Create Material Requests'),
        fields: [
            {
                fieldtype: 'HTML',
                fieldname: 'info',
                options: `<div class="alert alert-info">
                    <b>${__('Live Stock Snapshot')}</b><br>
                    ${__('The system will re-read live stock levels before creating Material Requests.')}
                    <br><br>
                    <b>${__('Items requiring replenishment:')} ${frm.doc.total_items}</b><br>
                    ${frm.doc.partial_supply_count > 0 ? `<span class="text-warning">${frm.doc.partial_supply_count} ${__('may receive partial supply')}</span><br>` : ''}
                    ${frm.doc.no_supply_count > 0 ? `<span class="text-danger">${frm.doc.no_supply_count} ${__('may have no supply available')}</span>` : ''}
                </div>`
            },
            {
                fieldtype: 'Check',
                fieldname: 'allow_override',
                label: __('I want to override quantities before creating MRs'),
                default: 0
            }
        ],
        primary_action_label: __('Proceed'),
        primary_action: function(values) {
            dialog.hide();
            if (values.allow_override) {
                _show_quantity_override_dialog(frm);
            } else {
                _execute_create_mrs(frm, {});
            }
        }
    });
    dialog.show();
}


function _show_quantity_override_dialog(frm) {
    // Build fields for each item that has a pending requirement
    let fields = [{
        fieldtype: 'HTML',
        fieldname: 'header',
        options: `<p class="text-muted">${__('Override quantities as needed. Leave blank to use calculated values.')}</p>`
    }];

    let pending_items = frm.doc.items.filter(i => i.supply_status === 'Pending' || !i.supply_status);
    // Limit to first 50 in dialog (edge case for very large stores)
    let display_items = pending_items.slice(0, 50);

    display_items.forEach(function(item) {
        fields.push({
            fieldtype: 'Float',
            fieldname: `qty_${item.item_code.replace(/[^a-zA-Z0-9]/g, '_')}`,
            label: `${item.item_code} — ${item.item_name}`,
            description: __('Calculated: {0} {1}', [item.forecasted_requirement, item.uom]),
            default: item.forecasted_requirement
        });
    });

    if (pending_items.length > 50) {
        fields.push({
            fieldtype: 'HTML',
            fieldname: 'overflow_note',
            options: `<p class="text-warning">${__('Showing first 50 items. Remaining {0} items will use calculated quantities.', [pending_items.length - 50])}</p>`
        });
    }

    let override_dialog = new frappe.ui.Dialog({
        title: __('Override Quantities'),
        size: 'large',
        fields: fields,
        primary_action_label: __('Create Material Requests'),
        primary_action: function(values) {
            override_dialog.hide();

            // Build override dict
            let overrides = {};
            display_items.forEach(function(item) {
                let key = `qty_${item.item_code.replace(/[^a-zA-Z0-9]/g, '_')}`;
                if (values[key] !== undefined && values[key] !== null && values[key] !== item.forecasted_requirement) {
                    overrides[item.item_code] = values[key];
                }
            });

            _execute_create_mrs(frm, overrides);
        }
    });
    override_dialog.show();
}


function _execute_create_mrs(frm, override_qtys) {
    frappe.show_progress(__('Creating Material Requests'), 0, 100, __('Fetching live stock data...'));

    frappe.call({
        method: 'auto_replenishment.api.endpoints.create_material_requests',
        args: {
            forecast_name: frm.doc.name,
            override_qtys: JSON.stringify(override_qtys)
        },
        freeze: true,
        freeze_message: __('Creating Material Requests... This may take a moment for large stores.'),
        callback: function(r) {
            frappe.hide_progress();
            if (r.message) {
                let result = r.message;
                _show_mr_result_dialog(frm, result);
                frm.reload_doc();
            }
        },
        error: function(r) {
            frappe.hide_progress();
            frappe.msgprint({
                title: __('Error'),
                message: __('Failed to create Material Requests. Please check the error log.'),
                indicator: 'red'
            });
        }
    });
}


function _show_mr_result_dialog(frm, result) {
    let mr_links = (result.created_mrs || []).map(mr =>
        `<a href="/app/material-request/${mr}" target="_blank">${mr}</a>`
    ).join('<br>');

    let partial_html = result.partial_items && result.partial_items.length > 0
        ? `<br><span class="text-warning">⚠ ${result.partial_items.length} item(s) received partial supply</span>`
        : '';

    let no_supply_html = result.no_supply_items && result.no_supply_items.length > 0
        ? `<br><span class="text-danger">✗ ${result.no_supply_items.length} item(s) had no supply available</span>`
        : '';

    frappe.msgprint({
        title: __('Material Requests Created'),
        message: `
            <b>${result.mr_count || 0} Material Request(s) created:</b><br>
            ${mr_links || __('None')}
            ${partial_html}
            ${no_supply_html}
            <br><br>
            <small class="text-muted">${__('Partial and No Supply items remain visible in the forecast for follow-up.')}</small>
        `,
        indicator: result.no_supply_items && result.no_supply_items.length > 0 ? 'orange' : 'green'
    });
}


function _show_shortage_dialog(frm) {
    let shortage_items = frm.doc.items.filter(i =>
        i.supply_status === 'Partial Supply' || i.supply_status === 'No Supply'
    );

    let rows = shortage_items.map(i => `
        <tr>
            <td>${i.item_code}</td>
            <td>${i.item_name || ''}</td>
            <td class="${i.supply_status === 'No Supply' ? 'text-danger' : 'text-warning'}">${i.supply_status}</td>
            <td>${i.forecasted_requirement}</td>
            <td>${i.allocated_qty || 0}</td>
            <td class="text-danger">${i.shortage_qty || 0}</td>
        </tr>
    `).join('');

    frappe.msgprint({
        title: __('Shortage Items'),
        message: `
            <table class="table table-sm table-bordered">
                <thead>
                    <tr>
                        <th>${__('Item Code')}</th>
                        <th>${__('Item Name')}</th>
                        <th>${__('Status')}</th>
                        <th>${__('Required')}</th>
                        <th>${__('Allocated')}</th>
                        <th>${__('Shortage')}</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `,
        wide: true
    });
}


function _set_status_indicator(frm) {
    const status_map = {
        'Draft': 'orange',
        'Submitted': 'blue',
        'Material Requests Created': 'green',
        'Closed': 'grey'
    };
    let color = status_map[frm.doc.status] || 'grey';
    frm.page.set_indicator(frm.doc.status, color);
}


function _render_supply_summary(frm) {
    if (!frm.doc.items || frm.doc.items.length === 0) return;

    let full = frm.doc.items.filter(i => i.supply_status === 'Full Supply').length;
    let partial = frm.doc.items.filter(i => i.supply_status === 'Partial Supply').length;
    let no_supply = frm.doc.items.filter(i => i.supply_status === 'No Supply').length;
    let pending = frm.doc.items.filter(i => i.supply_status === 'Pending' || !i.supply_status).length;

    let total = frm.doc.items.length;

    // Update summary fields
    frm.set_value('full_supply_count', full);
    frm.set_value('partial_supply_count', partial);
    frm.set_value('no_supply_count', no_supply);
}

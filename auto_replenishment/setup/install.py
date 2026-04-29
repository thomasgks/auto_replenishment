"""
auto_replenishment/setup/install.py

Installation script — creates custom fields on Item Master
and sets up initial configuration.

Run with: bench --site [site] execute auto_replenishment.setup.install.after_install
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def after_install():
    """Called after app installation."""
    create_item_master_custom_fields()
    create_material_request_custom_fields()
    frappe.db.commit()
    print("Auto Replenishment: Custom fields created successfully.")


def create_item_master_custom_fields():
    """Add Auto Replenishment fields to Item Master."""
    custom_fields = {
        "Item": [
            {
                "fieldname": "custom_auto_replenishment_section",
                "fieldtype": "Section Break",
                "label": "Auto Replenishment",
                "insert_after": "reorder_levels",
                "collapsible": 1
            },
            {
                "fieldname": "custom_exclude_from_replenishment",
                "fieldtype": "Check",
                "label": "Exclude from Auto Replenishment",
                "insert_after": "custom_auto_replenishment_section",
                "description": "Tick to exclude this item from automated replenishment (e.g., discontinued, manually managed)."
            },
            {
                "fieldname": "custom_safety_days",
                "fieldtype": "Int",
                "label": "Custom Safety Days",
                "insert_after": "custom_exclude_from_replenishment",
                "description": "Override the system-wide safety days for this specific item. Leave 0 to use default.",
            },
            {
                "fieldname": "custom_replenishment_notes",
                "fieldtype": "Small Text",
                "label": "Replenishment Notes",
                "insert_after": "custom_safety_days",
            }
        ]
    }
    create_custom_fields(custom_fields, ignore_validate=True)


def create_material_request_custom_fields():
    """Add AR tracking fields to Material Request."""
    custom_fields = {
        "Material Request": [
            {
                "fieldname": "custom_auto_replenishment_forecast",
                "fieldtype": "Link",
                "label": "Auto Replenishment Forecast",
                "options": "Auto Replenishment Forecast",
                "insert_after": "amended_from",
                "read_only": 1
            },
            {
                "fieldname": "custom_source_warehouse",
                "fieldtype": "Link",
                "label": "Source Warehouse (AR)",
                "options": "Warehouse",
                "insert_after": "custom_auto_replenishment_forecast",
                "read_only": 1
            }
        ],
        "Material Request Item": [
            {
                "fieldname": "custom_forecast_item",
                "fieldtype": "Data",
                "label": "Forecast Item Ref",
                "insert_after": "item_name",
                "read_only": 1,
                "hidden": 1
            }
        ]
    }
    create_custom_fields(custom_fields, ignore_validate=True)

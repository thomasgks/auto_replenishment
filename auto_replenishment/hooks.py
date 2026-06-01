from . import __version__ as app_version

app_name = "auto_replenishment"

# DocType JS
doctype_js = {
    "Replenishment Store Plan": "public/js/replenishment_store_plan.js",
    "Replenishment Run":        "public/js/replenishment_run.js",
    "Replenishment Config":        "public/js/replenishment_config.js",
}

app_title       = "Auto Replenishment"
app_publisher   = "Printechs"
app_description = "Automated Material Forecast and Material Request Creation for ERPNext"
app_icon        = "octicon octicon-package"
app_color       = "#1a56db"
app_email       = "dev@printechs.com"
app_license     = "MIT"

# Fixtures — exported/imported with bench migrate
# custom_field.json lives at auto_replenishment/fixtures/custom_field.json
fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [["module", "=", "Auto Replenishment"]]
    },
    {
        "doctype": "Property Setter",
        "filters": [["module", "=", "Auto Replenishment"]]
    },
]

# Scheduled Tasks
scheduler_events = {
    "daily":  ["auto_replenishment.tasks.scheduler.run_daily_forecast"],
    "weekly": ["auto_replenishment.tasks.scheduler.run_weekly_forecast"],
}

# Permissions
has_permission = {
    "Replenishment Store Plan": (
        "auto_replenishment.auto_replenishment.doctype"
        ".replenishment_store_plan.replenishment_store_plan.has_permission"
    ),
}

override_doctype_class = {}
doc_events = {}
website_route_rules = []
jinja = {"methods": [], "filters": []}
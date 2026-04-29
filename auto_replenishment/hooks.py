from . import __version__ as app_version

app_name = "auto_replenishment"
app_title = "Auto Replenishment"
app_publisher = "Printechs"
app_description = "Automated Material Forecast and Material Request Creation for ERPNext"
app_icon = "octicon octicon-package"
app_color = "#1a56db"
app_email = "dev@printechs.com"
app_license = "MIT"

# DocTypes
# --------
fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [["module", "=", "Auto Replenishment"]]
    },
    {
        "doctype": "Property Setter",
        "filters": [["module", "=", "Auto Replenishment"]]
    }
]

# Scheduled Tasks
# ---------------
scheduler_events = {
    "daily": [
        "auto_replenishment.tasks.scheduler.run_daily_forecast"
    ],
    "weekly": [
        "auto_replenishment.tasks.scheduler.run_weekly_forecast"
    ]
}

# Permissions
# -----------
has_permission = {
    "Auto Replenishment Forecast": "auto_replenishment.auto_replenishment.doctype.auto_replenishment_forecast.auto_replenishment_forecast.has_permission"
}

# Override DocType Classes
# -------------------------
override_doctype_class = {}

# Document Events
# ---------------
doc_events = {}

# Website
# -------
website_route_rules = []

# Jinja Environment
# -----------------
jinja = {
    "methods": [],
    "filters": []
}

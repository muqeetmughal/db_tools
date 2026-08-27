import os
import json
import frappe

from db_tools.backend.common import guard

# Standard columns present on every DocType
STANDARD_COLUMNS = {
    "name",
    "owner",
    "creation",
    "modified",
    "modified_by",
    "docstatus",
    "idx",
    "parent",
    "parentfield",
    "parenttype",
    "_assign",
    "_comments",
    "_liked_by",
    "_seen",
    "_user_tags",
}

# These fieldtypes don't create database columns
NO_DB_FIELDTYPES = {
    "Section Break",
    "Column Break",
    "Tab Break",
    "HTML",
    "Button",
    "Image",
    "Heading",
    "Fold",
}


def get_expected_fields(doctype_json):
    """Return all fieldnames expected in the database."""

    fields = set(STANDARD_COLUMNS)

    # JSON fields
    for df in doctype_json.get("fields", []):
        if df.get("fieldtype") in NO_DB_FIELDTYPES:
            continue

        fieldname = df.get("fieldname")
        if fieldname:
            fields.add(fieldname)

    # Custom Fields
    custom_fields = frappe.get_all(
        "Custom Field",
        filters={"dt": doctype_json["name"]},
        fields=["fieldname", "fieldtype"],
    )

    for df in custom_fields:
        if df.fieldtype in NO_DB_FIELDTYPES:
            continue

        if df.fieldname:
            fields.add(df.fieldname)

    return fields


def find_doctype_json_paths(app_path):
    """Yield the <doctype>/<doctype>.json schema path for every doctype folder in an app."""
    app_root = os.path.join(app_path, os.path.basename(app_path))

    if not os.path.isdir(app_root):
        return

    for module in sorted(os.listdir(app_root)):
        module_path = os.path.join(app_root, module)
        if not os.path.isdir(module_path):
            continue

        doctypes_path = os.path.join(module_path, "doctype")
        if not os.path.isdir(doctypes_path):
            continue

        for doctype in sorted(os.listdir(doctypes_path)):
            doctype_path = os.path.join(doctypes_path, doctype)
            if not os.path.isdir(doctype_path):
                continue

            json_path = os.path.join(doctype_path, f"{doctype}.json")
            if os.path.isfile(json_path):
                yield json_path


def find_extra_db_columns():
    bench_path = frappe.utils.get_bench_path()
    apps_path = os.path.join(bench_path, "apps")

    report = []

    for app in sorted(frappe.get_installed_apps()):
        if app in {"frappe", "erpnext"}:
            continue

        app_path = os.path.join(apps_path, app)

        if not os.path.isdir(app_path):
            continue

        for json_path in find_doctype_json_paths(app_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue

            doctype = meta.get("name")
            if not doctype:
                continue

            table = f"tab{doctype}"

            if not frappe.db.table_exists(doctype):
                continue

            expected_fields = get_expected_fields(meta)

            db_columns = {
                row.Field
                for row in frappe.db.sql(
                    f"SHOW COLUMNS FROM `{table}`",
                    as_dict=True,
                )
            }

            extra_columns = sorted(db_columns - expected_fields)

            if extra_columns:
                report.append({
                    "app": app,
                    "doctype": doctype,
                    "table": table,
                    "extra_columns": extra_columns,
                })

    return report


def print_extra_db_columns():
    report = find_extra_db_columns()

    if not report:
        print("\n✅ No extra database columns found.")
        return report

    print("\n" + "=" * 100)
    print("EXTRA DATABASE COLUMNS")
    print("=" * 100)

    total = 0

    for item in report:
        print(f"\nApp      : {item['app']}")
        print(f"DocType  : {item['doctype']}")
        print(f"Table    : {item['table']}")
        print("Extra Columns:")

        for column in item["extra_columns"]:
            print(f"   - {column}")
            total += 1

    print("\n" + "=" * 100)
    print(f"DocTypes with extra columns : {len(report)}")
    print(f"Total extra columns         : {total}")
    print("=" * 100)

    return report


@frappe.whitelist()
def get_extra_db_columns_report():
    """Whitelisted API returning the extra-columns report as JSON for the dashboard."""
    guard()
    report = find_extra_db_columns()

    bench_path = frappe.utils.get_bench_path()
    apps_path = os.path.join(bench_path, "apps")

    apps_scanned = 0
    doctypes_scanned = 0
    for app in sorted(frappe.get_installed_apps()):
        if app in {"frappe", "erpnext"}:
            continue
        app_path = os.path.join(apps_path, app)
        if not os.path.isdir(app_path):
            continue
        apps_scanned += 1
        doctypes_scanned += sum(1 for _ in find_doctype_json_paths(app_path))

    apps_affected = sorted({item["app"] for item in report})
    doctypes_affected = len(report)
    total_extra = sum(len(item["extra_columns"]) for item in report)

    return {
        "report": report,
        "summary": {
            "apps_scanned": apps_scanned,
            "doctypes_scanned": doctypes_scanned,
            "apps_affected": apps_affected,
            "doctypes_affected": doctypes_affected,
            "total_extra_columns": total_extra,
        },
    }
"""Unit tests for the Broken Link Detector.

Run with::

    bench --site <site> run-tests --app db_tools --module test_broken_links

The pure-logic tests (severity, queries, renderers) need no database. The
integration tests create throwaway custom DocTypes and records on the site.
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from db_tools.backend.broken_link_detector.broken_links import detect_broken_links
from db_tools.backend.broken_link_detector.models import Finding, Report, Summary
from db_tools.backend.broken_link_detector.queries import (
    broken_batch_query,
    broken_count_query,
    dynamic_populated_batch_query,
    names_in_batch_query,
    populated_count_query,
)
from db_tools.backend.broken_link_detector.report import render
from db_tools.backend.broken_link_detector.scanner import get_doctypes
from db_tools.backend.broken_link_detector.utils import classify_severity, is_empty

TARGET = "Test BLD Target"
CHILD = "Test BLD Child"
PARENT = "Test BLD Parent"


class TestUtils(unittest.TestCase):
    def test_is_empty(self):
        self.assertTrue(is_empty(None))
        self.assertTrue(is_empty(""))
        self.assertTrue(is_empty("   "))
        self.assertFalse(is_empty("CUST-001"))
        self.assertFalse(is_empty(0))

    def test_classify_severity_defaults(self):
        self.assertEqual(classify_severity("User"), "critical")
        self.assertEqual(classify_severity("Company"), "critical")
        self.assertEqual(classify_severity("Customer"), "warning")
        self.assertEqual(classify_severity("Item"), "warning")
        self.assertEqual(classify_severity("Test BLD Target"), "info")
        self.assertEqual(classify_severity(""), "info")

    def test_classify_severity_configurable(self):
        overrides = {"User": "info", "My Doctype": "critical"}
        self.assertEqual(classify_severity("User", overrides), "info")
        self.assertEqual(classify_severity("My Doctype", overrides), "critical")
        self.assertEqual(classify_severity("Customer", overrides), "warning")


class TestQueries(unittest.TestCase):
    def test_populated_count_query(self):
        sql = populated_count_query("Shift Schedule", "employee")
        self.assertIn("`tabShift Schedule`", sql)
        self.assertIn("`employee` IS NOT NULL", sql)
        self.assertIn("`employee` != ''", sql)

    def test_broken_join_queries(self):
        batch = broken_batch_query("Parent DT", "link", "Target DT")
        self.assertIn("LEFT JOIN `tabTarget DT`", batch)
        self.assertIn("parent.name > %(last)s", batch)
        self.assertIn("LIMIT %(batch)s", batch)

        count = broken_count_query("Parent DT", "link", "Target DT")
        self.assertIn("COUNT(*)", count)
        self.assertIn("target.name IS NULL", count)

    def test_dynamic_batch_query(self):
        sql = dynamic_populated_batch_query("Journal Entry", "reference_doctype", "reference_name")
        self.assertIn("parent.`reference_doctype`", sql)
        self.assertIn("parent.`reference_name`", sql)
        self.assertIn("%(last)s", sql)

    def test_names_in_batch_placeholders(self):
        sql = names_in_batch_query("Target DT", 3)
        self.assertIn("`tabTarget DT`", sql)
        self.assertEqual(sql.count("%s"), 3)


class TestReportRenderers(unittest.TestCase):
    def make_report(self):
        summary = Summary(
            doctypes_scanned=2,
            link_fields_scanned=3,
            dynamic_link_fields_scanned=1,
            records_checked=10,
            broken_links=2,
            config_issues=1,
            execution_time=0.5,
        )
        findings = [
            Finding("Sales Invoice", "SINV-001", "customer", "Customer", "CUST-X", "Target document not found", "warning"),
            Finding("ToDo", "TOD-001", "user", "User", "u@x", "Target document not found", "critical"),
        ]
        config = [
            frappe._dict(doctype="X", fieldname="y", message="bad", severity="critical"),
        ]
        return Report(summary=summary, findings=findings, config_issues=config)

    def test_render_json(self):
        out = render(self.make_report(), "json")
        self.assertIn('"source_doctype": "Sales Invoice"', out)
        self.assertIn('"broken_links": 2', out)

    def test_render_csv(self):
        out = render(self.make_report(), "csv")
        self.assertIn("source_doctype,source_name,fieldname", out)
        self.assertIn("SINV-001", out)
        self.assertIn("config issues", out)

    def test_render_markdown(self):
        out = render(self.make_report(), "markdown")
        self.assertIn("# Broken Link Detector Report", out)
        self.assertIn("| Sales Invoice | SINV-001 |", out)

    def test_render_console(self):
        out = render(self.make_report(), "console")
        self.assertIn("BROKEN LINK DETECTOR", out)
        self.assertIn("SINV-001", out)

    def test_unknown_format(self):
        with self.assertRaises(ValueError):
            render(self.make_report(), "xml")


class TestBrokenLinksDB(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._create_doctypes()
        frappe.db.commit()

    @classmethod
    def _create_doctypes(cls):
        if not frappe.db.exists("DocType", TARGET):
            frappe.get_doc(
                {
                    "doctype": "DocType",
                    "name": TARGET,
                    "module": "Custom",
                    "custom": 1,
                    "fields": [{"fieldname": "title", "label": "Title", "fieldtype": "Data"}],
                    "permissions": [{"role": "System Manager", "read": 1, "write": 1}],
                }
            ).insert(ignore_permissions=True)

        if not frappe.db.exists("DocType", CHILD):
            frappe.get_doc(
                {
                    "doctype": "DocType",
                    "name": CHILD,
                    "module": "Custom",
                    "custom": 1,
                    "istable": 1,
                    "fields": [
                        {"fieldname": "item_link", "label": "Item Link", "fieldtype": "Link", "options": TARGET}
                    ],
                }
            ).insert(ignore_permissions=True)

        if not frappe.db.exists("DocType", PARENT):
            frappe.get_doc(
                {
                    "doctype": "DocType",
                    "name": PARENT,
                    "module": "Custom",
                    "custom": 1,
                    "fields": [
                        {"fieldname": "target_link", "label": "Target Link", "fieldtype": "Link", "options": TARGET},
                        {"fieldname": "ref_doctype", "label": "Ref Doctype", "fieldtype": "Link", "options": "DocType"},
                        {"fieldname": "ref_name", "label": "Ref Name", "fieldtype": "Dynamic Link", "options": "ref_doctype"},
                        {"fieldname": "children", "label": "Children", "fieldtype": "Table", "options": CHILD},
                    ],
                    "permissions": [{"role": "System Manager", "read": 1, "write": 1}],
                }
            ).insert(ignore_permissions=True)

    @classmethod
    def tearDownClass(cls):
        for dt in (PARENT, CHILD, TARGET):
            try:
                frappe.delete_doc("DocType", dt, force=1)
            except Exception:
                pass
        frappe.db.commit()
        super().tearDownClass()

    def setUp(self):
        frappe.get_doc({"doctype": TARGET, "name": "TGT-1", "title": "One"}).insert(ignore_permissions=True)
        frappe.get_doc(
            {"doctype": PARENT, "name": "P-1", "target_link": "TGT-1", "ref_doctype": TARGET, "ref_name": "TGT-1"}
        ).insert(ignore_permissions=True)
        frappe.get_doc(
            {
                "doctype": PARENT,
                "name": "P-2",
                "target_link": "TGT-MISSING",
                "ref_doctype": TARGET,
                "ref_name": "TGT-MISSING-2",
                "children": [{"item_link": "CHILD-MISSING"}],
            }
        ).insert(ignore_permissions=True)
        frappe.get_doc(
            {"doctype": PARENT, "name": "P-3", "target_link": "TGT-1", "ref_doctype": "NoSuchDocType", "ref_name": "X"}
        ).insert(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        for dt in (PARENT, TARGET):
            frappe.delete_doc(dt, frappe.db.get_all(dt, pluck="name"), force=1)
        frappe.delete_doc(CHILD, frappe.db.get_all(CHILD, pluck="name"), force=1)
        frappe.db.commit()

    def test_detects_broken_links(self):
        report = detect_broken_links(doctype=[PARENT, CHILD])
        broken = {(f.source_name, f.fieldname) for f in report.findings}

        self.assertIn(("P-2", "target_link"), broken)
        self.assertIn(("P-2", "ref_name"), broken)
        self.assertIn(("P-2", "ref_doctype"), broken)  # NoSuchDocType is not a DocType
        self.assertIn(("P-2", "item_link"), broken)  # broken link inside child table

        self.assertEqual(report.summary.broken_links, 4)
        self.assertEqual(report.summary.config_issues, 1)  # dynamic link to invalid DocType

    def test_valid_records_not_reported(self):
        report = detect_broken_links(doctype=[PARENT, CHILD], include_dynamic=False)
        names = {f.source_name for f in report.findings}
        self.assertNotIn("P-1", names)
        self.assertNotIn("TGT-1", names)

    def test_no_false_positives(self):
        frappe.db.set_value(PARENT, "P-2", {"target_link": "TGT-1"})
        report = detect_broken_links(doctype=[PARENT])
        self.assertNotIn("P-2", {f.source_name for f in report.findings})

    def test_severity_filter(self):
        report = detect_broken_links(doctype=[PARENT, CHILD], severity="critical")
        self.assertTrue(all(f.severity == "critical" for f in report.findings))

    def test_include_child_tables_flag(self):
        self.assertNotIn(CHILD, get_doctypes(include_child_tables=False))
        self.assertIn(CHILD, get_doctypes(include_child_tables=True))

    def test_scan_is_read_only(self):
        before = frappe.get_all(PARENT, pluck="name")
        detect_broken_links(doctype=[PARENT, CHILD])
        after = frappe.get_all(PARENT, pluck="name")
        self.assertEqual(sorted(before), sorted(after))

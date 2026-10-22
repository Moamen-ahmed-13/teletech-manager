"""Regression tests for BOQ/MIR counting rules.

Run from the repository root with:
    python -m unittest discover -s tests -v

These tests use synthetic state only; no customer PDFs, BOQ files, credentials,
or project databases are required.
"""
import unittest

import recon_module as recon


def sample_state():
    st = recon._new_state()
    st["boq"] = {
        "items": [{
            "id": "BOQ!R2", "sn": "1", "item": "CAT6A Cable", "reference": "",
            "part": "CAT6A", "brand": "Example", "unit": "ROLL", "qty": 10.0,
            "unit_price": 25.0, "total_price": 250.0, "flags": [],
        }],
        "warnings": [],
    }
    st["mir_docs"] = [{
        "id": "doc-1", "filename": "MIR-001.pdf",
        "uploaded_at": "2026-01-01T00:00:00Z", "pages": 1,
    }]
    return st


def add_record(st, *, qty=3, unit="ROLL", status="A", reviewed=True,
               matched=True, ref_no="MIR-001", description="CAT6A Cable",
               date="2026-01-01", duplicate_override=None):
    st["records"].append({
        "id": "rec-%d" % (len(st["records"]) + 1),
        "doc_id": "doc-1", "page": 1, "created_at": "2026-01-01T00:00:00Z",
        "origin": "manual", "ocr_flags": [], "ocr": {},
        "user": {
            "description": description, "unit": unit, "qty": qty,
            "total": qty, "ref_no": ref_no, "date": date,
            "mir_status": status, "brand": "Example", "note": None,
        },
        "review_status": "reviewed" if reviewed else "pending",
        "match": {
            "state": "approved" if matched else "suggested",
            "boq_id": "BOQ!R2" if matched else None,
            "candidates": [], "method": "manual",
        },
        "dup_override": duplicate_override,
    })


class NumericAndUnitTests(unittest.TestCase):
    def test_arabic_digits_and_decimal_separator(self):
        self.assertEqual(recon.to_number("١٢٣٫٥"), 123.5)

    def test_invalid_number_is_not_guessed(self):
        self.assertIsNone(recon.to_number("about 12"))

    def test_unit_aliases_are_canonicalized(self):
        self.assertEqual(recon.canon_unit("rolls"), "ROLL")
        self.assertEqual(recon.canon_unit("pieces"), "PC")

    def test_unknown_units_are_preserved_not_silently_mapped(self):
        self.assertEqual(recon.canon_unit("bundle"), "BUNDLE")

    def test_brand_aliases_normalize_common_ocr_errors(self):
        self.assertEqual(recon.brand_key("commscpe"), "commscope")


class ReconciliationCountingTests(unittest.TestCase):
    def test_only_reviewed_approved_match_with_status_a_or_b_counts(self):
        st = sample_state()
        add_record(st, qty=2, status="A", reviewed=True, matched=True)
        add_record(st, qty=3, status="B", reviewed=True, matched=True,
                   ref_no="MIR-002", date="2026-01-02")
        add_record(st, qty=5, status="C", reviewed=True, matched=True,
                   ref_no="MIR-003", date="2026-01-03")
        add_record(st, qty=7, status="D", reviewed=True, matched=True,
                   ref_no="MIR-004", date="2026-01-04")
        add_record(st, qty=11, status="A", reviewed=False, matched=True,
                   ref_no="MIR-005", date="2026-01-05")
        add_record(st, qty=13, status="A", reviewed=True, matched=False,
                   ref_no="MIR-006", date="2026-01-06")

        row = recon.compute(st)["items"][0]
        self.assertEqual(row["confirmed_qty"], 5.0)
        self.assertEqual(row["not_approved_qty"], 12.0)
        self.assertEqual(row["pending_qty"], 11.0)
        self.assertEqual(row["remaining_qty"], 5.0)

    def test_unit_conflict_is_excluded_from_confirmed_quantity(self):
        st = sample_state()
        add_record(st, qty=4, unit="M", status="A")
        row = recon.compute(st)["items"][0]
        self.assertEqual(row["confirmed_qty"], 0.0)
        self.assertEqual(row["unit_conflict_qty"], 4.0)
        self.assertEqual(row["remaining_qty"], 10.0)

    def test_over_boq_is_flagged_and_not_capped(self):
        st = sample_state()
        add_record(st, qty=12, status="A")
        row = recon.compute(st)["items"][0]
        self.assertEqual(row["confirmed_qty"], 12.0)
        self.assertEqual(row["remaining_qty"], -2.0)
        self.assertEqual(row["status"], "over_boq")
        self.assertIn("over_boq", row["flags"])

    def test_missing_price_keeps_financial_value_unavailable(self):
        st = sample_state()
        st["boq"]["items"][0]["unit_price"] = None
        add_record(st, qty=2, status="A")
        row = recon.compute(st)["items"][0]
        self.assertIsNone(row["boq_value"])
        self.assertIsNone(row["mir_value"])
        self.assertIn("price", row["unavailable"])

    def test_duplicate_is_not_counted_twice(self):
        st = sample_state()
        add_record(st, qty=2, status="A")
        add_record(st, qty=2, status="A", duplicate_override="duplicate",
                   ref_no="MIR-002", date="2026-01-02")
        row = recon.compute(st)["items"][0]
        self.assertEqual(row["confirmed_qty"], 2.0)
        self.assertIn("duplicate_record_excluded", row["flags"])

    def test_page_without_records_is_reported_for_manual_verification(self):
        st = sample_state()
        st["mir_docs"][0]["pages"] = 2
        add_record(st, qty=2, status="A")
        result = recon.compute(st)
        gaps = [e for e in result["exceptions"] if e["type"] == "mir_page_without_records"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["ref"], "MIR-001.pdf p2")
        self.assertIn("Check the scan", gaps[0]["message"])

    def test_document_without_completed_ocr_is_reported(self):
        st = sample_state()
        result = recon.compute(st)
        warnings = [e for e in result["exceptions"] if e["type"] == "mir_document_not_fully_read"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("not_run", warnings[0]["message"])


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

import recon_module as R


def record(doc_id, kind, ref, qty, total, unit='PC', status='A'):
    fields = {
        'description': 'CAT6A cable', 'unit': unit, 'qty': qty, 'total': total,
        'ref_no': ref, 'date': '01/01/2026', 'mir_status': status,
        'brand': 'Commscope', 'note': None,
    }
    return {
        'id': 'record-' + kind, 'doc_id': doc_id, 'doc_kind': kind, 'page': 1,
        'origin': 'manual', 'ocr': {}, 'user': fields, 'review_status': 'reviewed',
        'match': {'state': 'approved', 'boq_id': 'BOQ!R2', 'candidates': []},
        'dup_override': None, 'ocr_flags': [], 'created_at': '2026-01-01T00:00:00Z',
        'edited_by': 'tester', 'edited_at': '2026-01-01T00:00:00Z',
    }


def state(records, invoice_docs=None):
    return {
        'version': 1, 'settings': {'currency_label': 'EGP', 'count_statuses': ['A', 'B']},
        'boq': {'warnings': [], 'items': [{
            'id': 'BOQ!R2', 'sheet': 'BOQ', 'row': 2, 'sn': '1',
            'item': 'CAT6A cable', 'reference': '', 'part': '', 'brand': 'Commscope',
            'qty': 10.0, 'unit': 'PC', 'unit_price': 100.0, 'total_price': 900.0, 'flags': [],
        }]},
        'boq_overrides': {}, 'mir_docs': [
            {'id': 'mir-1', 'kind': 'mir', 'filename': 'MIR001.pdf', 'pages': 1, 'uploaded_at': '2026-01-01'},
        ],
        'invoice_docs': invoice_docs or [], 'records': records, 'audit': [],
    }


class FinancialCalculationTests(unittest.TestCase):
    def test_repository_boq_fixture_is_readable(self):
        fixture = Path(__file__).resolve().parents[1] / 'مقايسة بني سويف.xlsx'
        if not fixture.is_file():
            self.skipTest('Repository BOQ fixture is not present in this checkout')
        parsed = R.parse_boq(fixture, display_name=fixture.name)
        self.assertGreater(len(parsed['items']), 0)
        self.assertTrue(any(item.get('qty') is not None for item in parsed['items']))
        self.assertTrue(any(item.get('total_price') is not None or item.get('unit_price') is not None for item in parsed['items']),
                        'The BOQ fixture should expose a financial amount or unit rate for reconciliation')

    def test_repository_invoice_pdf_produces_reviewable_draft_rows(self):
        fixture = Path(__file__).resolve().parents[1] / 'فاتورة رقم 1 بنى سويف لامر التوريد القديم.pdf'
        if not fixture.is_file():
            self.skipTest('Repository invoice fixture is not present in this checkout')
        import shutil
        import tempfile
        import fitz
        if not shutil.which('tesseract'):
            self.skipTest('Tesseract is not installed')
        rows = []
        with tempfile.TemporaryDirectory(prefix='invoice_fixture_') as tmp:
            with fitz.open(str(fixture)) as pdf:
                for page_no, page in enumerate(pdf, 1):
                    image = Path(tmp) / ('page_%d.png' % page_no)
                    page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5)).save(str(image))
                    try:
                        lines = R._tesseract_lines(image, langs='eng+ara')
                    except R.ReconError as exc:
                        if 'ara' in str(exc).lower():
                            self.skipTest('Arabic Tesseract language pack is unavailable')
                        raise
                    parsed = R.parse_invoice_lines(lines, fixture.name)
                    rows.extend(parsed['rows'])
        self.assertGreater(len(rows), 0, 'The repository invoice scan should yield draft lines for manual review')

    def test_invoice_amount_is_used_instead_of_adding_to_mir_estimate(self):
        st = state([
            record('mir-1', 'mir', 'MIR-001', 2.0, None),
            record('invoice-1', 'invoice', 'INV-001', 1.0, 250.0),
        ], [{'id': 'invoice-1', 'kind': 'invoice', 'filename': 'INV-001.pdf',
             'pages': 1, 'uploaded_at': '2026-01-02'}])
        result = R.compute(st)
        row = result['items'][0]
        self.assertEqual(row['boq_value'], 900.0)
        self.assertEqual(row['mir_value'], 180.0)
        self.assertEqual(row['invoice_value'], 250.0)
        self.assertEqual(row['financial_value'], 250.0)
        self.assertEqual(row['financial_source'], 'invoice')
        self.assertEqual(row['remaining_value'], 650.0)
        self.assertAlmostEqual(row['fin_pct'], 27.78, places=2)
        self.assertEqual(result['brands'][0]['remaining_value'], 650.0)

    def test_mir_estimate_remains_fallback_when_no_approved_invoice_exists(self):
        st = state([record('mir-1', 'mir', 'MIR-001', 2.0, None)])
        row = R.compute(st)['items'][0]
        self.assertEqual(row['financial_source'], 'mir_estimate')
        self.assertEqual(row['financial_value'], 180.0)
        self.assertEqual(row['remaining_value'], 720.0)

    def test_invoice_parser_keeps_arabic_description_and_arabic_numbers(self):
        parsed = R.parse_invoice_lines([
            {'text': 'فاتورة رقم ١ كابل شبكة ١٠ لفة ٥٠٠ ٥٠٠٠', 'conf': 82}
        ], 'فاتورة رقم 1.pdf')
        self.assertEqual(parsed['ref_no'], '١')
        self.assertEqual(len(parsed['rows']), 1)
        self.assertIn('كابل', parsed['rows'][0]['description'])
        self.assertEqual(parsed['rows'][0]['qty'], 10.0)
        self.assertEqual(parsed['rows'][0]['unit'], 'ROLL')
        self.assertEqual(parsed['rows'][0]['total'], 5000.0)

    def test_pending_invoice_does_not_inflate_mir_quantity_review_count(self):
        mir = record('mir-1', 'mir', 'MIR-001', 2.0, None)
        invoice = record('invoice-1', 'invoice', 'INV-001', 1.0, 250.0)
        mir['match'] = {'state': 'suggested', 'boq_id': 'BOQ!R2', 'candidates': []}
        invoice['match'] = {'state': 'suggested', 'boq_id': 'BOQ!R2', 'candidates': []}
        st = state([mir, invoice], [{'id': 'invoice-1', 'kind': 'invoice', 'filename': 'INV-001.pdf',
                                     'pages': 1, 'uploaded_at': '2026-01-02'}])
        result = R.compute(st)
        self.assertEqual(result['items'][0]['awaiting_review'], 1)
        self.assertEqual(result['summary']['mir_records'], 1)
        self.assertEqual(result['summary']['invoice_records'], 1)

    def test_line_total_derives_rate_when_unit_price_is_blank(self):
        st = state([record('mir-1', 'mir', 'MIR-001', 2.0, None)])
        st['boq']['items'][0]['unit_price'] = None
        row = R.compute(st)['items'][0]
        self.assertEqual(row['unit_price'], 90.0)
        self.assertEqual(row['boq_value'], 900.0)
        self.assertEqual(row['mir_value'], 180.0)
        self.assertNotIn('price', row['unavailable'])

    def test_mir_unit_conflict_is_not_counted(self):
        st = state([record('mir-1', 'mir', 'MIR-001', 2.0, None, unit='ROLL')])
        row = R.compute(st)['items'][0]
        self.assertEqual(row['confirmed_qty'], 0.0)
        self.assertEqual(row['unit_conflict_qty'], 2.0)
        self.assertEqual(row['mir_value'], 0.0)
        self.assertEqual(row['remaining_value'], 900.0)


class InvoiceTableGeometryTests(unittest.TestCase):
    @staticmethod
    def word(text, left, top, width=35, height=18, conf=92):
        return {'text': text, 'left': left, 'top': top, 'width': width,
                'height': height, 'conf': conf}

    def test_tsv_words_map_fleximax_values_by_unique_math_relation(self):
        words = [
            # Explicit column headers are required before financial values can be assigned.
            self.word('Qty', 720, 55, 28),
            self.word('Price', 810, 55, 38),
            self.word('Total', 900, 55, 38),
            self.word('37', 20, 100, 18),
            self.word('P/N', 90, 99),
            self.word('7601840-7', 135, 101, 85),
            self.word('FlexiMAX', 230, 100, 72),
            self.word('U/UTP', 310, 102, 48),
            self.word('High', 370, 99, 35),
            self.word('Density', 410, 101, 55),
            self.word('Patch', 470, 100, 42),
            self.word('Panel', 520, 99, 42),
            self.word('عدد', 650, 102, 28),
            self.word('180', 720, 100, 30),
            self.word('75', 810, 101, 20),
            self.word('13500', 900, 99, 45),
        ]
        parsed = R.parse_invoice_lines(words, 'invoice.pdf')
        self.assertEqual(len(parsed['rows']), 1)
        row = parsed['rows'][0]
        self.assertIn('FlexiMAX', row['description'])
        self.assertEqual(row['unit'], 'PC')
        self.assertEqual(row['qty'], 180.0)
        self.assertEqual(row['unit_price'], 75.0)
        self.assertEqual(row['total'], 13500.0)
        self.assertTrue(row['math_valid'])
        self.assertIn('line_math_consistent', row['row_flags'])
        self.assertTrue(row['source_cells'])

    def test_tsv_words_do_not_guess_unverified_numeric_columns(self):
        words = [
            self.word('Cable', 100, 200, 48),
            self.word('item', 155, 201, 32),
            self.word('10', 700, 200, 20),
            self.word('7', 800, 201, 12),
            self.word('100', 900, 199, 30),
        ]
        parsed = R.parse_invoice_lines(words, 'invoice.pdf')
        self.assertEqual(len(parsed['rows']), 1)
        row = parsed['rows'][0]
        self.assertIsNone(row['qty'])
        self.assertIsNone(row['unit_price'])
        self.assertIsNone(row['total'])
        self.assertFalse(row['math_valid'])
        self.assertIn('numeric_columns_require_review', row['row_flags'])

    def test_invoice_page_classifier_detects_table_headers(self):
        words = [
            self.word('Item', 30, 40), self.word('Description', 100, 40, 80),
            self.word('Qty', 700, 40), self.word('Unit', 790, 40),
            self.word('Price', 830, 40), self.word('Amount', 930, 40),
            self.word('Cable', 100, 100, 55), self.word('10', 700, 100, 20),
            self.word('5', 820, 100, 12), self.word('50', 930, 100, 20),
        ]
        evidence = R._invoice_page_table_evidence(words)
        self.assertTrue(evidence['is_table'])
        self.assertIn('qty', evidence['header_hits'])
        self.assertIn('description', evidence['header_hits'])

    def test_invoice_certificate_page_is_not_misclassified_from_amounts_alone(self):
        words = [
            self.word('Certificate', 100, 40, 100),
            self.word('Date', 100, 100), self.word('2026', 220, 100),
            self.word('Reference', 100, 150, 80), self.word('12345', 220, 150),
            self.word('Total', 100, 220), self.word('5000', 220, 220),
            self.word('Approved', 100, 300, 90),
        ]
        evidence = R._invoice_page_table_evidence(words)
        self.assertFalse(evidence['is_table'])

    def test_invoice_continuation_text_joins_previous_line_without_inventing_values(self):
        rows = [
            {'description': 'CAT6A cable', 'qty': 10.0, 'unit_price': 5.0, 'total': 50.0,
             'row_flags': [], 'source_cells': []},
            {'description': 'indoor UTP blue', 'qty': None, 'unit_price': None, 'total': None,
             'row_flags': [], 'source_cells': [{'text': 'indoor'}]},
        ]
        merged = R._invoice_join_continuation_rows(rows)
        self.assertEqual(len(merged), 1)
        self.assertIn('indoor UTP blue', merged[0]['description'])
        self.assertEqual(merged[0]['qty'], 10.0)
        self.assertIn('description_continuation_joined', merged[0]['row_flags'])


if __name__ == '__main__':
    unittest.main()

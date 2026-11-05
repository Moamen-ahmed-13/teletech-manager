import unittest

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


if __name__ == '__main__':
    unittest.main()

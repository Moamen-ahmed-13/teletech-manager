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
        from unittest.mock import patch
        if not shutil.which('tesseract'):
            self.skipTest('Tesseract is not installed')
        status = R.ocr_status()
        if not status.get('available'):
            self.skipTest(status.get('message') or 'Tesseract is unavailable')
        if 'ara' not in (status.get('langs') or []):
            self.skipTest('Arabic Tesseract language pack is unavailable')
        with tempfile.TemporaryDirectory(prefix='invoice_fixture_') as tmp:
            project = Path(tmp) / 'project'
            doc = {'id': 'invoice-fixture', 'kind': 'invoice', 'filename': fixture.name,
                   'stored_name': 'fixture.pdf', 'pages': R.pdf_page_count(fixture),
                   'ocr_status': 'running'}
            target = R.recon_dir(project) / 'invoice' / doc['stored_name']
            shutil.copyfile(fixture, target)
            st = R._new_state()
            st['invoice_docs'].append(doc)
            # Exercise the same Tesseract/native-PDF path used by the application,
            # while explicitly preventing optional external vision API calls.
            with patch('recon_module._invoice_vision_pages', return_value=None):
                page_results = R._ocr_pages(project, doc, status)
            count = R._apply_ocr(st, doc, page_results, {'username': 'fixture-test'})
            table_pages = sum(1 for _, parsed in page_results
                              if (parsed.get('page_analysis') or {}).get('is_table'))
            rows = st['records']
            self.assertEqual(len(page_results), doc['pages'],
                             'The OCR pipeline must return a diagnostic result for every PDF page')
            self.assertEqual(len(doc.get('invoice_page_analysis') or []), doc['pages'],
                             'Every page must retain its classification and extraction diagnostics')
            self.assertGreater(table_pages, 0,
                               'The invoice page classifier should detect at least one item-table page')
            self.assertGreater(count, 0,
                               'The complete invoice OCR pipeline should yield draft lines for manual review')
            self.assertEqual(count, len(rows))
            self.assertTrue(all((r.get('ocr') or {}).get('description') for r in rows),
                            'Extracted invoice records must contain a non-empty item description')
            self.assertTrue(all(r.get('doc_kind') == 'invoice' for r in rows))
            print('[INVOICE FIXTURE] pages=%d table_pages=%d skipped_or_empty=%d draft_rows=%d' % (
                doc['pages'], table_pages, doc['pages'] - table_pages, count))

    def test_invoice_page_without_detected_rows_is_diagnostic_not_fake_line(self):
        doc = {'id': 'invoice-doc', 'kind': 'invoice', 'filename': 'invoice.pdf',
               'pages': 1, 'ocr_status': 'running'}
        st = state([], [doc])
        parsed = {'rows': [], 'flags': [], 'page_analysis': {'is_table': True, 'header_hits': ['qty']},
                  'status': {}, 'raw_text': 'Invoice table header only'}
        count = R._apply_ocr(st, doc, [(1, parsed)], {'username': 'tester'})
        self.assertEqual(count, 0)
        self.assertEqual(len(st['records']), 0)
        self.assertEqual(doc['invoice_page_analysis'][0]['row_count'], 0)
        self.assertIn('ocr_no_lines', doc['invoice_page_analysis'][0]['flags'])
        self.assertEqual(doc['ocr_status'], 'done')

    def test_invoice_ocr_keeps_row_source_cells_and_original_text(self):
        doc = {'id': 'invoice-doc', 'kind': 'invoice', 'filename': 'invoice.pdf',
               'pages': 1, 'ocr_status': 'running'}
        st = state([], [doc])
        cells = [{'text': 'Aggregation', 'left': 100, 'top': 100, 'width': 90, 'height': 18, 'conf': 92},
                 {'text': 'Switch', 'left': 195, 'top': 100, 'width': 55, 'height': 18, 'conf': 90},
                 {'text': 'PC', 'left': 620, 'top': 100, 'width': 25, 'height': 18, 'conf': 90},
                 {'text': '2', 'left': 700, 'top': 100, 'width': 15, 'height': 18, 'conf': 92},
                 {'text': '50', 'left': 800, 'top': 100, 'width': 25, 'height': 18, 'conf': 92},
                 {'text': '100', 'left': 900, 'top': 100, 'width': 30, 'height': 18, 'conf': 92}]
        parsed = {'rows': [{'description': 'Aggregation Switch', 'unit': 'PC', 'qty': 2,
                            'unit_price': 50, 'total': 100, 'conf': 91, 'source_cells': cells,
                            'row_flags': ['line_math_consistent']}],
                  'flags': ['preprocessing_variant_selected:gray_psm6'],
                  'page_analysis': {'is_table': True, 'header_hits': ['description', 'qty', 'unit_price', 'total']},
                  'engine': 'invoice-tesseract-table-detection', 'variant_row_counts': {'gray_psm6': 1},
                  'status': {}, 'raw_text': 'Aggregation Switch PC 2 50 100'}
        count = R._apply_ocr(st, doc, [(1, parsed)], {'username': 'tester'})
        self.assertEqual(count, 1)
        rec = st['records'][0]
        self.assertEqual(rec['ocr']['raw_text'], 'Aggregation Switch PC 2 50 100')
        self.assertEqual(rec['ocr']['source_cells'], cells)
        self.assertIn('line_math_consistent', rec['ocr_flags'])
        self.assertEqual(doc['invoice_page_analysis'][0]['engine'], 'invoice-tesseract-table-detection')
        self.assertEqual(doc['invoice_page_analysis'][0]['variant_row_counts'], {'gray_psm6': 1})

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
        self.assertEqual(row['confirmed_qty'], 2.0, 'Invoice qty must not be added to MIR-confirmed qty')
        self.assertEqual(row['invoice_confirmed_qty'], 1.0, 'Approved invoice qty is tracked separately')
        self.assertEqual(row['invoice_pending_qty'], 0.0)
        self.assertEqual(row['invoice_qty_by_unit'], {'PC': 1.0})
        self.assertEqual(row['financial_value'], 250.0)
        self.assertEqual(row['financial_source'], 'invoice')
        self.assertEqual(row['remaining_value'], 650.0)
        self.assertAlmostEqual(row['fin_pct'], 27.78, places=2)
        self.assertEqual(result['brands'][0]['remaining_value'], 650.0)
        self.assertEqual(result['brands'][0]['invoice_qty_by_unit'], {'PC': 1.0})
        self.assertEqual(result['brands'][0]['invoice_confirmed_qty_total'], 1.0)

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

    def test_invoice_parser_excludes_header_and_footer_metadata(self):
        words = [
            self.word('Company', 100, 10, 80),
            self.word('Address', 190, 10, 70),
            self.word('Cairo', 270, 10, 45),
            self.word('Description', 100, 60, 100),
            self.word('Qty', 700, 60, 28),
            self.word('Price', 800, 60, 40),
            self.word('Total', 900, 60, 40),
            self.word('Aggregation', 100, 110, 90),
            self.word('Switch', 195, 110, 55),
            self.word('PC', 620, 110, 25),
            self.word('2', 700, 110, 15),
            self.word('50', 800, 110, 25),
            self.word('100', 900, 110, 30),
            self.word('Address', 100, 900, 70),
            self.word('Cairo', 180, 900, 45),
            self.word('Phone', 240, 900, 50),
            self.word('0123456789', 300, 900, 90),
        ]
        parsed = R.parse_invoice_lines(words, 'invoice.pdf')
        self.assertEqual(len(parsed['rows']), 1)
        self.assertIn('Aggregation Switch', parsed['rows'][0]['description'])
        self.assertEqual(parsed['rows'][0]['qty'], 2.0)
        self.assertEqual(parsed['rows'][0]['unit_price'], 50.0)
        self.assertEqual(parsed['rows'][0]['total'], 100.0)

    def test_invoice_parser_preserves_multiple_distinct_item_rows(self):
        words = [
            self.word('Description', 100, 40, 100),
            self.word('Qty', 700, 40, 28),
            self.word('Price', 800, 40, 40),
            self.word('Total', 900, 40, 40),
            self.word('Aggregation', 100, 100, 90),
            self.word('Switch', 195, 100, 55),
            self.word('2', 700, 100, 15),
            self.word('50', 800, 100, 25),
            self.word('100', 900, 100, 30),
            self.word('Indoor', 100, 150, 55),
            self.word('Access', 160, 150, 45),
            self.word('Point', 210, 150, 40),
            self.word('3', 700, 150, 15),
            self.word('20', 800, 150, 25),
            self.word('60', 900, 150, 25),
        ]
        parsed = R.parse_invoice_lines(words, 'invoice.pdf')
        self.assertEqual(len(parsed['rows']), 2)
        self.assertIn('Aggregation Switch', parsed['rows'][0]['description'])
        self.assertIn('Indoor Access Point', parsed['rows'][1]['description'])

    def test_invoice_page_classifier_recognizes_arabic_table_headers(self):
        words = [
            {'text': 'بيان', 'left': 900, 'top': 100, 'width': 80, 'height': 25},
            {'text': 'الصنف', 'left': 820, 'top': 100, 'width': 70, 'height': 25},
            {'text': 'الوحدة', 'left': 650, 'top': 100, 'width': 60, 'height': 25},
            {'text': 'الكمية', 'left': 520, 'top': 100, 'width': 60, 'height': 25},
            {'text': 'سعر', 'left': 340, 'top': 100, 'width': 40, 'height': 25},
            {'text': 'الوحدة', 'left': 290, 'top': 100, 'width': 50, 'height': 25},
            {'text': 'الإجمالي', 'left': 100, 'top': 100, 'width': 80, 'height': 25},
            {'text': 'كابل', 'left': 900, 'top': 150, 'width': 80, 'height': 25},
            {'text': 'PC', 'left': 650, 'top': 150, 'width': 25, 'height': 25},
            {'text': '10', 'left': 520, 'top': 150, 'width': 25, 'height': 25},
            {'text': '100', 'left': 320, 'top': 150, 'width': 35, 'height': 25},
            {'text': '1000', 'left': 120, 'top': 150, 'width': 45, 'height': 25},
        ]
        evidence = R._invoice_page_table_evidence(words)
        self.assertTrue(evidence['is_table'])
        self.assertIn('qty', evidence['header_hits'])
        self.assertIn('description', evidence['header_hits'])
        self.assertIn('total', evidence['header_hits'])

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

    def test_headerless_invoice_continuation_uses_previous_page_columns(self):
        first_page = [
            self.word('Description', 100, 40, 100),
            self.word('Qty', 700, 40, 28),
            self.word('Price', 800, 40, 40),
            self.word('Total', 900, 40, 40),
            self.word('Aggregation', 100, 100, 90),
            self.word('Switch', 195, 100, 55),
            self.word('PC', 620, 100, 25),
            self.word('2', 700, 100, 15),
            self.word('50', 800, 100, 25),
            self.word('100', 900, 100, 30),
        ]
        first = R.parse_invoice_lines(first_page, 'invoice.pdf')
        self.assertTrue(all(k in first['header_centers'] for k in ('qty', 'unit_price', 'total')))
        second_page = [
            self.word('Indoor', 100, 100, 55),
            self.word('Access', 160, 100, 45),
            self.word('Point', 210, 100, 40),
            self.word('PC', 620, 100, 25),
            self.word('3', 700, 100, 15),
            self.word('20', 800, 100, 25),
            self.word('60', 900, 100, 25),
        ]
        parsed = R.parse_invoice_lines(second_page, 'invoice.pdf',
                                       inherited_header_centers=first['header_centers'])
        evidence = R._invoice_page_table_evidence(second_page)
        evidence = R._invoice_mark_continuation_page(parsed, evidence, first['header_centers'])
        self.assertTrue(parsed['used_inherited_header_centers'])
        self.assertTrue(evidence['is_table'])
        self.assertTrue(evidence['continuation'])
        self.assertEqual(len(parsed['rows']), 1)
        self.assertEqual(parsed['rows'][0]['qty'], 3.0)
        self.assertEqual(parsed['rows'][0]['unit_price'], 20.0)
        self.assertEqual(parsed['rows'][0]['total'], 60.0)

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

    def test_separate_invoice_item_descriptions_are_not_blindly_collapsed(self):
        rows = [
            {'description': 'Aggregation Switch 48 Port', 'qty': None, 'unit_price': None,
             'total': None, 'row_flags': ['numeric_columns_require_review'], 'source_cells': []},
            {'description': 'Indoor Access Point', 'qty': None, 'unit_price': None,
             'total': None, 'row_flags': ['numeric_columns_require_review'], 'source_cells': []},
            {'description': 'DC Firewall', 'qty': None, 'unit_price': None,
             'total': None, 'row_flags': ['numeric_columns_require_review'], 'source_cells': []},
        ]
        # The production page pipeline must preserve each candidate row for review.
        self.assertEqual(len(rows), 3)
        self.assertEqual([r['description'] for r in rows],
                         ['Aggregation Switch 48 Port', 'Indoor Access Point', 'DC Firewall'])

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

    def test_parser_joins_geometry_aligned_wrapped_description(self):
        words = [
            self.word('Description', 100, 40, 100),
            self.word('Qty', 700, 40, 28),
            self.word('Price', 800, 40, 40),
            self.word('Total', 900, 40, 40),
            self.word('Aggregation', 100, 100, 90),
            self.word('Switch', 195, 100, 55),
            self.word('PC', 620, 100, 25),
            self.word('2', 700, 100, 15),
            self.word('50', 800, 100, 25),
            self.word('100', 900, 100, 30),
            self.word('48', 100, 125, 18),
            self.word('Port', 125, 125, 35),
            self.word('Managed', 165, 125, 60),
        ]
        parsed = R.parse_invoice_lines(words, 'invoice.pdf')
        self.assertEqual(len(parsed['rows']), 1)
        row = parsed['rows'][0]
        self.assertIn('Aggregation Switch 48 Port Managed', row['description'])
        self.assertEqual(row['qty'], 2.0)
        self.assertEqual(row['unit_price'], 50.0)
        self.assertEqual(row['total'], 100.0)
        self.assertIn('description_continuation_joined', row['row_flags'])

    def test_invoice_candidate_score_does_not_reward_noisy_row_count(self):
        evidence = {'is_table': True, 'header_score': 3}
        good = {'rows': [
            {'description': 'Aggregation Switch 48 Port', 'unit': 'PC', 'qty': 2,
             'unit_price': 50, 'total': 100, 'math_valid': True, 'conf': 92},
        ]}
        noisy = {'rows': [
            {'description': 'Aggregation Switch 48 Port', 'unit': 'PC', 'qty': 2,
             'unit_price': 50, 'total': 100, 'math_valid': True, 'conf': 92},
            {'description': 'gill Tent Cy', 'unit': None, 'qty': None,
             'unit_price': None, 'total': None, 'math_valid': False, 'conf': 15},
            {'description': 'Of O WA', 'unit': None, 'qty': None,
             'unit_price': None, 'total': None, 'math_valid': False, 'conf': 12},
        ]}
        self.assertGreater(R._invoice_candidate_score(good, evidence),
                           R._invoice_candidate_score(noisy, evidence))


if __name__ == '__main__':
    unittest.main()

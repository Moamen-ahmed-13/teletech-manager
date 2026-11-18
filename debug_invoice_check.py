import json
from pathlib import Path

import pymupdf

import recon_module as R

root = Path('c:/Users/Omar/Downloads/CAD EXAMPELES/TELETECH_CAD_UI_LATEST_VERSION_9222026/teletech_ui_patch (2)/teletech_ui_patch/auth version/auth version')
file_path = root / 'فاتورة رقم 1 بنى سويف لامر التوريد القديم.pdf'
summary_path = root / 'debug_invoice_summary.json'
print('root', root)
print('exists', file_path.exists())
print('file', file_path)
print('ocr', R.ocr_status())

pages = []
with pymupdf.open(str(file_path)) as pdf:
    for page_no in range(len(pdf)):
        page = pdf[page_no]
        words = []
        for item in page.get_text('words'):
            if len(item) >= 5 and str(item[4]).strip():
                x0, y0, x1, y1 = map(float, item[:4])
                words.append({'text': str(item[4]), 'left': x0, 'top': y0,
                              'width': max(1.0, x1 - x0), 'height': max(1.0, y1 - y0),
                              'conf': 100.0})
        pages.append({'page_no': page_no + 1, 'words': words, 'table': R._invoice_page_table_evidence(words), 'parsed': R.parse_invoice_lines(words, file_path.name)})

rows = []
for p in pages:
    rows.extend(p['parsed'].get('rows') or [])
summary = {
    'pages': len(pages),
    'page_tables': [p['page_no'] for p in pages if p['table'].get('is_table')],
    'row_count': len(rows),
    'sample_rows': rows[:5],
}
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print('saved_summary', summary_path)
print(json.dumps(summary, ensure_ascii=False, indent=2)[:2000])

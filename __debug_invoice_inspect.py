import pymupdf
from pathlib import Path
import recon_module as R

pdf_path = Path('.') / 'فاتورة رقم 1 بنى سويف لامر التوريد القديم.pdf'
print('pdf_path', pdf_path)
print('exists', pdf_path.exists())
doc = pymupdf.open(str(pdf_path))
page = doc[0]
words = []
for item in page.get_text('words'):
    if len(item) >= 5 and str(item[4]).strip():
        words.append({
            'text': str(item[4]),
            'left': float(item[0]),
            'top': float(item[1]),
            'width': max(1.0, float(item[2]) - float(item[0])),
            'height': max(1.0, float(item[3]) - float(item[1])),
            'conf': 100.0,
        })
print('word_count', len(words))
print('sample', words[:80])
print('table', R._invoice_page_table_evidence(words))
print('parsed', R.parse_invoice_lines(words, pdf_path.name))
doc.close()

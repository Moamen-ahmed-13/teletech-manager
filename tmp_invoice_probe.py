import os
import fitz
import recon_module as R

files = [f for f in os.listdir('.') if 'فاتورة' in f or 'invoice' in f.lower()]
print('files', files[:10])
pdf_name = files[0]
print('using', pdf_name)
doc = fitz.open(pdf_name)
page = doc[0]
words = page.get_text('words')
print('word_count', len(words))
rows = R.parse_invoice_lines([
    {'text': w[4], 'left': w[0], 'top': w[1], 'width': w[2] - w[0], 'height': w[3] - w[1], 'conf': 95.0}
    for w in words
], pdf_name)['rows']
print('row_count', len(rows))
for r in rows[:12]:
    print(r)

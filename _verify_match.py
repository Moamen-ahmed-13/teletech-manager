import recon_module

st = {
    'boq': {'items': [
        {'id': 'b1', 'sn': '36', 'item': 'Item 36', 'reference': 'R36', 'brand': 'X', 'part': 'P36'},
        {'id': 'b2', 'sn': '37', 'item': 'Item 37', 'reference': 'R37', 'brand': 'Y', 'part': 'P37'},
    ]}
}
rec = {'doc_kind': 'invoice', 'ocr': {'contract_item_no': '036.0'}, 'description': 'Something else'}

print(recon_module._item_no_key('036.0'))
print(recon_module._score_candidates(st, rec))

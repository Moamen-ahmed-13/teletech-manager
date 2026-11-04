"""Post-export verification for deterministic As-Built DXF edits.

This module checks the actual exported DXF against the requested DELETE/MOVE
operations. It never edits either drawing.
"""
from __future__ import annotations
import hashlib
import math
from collections import Counter
from pathlib import Path

def _sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()

def _key(item, xy=None, digits=2):
    p=xy if xy is not None else item.get('world') or [0,0]
    return (str(item.get('name') or '').upper(), round(float(p[0]),digits), round(float(p[1]),digits))

def validate_export(original_path, output_path, applied, original_sha256=None, tolerance=2.0):
    """Validate expected operations by comparing endpoint INSERT inventories."""
    from dxfedit import Dxf
    original_path=Path(original_path); output_path=Path(output_path)
    result={'ok':False,'original_unchanged':False,'output_exists':output_path.is_file(),
            'checks':[],'errors':[],'expected_deletes':0,'verified_deletes':0,
            'expected_moves':0,'verified_moves':0}
    if not output_path.is_file():
        result['errors'].append('Output DXF was not created.')
        return result
    if not original_path.is_file():
        result['errors'].append('Original DXF is missing.')
        return result
    before_hash=_sha256(original_path)
    result['original_unchanged']=(original_sha256 is None or before_hash==original_sha256)
    if not result['original_unchanged']:
        result['errors'].append('Original DXF hash changed during export.')
    try:
        before=Dxf(str(original_path)).instances()
        after=Dxf(str(output_path)).instances()
    except Exception as exc:
        result['errors'].append('Could not reopen DXF for verification: '+str(exc))
        return result
    # Position matching is tolerant of DXF float serialization and nested transforms.
    def exists(items, name, xy):
        return any(str(x.get('name') or '').upper()==str(name or '').upper() and
                   math.hypot(float(x['world'][0])-float(xy[0]),float(x['world'][1])-float(xy[1]))<=tolerance
                   for x in items)
    for change in applied or []:
        op=str(change.get('op') or '').upper()
        name=change.get('block')
        if op=='DELETE':
            result['expected_deletes']+=1
            old=change.get('world') or [0,0]
            removed=exists(before,name,old) and not exists(after,name,old)
            if removed: result['verified_deletes']+=1
            else: result['errors'].append('DELETE not verified for %s at %s.'%(name,old))
            result['checks'].append({'op':'DELETE','block':name,'verified':bool(removed)})
        elif op=='MOVE':
            result['expected_moves']+=1
            old=change.get('from') or [0,0]; new=change.get('to') or [0,0]
            moved=exists(before,name,old) and exists(after,name,new) and not exists(after,name,old)
            if moved: result['verified_moves']+=1
            else: result['errors'].append('MOVE not verified for %s from %s to %s.'%(name,old,new))
            result['checks'].append({'op':'MOVE','block':name,'from':old,'to':new,'verified':bool(moved)})
    result['original_unchanged']=result['original_unchanged'] and _sha256(original_path)==before_hash
    if not result['original_unchanged'] and 'Original DXF hash changed during export.' not in result['errors']:
        result['errors'].append('Original DXF changed after verification began.')
    result['ok']=bool(result['output_exists'] and result['original_unchanged'] and not result['errors'] and
                      result['verified_deletes']==result['expected_deletes'] and
                      result['verified_moves']==result['expected_moves'])
    return result

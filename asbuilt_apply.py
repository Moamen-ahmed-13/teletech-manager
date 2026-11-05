"""End-to-end As-Built PDF -> registered DXF workflow (CLI; the UI uses the same core).

Usage:
  python asbuilt_apply.py --dxf ORIGINAL.dxf --pdf page1.pdf page2.pdf --out AsBuilt_Output

For ONE floor at a time: pass that floor's DXF and only that floor's PDF pages.
The original DXF is never modified; a new ``*_AsBuilt.dxf`` and
``asbuilt_report.json`` are written to --out.

Pink/Red  -> endpoint deleted      (Patch Panel SHIFT)
Yellow    -> endpoint deleted      (Patch Panel RESERVE)
Orange    -> endpoint moved to the registered tip of the blue ballpoint arrow
Green     -> no CAD change
Anything uncertain (registration, match, arrow) goes to the review list.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile

import asbuilt_pipeline as PL
import asbuilt_registration as R
import dxfedit
from asbuilt_markups import detect_markups  # noqa: F401  (re-exported for the unit tests)

FALLBACK_BLOCKS = {'D-V', 'D-W', 'DD', 'D-V BOX', 'CAM1', 'CAM 2', 'PROJECTOR', 'AV SYSTEM DOOM CAMERA',
                   'AV SYSTEM RACK', 'EC-TP', 'ES-CCTV'}


def load_endpoints(dxf_path, drawing):
    """Counted endpoints exactly as TELETECH analyses them (same set as the UI).

    Falls back to a block-name list only if the analyzer cannot be imported."""
    try:
        os.environ.setdefault('TELETECH_AUTH_DB', os.path.join(tempfile.gettempdir(), 'teletech_cli_users.db'))
        import server as S
        res = S.analyze_cad(str(dxf_path), '', '', 'Auto Detect')
        recs = [r for r in (res.get('mapped') or []) if str(r.get('status') or '').upper() in {'MAPPED', 'COUNTED', ''}]
        eps = []
        for r in recs:
            r = dict(r)
            r['target_id'] = f"{r.get('occurrence_id')}|{r.get('handle')}"
            eps.append(r)
        return PL.endpoints_from_targets(eps), 'analyzer'
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f'[asbuilt_apply] analyzer unavailable ({exc}); using block-name fallback')
        eps = []
        for i in drawing.instances(FALLBACK_BLOCKS):
            eps.append({'target_id': '|'.join(p['start'] for p in i['path']), 'handle': i['handle'],
                        'ancestor_handles': [p['start'] for p in i['path'][:-1]], 'block': i['name'],
                        'x': i['world'][0], 'y': i['world'][1], 'insert_x': i['world'][0],
                        'insert_y': i['world'][1], 'depth': i['depth']})
        return PL.endpoints_from_targets(eps), 'block-name fallback'


def run(dxf_path, pdfs, out_dir, endpoints=None, log=None):
    os.makedirs(out_dir, exist_ok=True)
    drawing = dxfedit.Dxf(dxf_path)
    source = 'provided'
    if endpoints is None:
        endpoints, source = load_endpoints(dxf_path, drawing)
    if not endpoints:
        raise RuntimeError('No endpoint instances were found in the DXF.')
    PL.attach_footprints(endpoints, drawing)
    geom = R.load_cad_geometry(dxf_path)

    report = {'dxf': os.path.basename(dxf_path), 'endpoint_source': source, 'endpoint_count': len(endpoints),
              'pages': [], 'review': [], 'changes': [], 'applied': []}
    plan, page_no = [], 0
    for pdf in pdfs:
        for img in PL.render_pdf_native(pdf):
            page_no += 1
            res = PL.analyze_page(img, geom, endpoints, page_number=page_no, log=log)
            for rec in res['markups']:
                ch = PL.plan_change(rec, endpoints)
                if ch:
                    ch['page'] = page_no
                    plan.append(ch)
                elif rec['state'] not in ('NO_CHANGE',):
                    report['review'].append({'page': page_no, 'color': rec['color'], 'state': rec['state'],
                                             'reason': rec.get('reason') or rec.get('relocation_reason', ''),
                                             'relocation_state': rec.get('relocation_state'),
                                             'cad_xy': rec.get('cad_xy')})
            res['markups'] = [PL.strip_private(r) for r in res['markups']]
            report['pages'].append({'pdf': os.path.basename(str(pdf)), **res})
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(dxf_path))[0] + '_AsBuilt.dxf')
    result = PL.apply_changes(dxf_path, out, plan) if plan else None
    if result is None:  # nothing to change: still emit a verified identical copy
        result = PL.apply_changes(dxf_path, out, [])
    report.update(output=out, applied=result['applied'], apply_failed=result['failed'],
                  verification=result['verification'], original_unchanged=result['original_unchanged'])
    for f in result['failed']:
        report['review'].append({'page': f.get('page'), 'color': f.get('color'), 'state': 'APPLY_FAILED',
                                 'reason': f.get('error')})
    report['summary'] = {'pages': page_no, 'applied': len(result['applied']), 'review_items': len(report['review']),
                         'verified': bool(result['verification']['ok']),
                         'registration_failed_pages': sum(1 for p in report['pages'] if not p['registration_ok'])}
    with open(os.path.join(out_dir, 'asbuilt_report.json'), 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    return report


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Apply As-Built markups to a COPY of the original DXF.')
    ap.add_argument('--dxf', required=True, help='Original floor DXF')
    ap.add_argument('--pdf', nargs='+', required=True, help='As-Built PDF(s) of THIS floor; every page is processed')
    ap.add_argument('--out', required=True, help='Output folder')
    a = ap.parse_args()
    rep = run(a.dxf, a.pdf, a.out)
    print(json.dumps({'summary': rep['summary'], 'applied': rep['applied'], 'review': rep['review'],
                      'verification': rep['verification'], 'original_unchanged': rep['original_unchanged'],
                      'output': rep['output']}, ensure_ascii=False, indent=2, default=str))

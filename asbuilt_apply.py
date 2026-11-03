"""Conservative end-to-end As-Built PDF -> registered DXF workflow.

Usage:
  python asbuilt_apply.py --dxf ORIGINAL.dxf --pdf page1.pdf page2.pdf --out AsBuilt_Output

The original DXF is never overwritten. Only confidently registered pages and
unambiguous endpoint matches are applied. Unsupported/ambiguous operations are
written to asbuilt_report.json for review.
"""
from __future__ import annotations
import os, json, hashlib, math, argparse
import cv2
import numpy as np
import pypdfium2 as pdfium
import asbuilt_registration as R
import asbuilt_arrow as AR
import dxfedit
import dxfmini
from render import render

ENDPOINT_BLOCKS = {'D-V', 'D-W', 'DD', 'D-V BOX', 'ES-cctv'}
MATCH_MAX = 900.0
AMBIG_RATIO = 1.4

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def detect_markups(image):
    """Find colored highlighter regions. Red and magenta both normalize to pink."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    height, width = image.shape[:2]
    hue = hsv[:, :, 0]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    masks = {
        'orange': ((hue >= 6) & (hue <= 22) & (saturation >= 95) & (value >= 150)),
        'yellow': ((hue >= 23) & (hue <= 38) & (saturation >= 95) & (value >= 150)),
        'green': ((hue >= 40) & (hue <= 90) & (saturation >= 95) & (value >= 150)),
        'pink': (((hue >= 160) | (hue <= 5)) & (saturation >= 95) & (value >= 150)),
    }
    out = []
    kernel = np.ones((9, 9), np.uint8)
    for color, mask in masks.items():
        closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
        count, labels, stats, centers = cv2.connectedComponentsWithStats(closed, 8)
        for k in range(1, count):
            x, y, w, h, area = [int(v) for v in stats[k]]
            if not 120 <= area <= 9000:
                continue
            legend = x > width * 0.70 and y < height * 0.20 and w / max(h, 1) >= 2.0
            out.append({'color': color, 'bbox': [x, y, w, h],
                        'center': [float(centers[k][0]), float(centers[k][1])],
                        'area': area, 'legend': bool(legend)})
    return out

def cad_raster(polylines, bbox, width=3000):
    image, pixels_per_unit = render(polylines, bbox, width, 24)
    x0, y0, _, y1 = [float(v) for v in bbox]
    px_to_world = np.array([[1.0 / pixels_per_unit, 0.0, x0 - 24.0 / pixels_per_unit],
                            [0.0, -1.0 / pixels_per_unit, y1 + 24.0 / pixels_per_unit]], dtype=float)
    return image, px_to_world

def _page_bgr(page):
    bitmap = page.render(scale=1.0)
    arr = bitmap.to_numpy()
    if arr.ndim == 3 and arr.shape[2] == 4:
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    if arr.ndim == 3 and arr.shape[2] == 3:
        return arr.copy()
    raise ValueError(f'Unsupported PDF page image shape: {arr.shape}')

def run(dxf_path, pdfs, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    original_hash = file_sha256(dxf_path)
    drawing = dxfedit.Dxf(dxf_path)
    editor = dxfedit.Editor(drawing)
    instances = [item for item in drawing.instances() if item['name'].upper() in ENDPOINT_BLOCKS]
    if not instances:
        raise RuntimeError('No supported endpoint block instances were found in the DXF.')

    entities, blocks = dxfmini.read_dxf(dxf_path)
    polylines = dxfmini.flatten(entities, blocks)
    renderable = [np.asarray(line, dtype=float) for line in polylines if len(line) >= 2]
    if not renderable:
        raise RuntimeError('The DXF contains no renderable linework.')
    all_points = np.concatenate(renderable, axis=0)
    bbox = (float(np.percentile(all_points[:, 0], 1)), float(np.percentile(all_points[:, 1], 1)),
            float(np.percentile(all_points[:, 0], 99)), float(np.percentile(all_points[:, 1], 99)))
    cad_image, px_to_world = cad_raster(polylines, bbox)

    report = {'pages': [], 'changes': [], 'review': [], 'applied': [], 'page_failures': [],
              'original_sha256': original_hash, 'original_unchanged': None}
    used = set()
    page_number = 0

    for pdf_path in pdfs:
        document = pdfium.PdfDocument(pdf_path)
        for pdf_page_index in range(len(document)):
            page_number += 1
            try:
                image = _page_bgr(document[pdf_page_index])
                registration = R.register_page_to_cad(image, cad_image, px_to_world)
            except Exception as exc:
                report['page_failures'].append({'page': page_number, 'pdf': os.path.basename(pdf_path), 'error': str(exc)})
                report['review'].append({'page': page_number, 'state': 'REGISTRATION_FAILED', 'reason': str(exc)})
                continue

            page_report = {'page': page_number, 'pdf': os.path.basename(pdf_path),
                           'pdf_page_index': pdf_page_index + 1,
                           'registration_ok': bool(registration.get('ok')),
                           'reason': registration.get('reason', ''),
                           'diag': registration.get('diagnostics', {}), 'markups': []}
            report['pages'].append(page_report)
            if not registration.get('ok'):
                report['review'].append({'page': page_number, 'state': 'REGISTRATION_FAILED',
                                         'reason': registration.get('reason', 'Registration rejected')})
                continue

            marks = [m for m in detect_markups(image) if not m['legend']]
            orange_marks = [m for m in marks if m['color'] == 'orange']
            arrows = AR.detect_blue_arrow_tips(image, orange_marks)
            orange_index = {id(mark): idx + 1 for idx, mark in enumerate(orange_marks)}

            for mark in marks:
                world = R.page_px_to_cad(registration, *mark['center'])
                rec = {'color': mark['color'], 'page_px': [round(v, 1) for v in mark['center']],
                       'cad_xy': [round(world[0], 2), round(world[1], 2)], 'state': 'PENDING'}
                page_report['markups'].append(rec)
                if mark['color'] == 'green':
                    rec.update(state='NO_CHANGE', action='Patch Panel unchanged')
                    continue

                candidates = sorted((math.hypot(item['world'][0] - world[0], item['world'][1] - world[1]), idx)
                                    for idx, item in enumerate(instances))
                if not candidates:
                    rec.update(state='NOT_MATCHED', reason='No endpoint candidates in the selected DXF.')
                    report['review'].append({'page': page_number, 'color': mark['color'], 'state': rec['state'], 'reason': rec['reason']})
                    continue
                d1, best_idx = candidates[0]
                d2 = candidates[1][0] if len(candidates) > 1 else float('inf')
                target = instances[best_idx]
                rec['match'] = {'block': target['name'], 'depth': target['depth'],
                                'world': [round(v, 2) for v in target['world']],
                                'distance': round(d1, 2), 'next_distance': round(d2, 2)}

                def reject(state, reason):
                    rec.update(state=state, reason=reason)
                    report['review'].append({'page': page_number, 'color': mark['color'],
                                             'cad_xy': rec['cad_xy'], 'state': state, 'reason': reason})

                if d1 > MATCH_MAX:
                    reject('NOT_MATCHED', f'Nearest endpoint is {d1:.1f} CAD units away (limit {MATCH_MAX:.0f}).')
                    continue
                if d2 < AMBIG_RATIO * max(d1, 1e-6):
                    reject('AMBIGUOUS', f'Two endpoints are too close in match score ({d1:.1f}, {d2:.1f}).')
                    continue
                if best_idx in used:
                    reject('DUPLICATE', 'This endpoint has already been assigned to another markup.')
                    continue

                try:
                    if mark['color'] == 'pink':
                        editor.delete(target)
                        used.add(best_idx)
                        rec.update(state='CONFIRMED', action='SITE_REMOVE', patch_action='SHIFT')
                        report['changes'].append({'op': 'SITE_REMOVE', 'page': page_number, 'color': 'pink',
                                                  'patch_action': 'SHIFT', 'block': target['name'],
                                                  'world': target['world'], 'dxf_handle_path': [p['start'] for p in target['path']]})
                        report['applied'].append({'color': 'pink', 'op': 'DELETE', 'block': target['name'],
                                                  'depth': target['depth'], 'world': target['world']})
                    elif mark['color'] == 'yellow':
                        editor.delete(target)
                        used.add(best_idx)
                        rec.update(state='CONFIRMED', action='SITE_REMOVE', patch_action='RESERVE')
                        report['changes'].append({'op': 'SITE_REMOVE', 'page': page_number, 'color': 'yellow',
                                                  'patch_action': 'RESERVE', 'block': target['name'],
                                                  'world': target['world'], 'dxf_handle_path': [p['start'] for p in target['path']]})
                        report['applied'].append({'color': 'yellow', 'op': 'DELETE', 'block': target['name'],
                                                  'depth': target['depth'], 'world': target['world']})
                    elif mark['color'] == 'orange':
                        arrow = arrows.get(orange_index[id(mark)], {})
                        rec['arrow'] = arrow
                        if arrow.get('status') != 'CONFIRMED':
                            reject(arrow.get('status', 'ARROW_NOT_CONFIRMED'), arrow.get('reason', 'Blue relocation arrow not confirmed.'))
                            continue
                        destination = R.page_px_to_cad(registration, *arrow['tip_px'])
                        editor.move(target, destination)
                        used.add(best_idx)
                        rec.update(state='CONFIRMED', action='MOVE_ENDPOINT',
                                   destination_cad=[round(destination[0], 2), round(destination[1], 2)],
                                   patch_action='KEEP')
                        report['changes'].append({'op': 'MOVE_ENDPOINT', 'page': page_number, 'color': 'orange',
                                                  'patch_action': 'KEEP', 'block': target['name'],
                                                  'from': target['world'], 'x': float(destination[0]), 'y': float(destination[1])})
                        report['applied'].append({'color': 'orange', 'op': 'MOVE', 'block': target['name'],
                                                  'depth': target['depth'], 'from': target['world'],
                                                  'to': [float(destination[0]), float(destination[1])]})
                except Exception as exc:
                    reject('APPLY_FAILED', str(exc))

    output = os.path.join(out_dir, os.path.splitext(os.path.basename(dxf_path))[0] + '_AsBuilt.dxf')
    editor.write(output)
    report['output'] = output
    report['original_unchanged'] = file_sha256(dxf_path) == original_hash
    report['summary'] = {'pages_processed': page_number, 'changes_applied': len(report['applied']),
                         'review_items': len(report['review']), 'registration_failures': len(report['page_failures'])}
    with open(os.path.join(out_dir, 'asbuilt_report.json'), 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report, editor

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Apply reviewed As-Built changes to a copy of the original DXF.')
    parser.add_argument('--dxf', required=True, help='Original floor DXF')
    parser.add_argument('--pdf', nargs='+', required=True, help='One or more As-Built PDF files; every page is processed')
    parser.add_argument('--out', required=True, help='Output folder')
    args = parser.parse_args()
    result, _ = run(args.dxf, args.pdf, args.out)
    print(json.dumps({'summary': result['summary'], 'applied': result['applied'],
                      'review': result['review'], 'original_unchanged': result['original_unchanged'],
                      'output': result['output']}, ensure_ascii=False, indent=2))

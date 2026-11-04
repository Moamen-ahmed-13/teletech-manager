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

ENDPOINT_BLOCKS = {'D-V', 'D-W', 'DD', 'D-V BOX', 'D·V BOX', 'D.V BOX', 'DV BOX', 'EC-TP', 'ES-CCTV'}
MATCH_MAX = 900.0
AMBIG_RATIO = 1.4

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def markup_policy(color):
    """Canonical site / patch-panel / label action for each As-Built color."""
    return {
        'pink': {'site_action': 'DELETE', 'patch_panel_action': 'NO_SLOT', 'label_action': 'SHIFT'},
        'yellow': {'site_action': 'KEEP', 'patch_panel_action': 'RESERVE_SLOT', 'label_action': 'KEEP'},
        'orange': {'site_action': 'MOVE', 'patch_panel_action': 'KEEP_SLOT', 'label_action': 'KEEP'},
        'green': {'site_action': 'KEEP', 'patch_panel_action': 'KEEP_SLOT', 'label_action': 'KEEP'},
    }.get(str(color or '').lower())


def relocation_destination_conflict(source, destination, instances, source_index, tolerance):
    """Return a reason when a move would be a no-op or collide with another endpoint."""
    if math.hypot(float(destination[0])-float(source[0]), float(destination[1])-float(source[1])) <= tolerance:
        return 'Arrow tip maps back to the source endpoint; relocation is a no-op.'
    for idx, item in enumerate(instances):
        if idx == source_index:
            continue
        point = item.get('world') or [0, 0]
        if math.hypot(float(destination[0])-float(point[0]), float(destination[1])-float(point[1])) <= tolerance:
            return 'Arrow tip overlaps another existing endpoint; destination requires review.'
    return ''


def relocation_destination_match(destination, instances, source_index, endpoint_spacing):
    """Conservative CAD endpoint validation for a registered arrow tip.

    A valid arrowhead does not prove that its mapped point is a valid destination.
    Require a close endpoint candidate and separation from the runner-up.
    """
    spacing=max(float(endpoint_spacing or 0.0),1e-6)
    candidates=[]
    for idx,item in enumerate(instances):
        if idx==source_index:
            continue
        point=item.get('world') or [0,0]
        distance=math.hypot(float(point[0])-float(destination[0]),float(point[1])-float(destination[1]))
        candidates.append((distance,idx))
    candidates.sort()
    if not candidates:
        return {'ok':False,'reason':'No other CAD endpoint is available to validate the arrow destination.','confidence':0.0}
    d1,idx=candidates[0]
    d2=candidates[1][0] if len(candidates)>1 else float('inf')
    units=d1/spacing
    proximity=math.exp(-0.72*max(0.0,units))
    separation=1.0 if not math.isfinite(d2) else max(0.0,min(1.0,(d2-d1)/max(d2/spacing,0.25)/1.0))
    confidence=0.18+0.68*proximity+0.14*separation
    margin=1.0 if not math.isfinite(d2) else max(0.0,(d2-d1)/max(spacing,1e-6))
    ok=bool(units<=0.35 and confidence>=0.78 and margin>=0.04)
    return {'ok':ok,'candidate_index':idx,'distance':round(d1,2),
            'distance_in_endpoint_spacing':round(units,3),'next_distance':round(d2,2),
            'confidence':round(confidence,2),'margin_in_endpoint_spacing':round(margin,3),
            'reason':'' if ok else ('Registered arrow tip is not close enough to a distinct CAD endpoint, or the nearest destination is ambiguous; manual review required.')}


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
    # Detect a compact legend stack anywhere in the page margins. Requiring
    # the canonical five-swatch order avoids suppressing isolated field marks.
    legend_boxes = []
    comps = {}
    for color, mask in masks.items():
        n, lab, st, cen = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        comps[color] = []
        for k in range(1, n):
            x, y, ww, hh, area = [int(v) for v in st[k]]
            if area >= 80 and ww >= 8 and hh >= 5:
                comps[color].append({'x':x,'y':y,'w':ww,'h':hh,'cx':float(cen[k][0]),'cy':float(cen[k][1])})
    for p in comps['pink']:
        if not (p['x'] < width*.22 or p['x']+p['w'] > width*.78 or p['y'] < height*.18 or p['y']+p['h'] > height*.82):
            continue
        greens=[g for g in comps['green'] if g['cy']>p['cy'] and abs(g['cx']-p['cx'])<max(180,width*.12)]
        for g in greens:
            yellows=[z for z in comps['yellow'] if z['cy']>g['cy'] and abs(z['cx']-p['cx'])<max(180,width*.12)]
            for z in yellows:
                oranges=sorted([o for o in comps['orange'] if o['cy']>z['cy'] and abs(o['cx']-p['cx'])<max(180,width*.12)],key=lambda q:q['cy'])
                if len(oranges)<2: continue
                seq=[p,g,z,oranges[0],oranges[1]]
                gaps=[seq[i+1]['cy']-seq[i]['cy'] for i in range(4)]
                if min(gaps)<=0 or max(gaps)/max(min(gaps),1)>3.2: continue
                x0=min(q['x'] for q in seq); y0=min(q['y'] for q in seq)
                x1=max(q['x']+q['w'] for q in seq); y1=max(q['y']+q['h'] for q in seq)
                if (x1-x0)>max(260,width*.18) or (y1-y0)>height*.22: continue
                legend_boxes.append((x0,y0,x1-x0,y1-y0))
    out = []
    kernel = np.ones((9, 9), np.uint8)
    for color, mask in masks.items():
        closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
        count, labels, stats, centers = cv2.connectedComponentsWithStats(closed, 8)
        for k in range(1, count):
            x, y, w, h, area = [int(v) for v in stats[k]]
            if not 120 <= area <= 9000:
                continue
            cx, cy = float(centers[k][0]), float(centers[k][1])
            legend = any(x0 <= cx <= x0+bw and y0 <= cy <= y0+bh for x0,y0,bw,bh in legend_boxes)
            out.append({'color': color, 'bbox': [x, y, w, h],
                        'center': [cx, cy], 'area': area, 'legend': bool(legend)})
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
    nearest_spacings = []
    for i, item in enumerate(instances):
        distances = [math.hypot(float(item['world'][0])-float(other['world'][0]),
                                float(item['world'][1])-float(other['world'][1]))
                     for j, other in enumerate(instances) if j != i]
        positive = [d for d in distances if d > 1e-8]
        if positive:
            nearest_spacings.append(min(positive))
    nearest_spacings.sort()
    endpoint_spacing = nearest_spacings[len(nearest_spacings)//2] if nearest_spacings else 1.0
    relocation_guard = max(1e-6, float(endpoint_spacing) * 0.05)

    entities, blocks = dxfmini.read_dxf(dxf_path)
    polylines = dxfmini.flatten(entities, blocks)
    renderable = [np.asarray(line, dtype=float) for line in polylines if len(line) >= 2]
    if not renderable:
        raise RuntimeError('The DXF contains no renderable linework.')
    all_points = np.concatenate(renderable, axis=0)
    bbox = (float(np.percentile(all_points[:, 0], 1)), float(np.percentile(all_points[:, 1], 1)),
            float(np.percentile(all_points[:, 0], 99)), float(np.percentile(all_points[:, 1], 99)))
    cad_image, px_to_world = cad_raster(polylines, bbox)
    cad_image = cv2.cvtColor(cad_image, cv2.COLOR_BGR2GRAY)

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
                diag = registration.get('diagnostics') or {}
                grid = diag.get('inlier_grid_5x5') or []
                gh, gw = image.shape[:2]
                gx = max(0, min(4, int(float(mark['center'][0]) / max(gw, 1) * 5)))
                gy = max(0, min(4, int(float(mark['center'][1]) / max(gh, 1) * 5)))
                local_support = (int(sum(grid[yy][xx]
                    for yy in range(max(0, gy-1), min(5, gy+2))
                    for xx in range(max(0, gx-1), min(5, gx+2))))
                    if len(grid) == 5 and all(len(row) == 5 for row in grid) else 0)
                rec = {'color': mark['color'], 'page_px': [round(v, 1) for v in mark['center']],
                       'cad_xy': [round(world[0], 2), round(world[1], 2)],
                       'local_registration_support': local_support, 'state': 'PENDING'}
                page_report['markups'].append(rec)
                policy = markup_policy(mark['color']) or {}
                if policy.get('site_action') == 'KEEP':
                    patch_action = policy.get('patch_panel_action')
                    rec.update(state='NO_CHANGE', action='KEEP_SITE',
                               patch_action=patch_action, label_action=policy.get('label_action'))
                    report['changes'].append({'op': 'KEEP', 'page': page_number, 'color': mark['color'],
                                              'patch_action': patch_action,
                                              'label_action': policy.get('label_action')})
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

                if local_support < 3:
                    reject('LOCAL_REGISTRATION_WEAK', f'Only {local_support} local registration inliers support this markup; destructive CAD edit blocked.')
                    continue
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
                    elif mark['color'] == 'orange':
                        arrow = arrows.get(orange_index[id(mark)], {})
                        rec['arrow'] = arrow
                        if arrow.get('status') != 'CONFIRMED':
                            reject(arrow.get('status', 'ARROW_NOT_CONFIRMED'), arrow.get('reason', 'Blue relocation arrow not confirmed.'))
                            continue
                        destination = R.page_px_to_cad(registration, *arrow['tip_px'])
                        conflict = relocation_destination_conflict(
                            target['world'], destination, instances, best_idx, relocation_guard)
                        if conflict:
                            reject('DESTINATION_CONFLICT', conflict)
                            continue
                        destination_match=relocation_destination_match(destination,instances,best_idx,endpoint_spacing)
                        rec['destination_match']=destination_match
                        if not destination_match.get('ok'):
                            reject('DESTINATION_REVIEW_REQUIRED',destination_match.get('reason') or 'CAD destination match is not sufficiently confident.')
                            continue
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
    try:
        from asbuilt_validation import validate_export
        report['validation'] = validate_export(dxf_path, output, report['applied'], original_sha256=original_hash)
    except Exception as validation_error:
        report['validation'] = {'ok': False, 'errors': [f'{type(validation_error).__name__}: {validation_error}']}
    report['summary'] = {'pages_processed': page_number, 'changes_applied': len(report['applied']),
                         'review_items': len(report['review']), 'registration_failures': len(report['page_failures']),
                         'validation_ok': bool((report.get('validation') or {}).get('ok'))}
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

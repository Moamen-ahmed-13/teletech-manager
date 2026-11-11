"""TELETECH As-Built core: page registration -> markup/endpoint matching ->
blue-arrow destination -> verified DXF edit.

Shared by the server (UI scan + Generate) and the asbuilt_apply.py CLI so both
behave identically.  Nothing here modifies the original DXF.

States reported per markup (what the UI shows)
    CONFIRMED            endpoint matched unambiguously (and, for orange, the
                         arrow destination is registered)
    AMBIGUOUS            two endpoints (or two arrows) are equally plausible
    NOT_DETECTED         orange without a usable blue arrow / no endpoint evidence
    REGISTRATION_FAILED  the page could not be registered to the DXF
    DUPLICATE            another markup already claimed that endpoint
    NO_CHANGE            green (Patch Panel status unchanged, CAD untouched)
    LEGEND               colour key in the page corner (ignored)
"""
import math
import os

import cv2
import numpy as np

import asbuilt_arrow as AR
import asbuilt_registration as R
import dxfedit
from asbuilt_markups import detect_markups

# scoring / gating
# Keep the established Orange relocation matcher unchanged. Pink/Yellow are
# destructive deletions and use the stricter footprint-evidence gate below.
MIN_MATCH_SCORE = 0.35
MIN_DELETE_MATCH_SCORE = 0.48
AMBIGUITY_MARGIN = 0.12
MIN_FOOTPRINT_POINTS = 8
MIN_DELETE_COVERAGE = 0.16
STRONG_DELETE_COVERAGE = 0.32
CENTROID_REACH_MM = 700.0
MIN_MOVE_MM = 400.0

PATCH_ACTION = {'pink': 'SHIFT', 'yellow': 'RESERVE', 'orange': 'KEEP', 'green': 'KEEP'}


# ------------------------------------------------------------------------------
# rendering PDFs at the scan's native resolution
# ------------------------------------------------------------------------------
def render_pdf_native(pdf_path, max_long=4200):
    """Render every page at the resolution of its embedded scan (annotations
    such as ink strokes are included), capped at ``max_long`` px."""
    pages = []
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        try:
            for page in doc:
                scale = 150.0 / 72.0
                try:
                    imgs = page.get_images(full=True)
                    if imgs:
                        info = doc.extract_image(imgs[0][0])
                        scale = max(info['width'] / page.rect.width, info['height'] / page.rect.height)
                except Exception:
                    pass
                long_side = max(page.rect.width, page.rect.height) * scale
                if long_side > max_long:
                    scale *= max_long / long_side
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
                pages.append(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if pix.n == 3 else arr[:, :, :3][:, :, ::-1].copy())
        finally:
            doc.close()
        return pages
    except ImportError:
        pass
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(pdf_path))
    for i in range(len(document)):
        page = document[i]
        w, h = page.get_size()
        scale = min(2.0, max_long / max(w, h))
        arr = page.render(scale=scale).to_numpy()
        pages.append(cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR) if arr.shape[2] == 4 else arr.copy())
    return pages


# ------------------------------------------------------------------------------
# endpoints
# ------------------------------------------------------------------------------
def endpoints_from_targets(targets):
    """Server ``_asbuilt_targets`` / analyzer records -> endpoint dicts."""
    out = []
    for t in targets:
        handles = list(t.get('ancestor_handles') or []) + [t.get('handle')]
        handles = [str(h) for h in handles if str(h)]
        if not handles:
            continue
        ix = t.get('insert_x', t.get('x'))
        iy = t.get('insert_y', t.get('y'))
        out.append({'id': t.get('target_id') or '|'.join(handles), 'handles': handles,
                    'block': t.get('block') or '', 'x': float(t.get('x') or 0.0), 'y': float(t.get('y') or 0.0),
                    'insert': [float(ix or 0.0), float(iy or 0.0)],
                    'label': t.get('label') or '', 'service': t.get('service') or '',
                    'rack': t.get('rack') or '', 'floor': t.get('floor') or t.get('schedule_floor') or '',
                    'depth': int(t.get('depth') or 0), 'raw': t})
    return out


def attach_footprints(endpoints, drawing):
    for e in endpoints:
        try:
            e['_foot'] = dxfedit.footprint_points(drawing, e['handles'])
        except Exception:
            e['_foot'] = np.zeros((0, 2))
    return endpoints


# ------------------------------------------------------------------------------
# page analysis
# ------------------------------------------------------------------------------
def _blob_distance_fn(mask_tuple, margin=40):
    x, y, w, h, blob = mask_tuple
    m = np.zeros((h + 2 * margin, w + 2 * margin), np.uint8)
    m[margin:margin + h, margin:margin + w] = blob.astype(np.uint8)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8))
    dist = cv2.distanceTransform(1 - m, cv2.DIST_L2, 3)

    def f(px, py):
        lx, ly = int(round(px - x + margin)), int(round(py - y + margin))
        if 0 <= lx < dist.shape[1] and 0 <= ly < dist.shape[0]:
            return float(dist[ly, lx]), bool(m[ly, lx])
        return float('inf'), False
    return f


def _score_endpoints(reg, mark, endpoints, P):
    """Rank endpoints for one markup blob.  Returns list of dict sorted by score."""
    wx, wy = reg.to_world([mark['center']])[0]
    dist_fn = _blob_distance_fn(mark['_mask'])
    rows = []
    for i, e in enumerate(endpoints):
        dc = math.hypot(e['x'] - wx, e['y'] - wy)
        d_blob, inside = dist_fn(P[i][0], P[i][1])
        foot = e.get('_foot')
        foot_n = int(len(foot)) if foot is not None else 0
        cov = 0.0
        if foot_n:
            fp = reg.to_photo(foot)
            hit = sum(1 for q in fp if dist_fn(q[0], q[1])[1])
            cov = hit / float(foot_n)
        if dc > CENTROID_REACH_MM * 2.2 and not inside and cov == 0:
            continue
        prox = max(0.0, 1.0 - dc / CENTROID_REACH_MM)
        if mark['color'] in ('pink', 'yellow'):
            # Footprint coverage carries most of the weight. The centre-in-blob
            # signal is secondary because wide marker strokes can cross neighbours.
            score = 0.62 * cov + 0.28 * (1.0 if inside else 0.0) + 0.10 * prox
        else:
            # Preserve the already validated Orange source/relocation scoring.
            score = 0.5 * cov + 0.3 * (1.0 if inside else 0.0) + 0.2 * prox
        rows.append({'index': i, 'score': round(score, 3), 'coverage': round(cov, 3),
                     'footprint_points': foot_n, 'anchor_inside': inside,
                     'centroid_dist_mm': round(dc, 1), 'blob_dist_px': None if d_blob == float('inf') else round(d_blob, 1)})
    rows.sort(key=lambda r: (-r['score'], r['centroid_dist_mm']))
    return rows, (float(wx), float(wy))


def analyze_page(page_bgr, geom, endpoints, registration=None, page_number=1, log=None):
    """Analyse one page.  Returns dict(registration, markups[], legend_count)."""
    pts = [(e['x'], e['y']) for e in endpoints]
    reg = registration or R.register_page_to_dxf(page_bgr, geom, extra_world_points=pts, log=log)
    marks = detect_markups(page_bgr)
    legend = [m for m in marks if m['legend']]
    marks = [m for m in marks if not m['legend']]
    H, W = page_bgr.shape[:2]
    result = {'page_number': page_number, 'registration': reg.summary(), 'registration_ok': reg.ok,
              'image_size': [W, H], 'legend_count': len(legend), 'markups': []}
    orange = [m for m in marks if m['color'] == 'orange']
    arrows = AR.detect_blue_arrow_tips(page_bgr, orange) if orange else {}
    oindex = {id(m): i + 1 for i, m in enumerate(orange)}
    P = reg.to_photo(np.array(pts)) if (reg.ok and pts) else None

    order = {'pink': 0, 'yellow': 1, 'orange': 2, 'green': 3}
    marks.sort(key=lambda m: (order[m['color']], m['bbox'][1], m['bbox'][0]))
    counters = {}
    for m in marks:
        c = m['color']
        counters[c] = counters.get(c, 0) + 1
        rec = {'markup_id': f"MK-{c.upper()}-P{page_number:02d}-{counters[c]:03d}", 'page_number': page_number,
               'color': c, 'bbox_px': m['bbox'], 'center_px': [round(v, 1) for v in m['center']],
               'area_px': m['area'], 'patch_action': PATCH_ACTION[c], 'state': 'PENDING', 'reason': '',
               'candidates': [], '_mark': m}
        result['markups'].append(rec)
        if not reg.ok:
            rec.update(state='REGISTRATION_FAILED', reason='; '.join(reg.reasons) or 'Page could not be registered to the DXF.')
            if c == 'orange':
                rec['arrow'] = arrows.get(oindex[id(m)], {})
                rec['relocation_state'] = 'REGISTRATION_FAILED'
            continue
        wx, wy = reg.to_world([m['center']])[0]
        rec['cad_xy'] = [round(float(wx), 2), round(float(wy), 2)]
        if c == 'green':
            rec.update(state='NO_CHANGE', reason='Green = cable pulled: Patch Panel status unchanged, CAD untouched.')
            continue
        rows, _ = _score_endpoints(reg, m, endpoints, P) if endpoints else ([], (wx, wy))
        rec['candidates'] = [dict(r, target_id=endpoints[r['index']]['id'], block=endpoints[r['index']]['block'],
                                  label=endpoints[r['index']]['label']) for r in rows[:4]]
        # Pink/Yellow are destructive removals: require a real sampled CAD
        # footprint and visible overlap. Orange retains its established matcher
        # because its source/destination relocation was already validated.
        if c in ('pink', 'yellow'):
            eligible = [r for r in rows if r.get('footprint_points', 0) >= MIN_FOOTPRINT_POINTS
                        and ((r.get('anchor_inside') and r.get('coverage', 0.0) >= MIN_DELETE_COVERAGE)
                             or r.get('coverage', 0.0) >= STRONG_DELETE_COVERAGE)]
            required_score = MIN_DELETE_MATCH_SCORE
        else:
            eligible = rows
            required_score = MIN_MATCH_SCORE
        if not eligible or eligible[0]['score'] < required_score:
            near = rows[0]['centroid_dist_mm'] if rows else None
            rec.update(state='NOT_DETECTED',
                       reason='No CAD symbol has sufficient footprint overlap with this markup; manual review required'
                       + (f' (nearest centre is {near:.0f} mm away).' if near is not None else '.'))
        elif (len(eligible) > 1 and (eligible[0]['score'] - eligible[1]['score']) < AMBIGUITY_MARGIN
              and not _clear_centre_winner(eligible)):
            rec.update(state='AMBIGUOUS', reason='Two CAD symbol footprints are similarly covered by this markup '
                       f"({eligible[0]['score']:.2f} vs {eligible[1]['score']:.2f}); manual review required.")
        else:
            best = _clear_centre_winner(eligible) or eligible[0]
            e = endpoints[best['index']]
            rec.update(state='CONFIRMED', target_index=best['index'], target_id=e['id'], block=e['block'],
                       label=e['label'], match_score=best['score'],
                       confidence=round(min(0.99, best['score'] * (0.75 + 0.25 * reg.confidence)), 2),
                       reason='CAD symbol footprint overlaps the highlighter with sufficient evidence.')
        if c == 'orange':
            arrow = arrows.get(oindex[id(m)], {})
            rec['arrow'] = arrow
            _resolve_relocation(rec, arrow, reg, geom, endpoints)
    _dedupe(result['markups'])
    return result


def _clear_centre_winner(rows):
    """Tie-break for near-equal coverage scores: the symbol whose centre is
    clearly (>=1.8x) closest to the highlighter blob wins.  Returns the row or None."""
    close = [r for r in rows if rows[0]['score'] - r['score'] < AMBIGUITY_MARGIN and r['score'] >= MIN_MATCH_SCORE]
    if len(close) < 2:
        return None
    close.sort(key=lambda r: r.get('centroid_dist_mm', 1e9))
    a, b = close[0], close[1]
    if a.get('centroid_dist_mm') is None or b.get('centroid_dist_mm') is None:
        return None
    if b['centroid_dist_mm'] >= 1.8 * max(a['centroid_dist_mm'], 50.0):
        return a
    return None


def _resolve_relocation(rec, arrow, reg, geom, endpoints):
    st = arrow.get('status')
    if st == 'AMBIGUOUS':
        rec['relocation_state'] = 'AMBIGUOUS'
        rec['relocation_reason'] = arrow.get('reason', '')
        return
    if st != 'CONFIRMED':
        rec['relocation_state'] = 'NOT_DETECTED'
        rec['relocation_reason'] = arrow.get('reason') or 'No blue relocation arrow detected.'
        return
    tip = arrow['tip_px']
    dest = reg.to_world([tip])[0]
    x0, x1, y0, y1 = geom['bbox']
    if not (x0 <= dest[0] <= x1 and y0 <= dest[1] <= y1):
        rec['relocation_state'] = 'NOT_DETECTED'
        rec['relocation_reason'] = 'Arrow tip lies outside the registered drawing area.'
        return
    rec['arrow_destination_px'] = tip
    rec['arrow_destination_xy'] = [round(float(dest[0]), 2), round(float(dest[1]), 2)]
    rec['arrow_destination_norm'] = arrow.get('tip_norm')
    rec['relocation_state'] = 'CONFIRMED'
    rec['relocation_reason'] = ''
    if rec.get('state') == 'CONFIRMED':
        e = endpoints[rec['target_index']]
        d = math.hypot(dest[0] - e['x'], dest[1] - e['y'])
        rec['move_distance_mm'] = round(d, 1)
        if d < MIN_MOVE_MM:
            rec['relocation_state'] = 'NOT_DETECTED'
            rec['relocation_reason'] = f'Arrow destination is only {d:.0f} mm from the point; treated as not a relocation.'


def _dedupe(markups):
    """One endpoint can only be claimed by one markup."""
    best = {}
    for r in markups:
        if r.get('state') == 'CONFIRMED':
            k = r['target_index']
            if k not in best or r['match_score'] > best[k]['match_score']:
                best[k] = r
    for r in markups:
        if r.get('state') == 'CONFIRMED' and best[r['target_index']] is not r:
            r.update(state='DUPLICATE', reason='Another markup covers this endpoint more strongly.')


def strip_private(rec):
    return {k: v for k, v in rec.items() if not k.startswith('_')}


# ------------------------------------------------------------------------------
# DXF application + verification
# ------------------------------------------------------------------------------
def plan_change(rec, endpoints):
    """Turn a CONFIRMED markup record into a DXF operation (or None)."""
    if rec.get('state') != 'CONFIRMED':
        return None
    e = endpoints[rec['target_index']]
    c = rec['color']
    if c in ('pink', 'yellow'):
        return {'op': 'DELETE', 'color': c, 'patch_action': PATCH_ACTION[c], 'handles': e['handles'],
                'block': e['block'], 'anchor': [e['x'], e['y']], 'insert': e['insert'], 'label': e['label']}
    if c == 'orange' and rec.get('relocation_state') == 'CONFIRMED':
        dx, dy = rec['arrow_destination_xy'][0] - e['x'], rec['arrow_destination_xy'][1] - e['y']
        return {'op': 'MOVE', 'color': c, 'patch_action': 'KEEP', 'handles': e['handles'], 'block': e['block'],
                'anchor': [e['x'], e['y']], 'insert': e['insert'], 'dest_anchor': rec['arrow_destination_xy'],
                'dest_insert': [e['insert'][0] + dx, e['insert'][1] + dy], 'label': e['label']}
    return None


def apply_changes(dxf_in, dxf_out, changes):
    """Apply DELETE/MOVE ops to a COPY of dxf_in and verify the written file.

    changes: [{'op':'DELETE'|'MOVE','handles':[root..target],'block','insert':[x,y],
               'dest_insert':[x,y] (MOVE)}]
    Returns {'output','applied':[..],'failed':[..],'verification':{..},
             'original_unchanged':bool}.  Raises only for unusable input files.
    """
    before = dxfedit.sha256(dxf_in)
    drawing = dxfedit.Dxf(dxf_in)
    editor = dxfedit.Editor(drawing)
    applied, failed, deletes, moves = [], [], [], []
    for ch in changes:
        try:
            tgt = drawing.resolve(ch['handles'])
            if ch.get('block') and tgt['name'].upper() != str(ch['block']).upper():
                raise ValueError(f"handle path resolves to {tgt['name']}, expected {ch['block']}")
            if math.hypot(tgt['world'][0] - ch['insert'][0], tgt['world'][1] - ch['insert'][1]) > 1.0:
                raise ValueError('INSERT position in the DXF differs from the analysed position; refusing edit.')
            if ch['op'] == 'DELETE':
                editor.delete(tgt)
                deletes.append({'path': list(ch['handles'])})
            elif ch['op'] == 'MOVE':
                editor.move(tgt, ch['dest_insert'])
                moves.append({'path': list(ch['handles']),
                              'delta': [ch['dest_insert'][0] - tgt['world'][0], ch['dest_insert'][1] - tgt['world'][1]]})
            else:
                raise ValueError(f"unsupported op {ch['op']}")
            applied.append({**{k: v for k, v in ch.items() if k != 'raw'}, 'world': tgt['world']})
        except Exception as exc:
            failed.append({**{k: v for k, v in ch.items() if k != 'raw'}, 'error': str(exc)})
    editor.write(dxf_out)
    ver = dxfedit.verify_output(dxf_in, dxf_out, deletes, moves)
    return {'output': str(dxf_out), 'applied': applied, 'failed': failed, 'verification': ver,
            'original_unchanged': dxfedit.sha256(dxf_in) == before}

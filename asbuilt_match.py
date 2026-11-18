"""Registered highlighter-marker -> CAD endpoint matching for As-Built sheets.

Why this exists
---------------
A technician circles / highlights the *symbol* of an endpoint with a blob that is
typically 1-2 m across in CAD units.  Measuring from the blob CENTRE to the nearest
endpoint (and converting that to a confidence via the median endpoint spacing)
produces low confidences for perfectly correct marks and lets unrelated margin
writing (the colour legend) match random endpoints metres away.

This module instead works in registered CAD space:

* the marker bbox is mapped through the verified page->CAD affine to a quadrilateral;
* an endpoint is a candidate when its CAD position lies inside (or within a small
  registration tolerance of) that quadrilateral;
* several endpoints under one marker, or none under it, are reported explicitly
  as AMBIGUOUS / NOT_MATCHED instead of being guessed;
* a wide, flat colour blob with no endpoint anywhere near it is classified as
  LEGEND_OR_MARGIN and must not be treated as a real markup.

Pure functions: no server state, easy to unit-test.
"""
from __future__ import annotations

import math

MATCH_OK_CONF = 0.78          # the UI only auto-allows rows at or above this
LEGEND_ASPECT = 2.0           # swatches in the legend are long and flat


def _aff(aff, u, v):
    a, b, c, d, tx, ty = aff
    return (a * u + b * v + tx, c * u + d * v + ty)


def marker_polygon(bbox_px, page_w, page_h, affine):
    """Map a page-pixel bbox to a CAD quadrilateral through the normalized affine."""
    x, y, w, h = [float(v) for v in bbox_px[:4]]
    pw, ph = max(float(page_w), 1.0), max(float(page_h), 1.0)
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return [_aff(affine, px * 1000.0 / pw, py * 1000.0 / ph) for px, py in pts]


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 1e-12 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def distance_to_polygon(p, poly):
    """0 when p is inside the (convex) polygon, otherwise distance to its boundary."""
    sign = 0
    inside = True
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        s = 1 if cross > 0 else (-1 if cross < 0 else 0)
        if s != 0:
            if sign == 0:
                sign = s
            elif s != sign:
                inside = False
                break
    if inside:
        return 0.0
    return min(_seg_dist(p, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def polygon_dims(poly):
    w = math.dist(poly[0], poly[1])
    h = math.dist(poly[1], poly[2])
    return max(w, h), max(min(w, h), 1e-6)


def polygon_center(poly):
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def _option(t, conf, d_center, d_blob, inside):
    return {
        'target_id': t.get('target_id'), 'candidate_key': t.get('candidate_key'),
        'floor': t.get('floor'), 'label': t.get('label') or t.get('block') or t.get('target_id'),
        'service': t.get('service', ''), 'block': t.get('block', ''), 'rack': t.get('rack', ''),
        'x': t.get('x'), 'y': t.get('y'),
        'cad_x_norm': int(t.get('nx') or 0), 'cad_y_norm': int(t.get('ny') or 0),
        'distance_norm': round(float(d_center), 1),
        'distance_to_marker': round(float(d_blob), 1),
        'inside_marker': bool(inside),
        'distance_in_endpoint_spacing': None,
        'semantic_match': False,
        'match_confidence': round(float(conf), 2),
        'editable_direct': bool(t.get('editable_direct')),
    }


def match_marker(bbox_px, page_w, page_h, affine, catalog_rows):
    """Match one colour marker to the endpoints of its floor.

    Returns {'status','reason','options','marker_cad_center','marker_size_cad','inside_count'}
    status: MATCHED | AMBIGUOUS | NOT_MATCHED | LEGEND_OR_MARGIN
    """
    poly = marker_polygon(bbox_px, page_w, page_h, affine)
    long_side, short_side = polygon_dims(poly)
    center = polygon_center(poly)
    margin = max(120.0, 0.12 * short_side)       # registration tolerance
    scored = []
    for t in catalog_rows or []:
        try:
            p = (float(t.get('x') or 0.0), float(t.get('y') or 0.0))
        except Exception:
            continue
        d_blob = distance_to_polygon(p, poly)
        scored.append((d_blob, math.hypot(p[0] - center[0], p[1] - center[1]), t))
    info = {'marker_cad_center': [round(center[0], 1), round(center[1], 1)],
            'marker_size_cad': [round(long_side, 1), round(short_side, 1)]}
    if not scored:
        return dict(status='NOT_MATCHED', reason='No CAD endpoints exist for this floor.', options=[], inside_count=0, **info)
    scored.sort(key=lambda z: (z[0], z[1]))
    inside = [s for s in scored if s[0] <= margin]
    near_limit = max(1500.0, 1.2 * long_side)
    aspect = long_side / short_side

    if not inside:
        d0 = scored[0][0]
        if d0 > near_limit:
            if aspect >= LEGEND_ASPECT:
                return dict(status='LEGEND_OR_MARGIN',
                            reason=(f'Wide colour swatch (aspect {aspect:.1f}) with no CAD endpoint within '
                                    f'{d0:.0f} units: legend / margin writing, not a drawing markup.'),
                            options=[], inside_count=0, **info)
            return dict(status='NOT_MATCHED',
                        reason=f'No CAD endpoint under this mark (nearest is {d0:.0f} units away).',
                        options=[], inside_count=0, **info)
        # near, but not under, the blob: accept only if close relative to the blob and well separated
        d1 = scored[1][0] if len(scored) > 1 else float('inf')
        rel = d0 / max(long_side, 1.0)
        sep = (d1 - d0) / max(d1, 1.0) if math.isfinite(d1) else 1.0
        if rel <= 0.15 and sep >= 0.5:
            conf, status, why = 0.80, 'MATCHED', f'Nearest endpoint is {d0:.0f} units from the marker edge.'
        elif rel <= 0.30:
            conf, status, why = 0.70, 'AMBIGUOUS', f'Endpoint {d0:.0f} units outside the marker; confirm manually.'
        else:
            return dict(status='NOT_MATCHED',
                        reason=f'No CAD endpoint under this mark (nearest is {d0:.0f} units away).',
                        options=[], inside_count=0, **info)
        opts = [_option(scored[0][2], conf, scored[0][1], d0, False)]
        for d_b, d_c, t in scored[1:4]:
            opts.append(_option(t, max(0.05, conf * 0.5), d_c, d_b, False))
        return dict(status=status, reason=why, options=opts, inside_count=0, **info)

    inside.sort(key=lambda z: z[1])               # closest to the blob centre first
    if len(inside) == 1:
        conf, status, why = 0.93, 'MATCHED', 'Exactly one CAD endpoint lies under this mark.'
    else:
        c1, c2 = inside[0][1], inside[1][1]
        if c2 >= 1.6 * max(c1, 1.0):
            conf, status, why = 0.82, 'MATCHED', (f'{len(inside)} endpoints under the mark; the nearest to its '
                                                  f'centre is clearly closer ({c1:.0f} vs {c2:.0f} units).')
        else:
            conf, status, why = 0.62, 'AMBIGUOUS', (f'{len(inside)} endpoints lie under this mark and none is clearly '
                                                    f'the one marked; choose manually.')
    opts = [_option(inside[0][2], conf, inside[0][1], inside[0][0], True)]
    for d_b, d_c, t in inside[1:4]:
        opts.append(_option(t, conf if status == 'AMBIGUOUS' else max(0.05, conf * 0.55), d_c, d_b, True))
    if len(opts) < 4:
        for d_b, d_c, t in scored:
            if len(opts) >= 4:
                break
            if all(o['target_id'] != t.get('target_id') for o in opts):
                opts.append(_option(t, 0.15, d_c, d_b, False))
    return dict(status=status, reason=why, options=opts, inside_count=len(inside), **info)

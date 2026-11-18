"""Blue-ballpoint relocation-arrow detector for Orange As-Built markups.

detect_blue_arrow_tips(image_path_or_bgr, orange_regions) -> {orange_index(1-based): result}

result keys
    status      CONFIRMED | AMBIGUOUS | NOT_CONFIRMED | NOT_FOUND
                (only CONFIRMED may be used to move CAD geometry)
    confidence  0..1
    tip_px      [x, y]  apex of the arrowhead  == the intended new location
    tip_norm    [x, y]  tip in 0..1000 page coordinates
    tail_px     [x, y]  where the stroke starts (at the orange marker)
    arrowhead_detected, head_score, stroke_bbox, stroke_length_px,
    orange_to_stroke_gap_px, candidates (diagnostics), reason

How it works (and why it is not "the farthest blue pixel")
    * Ballpoint ink is identified by chroma: B - max(R, G).  Printed CAD
      linework is neutral grey, so it does not pass, even on a warm/blue-cast
      phone photo where the old hue/saturation test picked up wall lines.
    * The search is local to each orange marker; broken strokes are chained
      when their gap is a few pixels; strokes that are closer to a *different*
      orange marker are not stolen.
    * The stroke is skeletonised.  The arrowhead is the place where the ink
      suddenly becomes much thicker than the shaft (filled triangle / barbs);
      the tip is the apex of that head along the local travel direction.
    * A stroke without a head, a head that points back at the marker, or two
      equally good arrows are NOT confirmed (the caller routes them to review).
"""
import math

import cv2
import numpy as np

try:
    from skimage.morphology import skeletonize
except Exception:  # pragma: no cover
    skeletonize = None

BLUENESS_MIN = 6          # B - max(R,G); printed grey lines are ~0-3
DARK_MARGIN = 18          # ink must be this much darker than local paper (V channel)
HEAD_THICKNESS_RATIO = 1.9
CONFIRM_CONF = 0.60
AMBIGUITY_MARGIN = 0.10


def _blue_ink_window(win_bgr):
    """Boolean ink mask for ballpoint-blue strokes inside a small window."""
    w = win_bgr.astype(np.int16)
    b, g, r = w[:, :, 0], w[:, :, 1], w[:, :, 2]
    v = w.max(axis=2).astype(np.uint8)
    k = max(15, (min(v.shape) // 6) | 1)
    paper = cv2.medianBlur(v, min(k, 101) | 1)
    m = (((b - np.maximum(g, r)) >= BLUENESS_MIN) &
         (v.astype(np.int16) < paper.astype(np.int16) - DARK_MARGIN)).astype(np.uint8)
    # drop thin axis-aligned printed-line residue (1-3 px wide, long)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
    for i in range(1, n):
        x, y, ww, hh, a = [int(t) for t in st[i]]
        if min(ww, hh) <= 3 and max(ww, hh) >= 14:
            m[lab == i] = 0
        elif a < 4:
            m[lab == i] = 0
    return m


def _endpoints(sk):
    k = cv2.filter2D(sk.astype(np.uint8), -1, np.ones((3, 3), np.float32), borderType=cv2.BORDER_CONSTANT)
    ys, xs = np.where((sk > 0) & (k == 2))
    return list(zip(xs.tolist(), ys.tolist()))


def _rect_gap(xs, ys, bb):
    bx, by, bw, bh = [float(v) for v in bb[:4]]
    dx = np.maximum(np.maximum(bx - xs, 0), xs - (bx + bw))
    dy = np.maximum(np.maximum(by - ys, 0), ys - (by + bh))
    return float(np.min(np.hypot(dx, dy)))


def _analyse_group(xs, ys, ocx, ocy, dim, max_gap, gap, min_len, max_len):
    """Return a candidate dict or None for one chained blue stroke group."""
    x0, y0 = int(xs.min()) - 6, int(ys.min()) - 6
    w, h = int(xs.max()) - x0 + 7, int(ys.max()) - y0 + 7
    m = np.zeros((h, w), np.uint8)
    m[ys - y0, xs - x0] = 1
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    if skeletonize is None:
        return None
    sk = skeletonize(m > 0).astype(np.uint8)
    skp = np.argwhere(sk > 0)                       # (row, col)
    if len(skp) < 8:
        return None
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 3)
    shaft_w = max(1.0, float(np.median(dt[sk > 0])))
    max_dt = float(dt.max())
    path_len = float(len(skp))
    eps = _endpoints(sk)
    if len(eps) < 2:
        return None
    def d_o(p):
        return math.hypot(p[0] + x0 - ocx, p[1] + y0 - ocy)
    eps.sort(key=d_o)
    tail = eps[0]

    # --- arrowhead: thick ink blob far from the orange marker ----------------
    thick = (dt >= max(2.5, 0.55 * max_dt)).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(thick, 8)
    best = None
    for k in range(1, n):
        area = int(st[k, cv2.CC_STAT_AREA])
        cx, cy = cen[k]
        dist = math.hypot(cx + x0 - ocx, cy + y0 - ocy)
        if dist < min_len * 0.8:
            continue                                # pen-start stub inside the marker
        local_max = float(dt[lab == k].max())
        ratio = local_max / shaft_w
        sc = ratio * (1.0 + min(area, 200) / 200.0)
        if best is None or sc > best[0]:
            best = (sc, k, area, cx, cy, local_max, ratio)
    head_found = False
    head_dt = max_dt
    if best is None:
        # plain line: far end is only a *candidate*, never confirmed
        far = eps[-1]
        hx, hy = far
        rh = max(4.0, shaft_w * 2)
        ratio, area = 1.0, 0
    else:
        _, k, area, hx, hy, local_max, ratio = best
        head_dt = local_max
        rh = max(4.0, math.sqrt(area / math.pi) * 1.6)
        head_found = bool(ratio >= HEAD_THICKNESS_RATIO and area >= 25)
    # --- local direction of travel into the head ----------------------------
    d = np.hypot(skp[:, 1] - hx, skp[:, 0] - hy)
    beh = skp[(d > rh * 0.9) & (d < rh * 3.8)]
    if len(beh) >= 3:
        vx, vy = hx - float(beh[:, 1].mean()), hy - float(beh[:, 0].mean())
    else:
        vx, vy = hx - tail[0], hy - tail[1]
    nv = math.hypot(vx, vy)
    if nv < 1e-6:
        return None
    vx, vy = vx / nv, vy / nv
    # apex = farthest ink pixel of the head blob along the travel direction
    yy, xx = np.nonzero(m)
    near = np.hypot(xx - hx, yy - hy) <= rh * 1.7
    if not near.any():
        return None
    proj = (xx[near] - hx) * vx + (yy[near] - hy) * vy
    j = int(np.argmax(proj))
    tipx, tipy = float(xx[near][j]) + x0, float(yy[near][j]) + y0
    tailx, taily = tail[0] + x0, tail[1] + y0
    length = math.hypot(tipx - tailx, tipy - taily)
    if length < min_len or length > max_len:
        return None
    away = (tipx - ocx) * vx + (tipy - ocy) * vy > 0 and \
        math.hypot(tipx - ocx, tipy - ocy) > math.hypot(tailx - ocx, taily - ocy)
    straight = min(1.0, length / max(path_len, 1.0))
    conf = (0.35 * min(1.0, ratio / 3.5) + 0.15 * min(1.0, area / 120.0) +
            0.20 * min(1.0, length / (dim * 0.05)) + 0.15 * max(0.0, 1 - gap / max_gap) +
            0.15 * straight)
    if not head_found:
        conf *= 0.6
    if not away:
        conf *= 0.4
    return {'conf': float(conf), 'tip': (tipx, tipy), 'tail': (tailx, taily), 'head': head_found,
            'away': bool(away), 'length': float(length), 'gap': float(gap),
            'head_ratio': round(float(ratio), 2), 'head_area': int(area),
            'bbox': [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]}


def detect_blue_arrow_tips(image, orange_regions):
    bgr = image if isinstance(image, np.ndarray) else cv2.imread(str(image), cv2.IMREAD_COLOR)
    if bgr is None:
        return {}
    H, W = bgr.shape[:2]
    dim = float(min(H, W))
    regs = [(r.get('bbox') if isinstance(r, dict) else r) for r in (orange_regions or [])]
    out = {}
    for oi, bb in enumerate(regs, 1):
        if not bb:
            out[oi] = {'status': 'NOT_FOUND', 'confidence': 0.0, 'arrowhead_detected': False,
                       'reason': 'Orange region has no bounding box.'}
            continue
        ox, oy, ow, oh = [float(v) for v in bb[:4]]
        ocx, ocy = ox + ow / 2.0, oy + oh / 2.0
        R = int(max(230, 0.15 * dim))
        x0, y0 = max(0, int(ocx - R)), max(0, int(ocy - R))
        x1, y1 = min(W, int(ocx + R)), min(H, int(ocy + R))
        m = _blue_ink_window(bgr[y0:y1, x0:x1])
        others = [b for oj, b in enumerate(regs, 1) if oj != oi and b]
        for b in others:                           # never use another marker's own area
            bx, by, bw, bh = [int(v) for v in b[:4]]
            m[max(0, by - 3 - y0):max(0, by + bh + 3 - y0), max(0, bx - 3 - x0):max(0, bx + bw + 3 - x0)] = 0
        link = int(max(7, dim * 0.005)) | 1
        grown = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (link, link)))
        n, lab, st, _ = cv2.connectedComponentsWithStats(grown, 8)
        max_gap = max(34.0, dim * 0.03)
        min_len = max(26.0, dim * 0.011)
        max_len = dim * 0.45
        cands = []
        for k in range(1, n):
            ys, xs = np.where((lab == k) & (m > 0))
            if len(xs) < 20:
                continue
            xs = xs + x0
            ys = ys + y0
            gap = _rect_gap(xs, ys, (ox, oy, ow, oh))
            if gap > max_gap:
                continue
            if any(_rect_gap(xs, ys, b) + 3 < gap for b in others):
                continue                           # belongs to another orange marker
            c = _analyse_group(xs, ys, ocx, ocy, dim, max_gap, gap, min_len, max_len)
            if c:
                cands.append(c)
        if not cands:
            out[oi] = {'status': 'NOT_FOUND', 'confidence': 0.0, 'arrowhead_detected': False,
                       'reason': 'No blue ballpoint stroke with a usable direction starts at this orange marker.'}
            continue
        cands.sort(key=lambda z: -z['conf'])
        b = cands[0]
        amb = len(cands) > 1 and cands[1]['head'] and cands[1]['away'] and (b['conf'] - cands[1]['conf']) < AMBIGUITY_MARGIN
        ok = b['head'] and b['away'] and b['conf'] >= CONFIRM_CONF and not amb
        out[oi] = {
            'status': 'CONFIRMED' if ok else ('AMBIGUOUS' if amb else 'NOT_CONFIRMED'),
            'confidence': round(b['conf'], 2), 'arrowhead_detected': bool(b['head']),
            'head_score': b['head_ratio'], 'head_area_px': b['head_area'],
            'tip_px': [int(round(b['tip'][0])), int(round(b['tip'][1]))],
            'tip_norm': [int(round(b['tip'][0] / W * 1000)), int(round(b['tip'][1] / H * 1000))],
            'tail_px': [int(round(b['tail'][0])), int(round(b['tail'][1]))],
            'stroke_bbox': b['bbox'], 'stroke_length_px': round(b['length'], 1),
            'orange_to_stroke_gap_px': round(b['gap'], 1), 'origin': 'orange_markup',
            'candidates': len(cands),
            'reason': ('Arrowhead found; destination is the head apex.' if ok else
                       'Two similarly good arrows start at this marker.' if amb else
                       'A stroke was found but it has no clear arrowhead pointing away from the marker.'),
        }
        # legacy key used by older UI code
        out[oi]['head_spread'] = b['head_ratio']
    return out

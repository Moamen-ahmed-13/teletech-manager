"""
TELETECH As-Built: automatic registration of a scanned/photographed As-Built
page to the original DXF floor plan.

Pipeline
--------
1. The DXF is rendered from its *actual geometry* (every LINE/ARC/CIRCLE/
   LWPOLYLINE/SPLINE/... reachable from model space, recursing through nested
   INSERTs) - never estimated from marker bounding boxes.
2. The page image is reduced to clean linework (coloured highlighter / pen ink
   removed, paper shading flattened).
3. SIFT features of both linework images are matched (ratio test) and a
   homography is estimated with MAGSAC/RANSAC.  Rotation, translation, scale
   and the perspective of a phone photo are all absorbed by the homography.
4. The homography is refined with ECC directly on the linework (page frame).
5. The result is VALIDATED - an algorithm returning a matrix is not proof:
     * inlier count / ratio              * reprojection error (px and mm)
     * Jacobian sanity (no flips, bounded anisotropy / scale change)
     * spatial coverage of the inliers   * ink overlap (chamfer precision)
     * alias test (the overlap must be a clear peak, not a repeating pattern)
   Anything that fails is reported as REGISTRATION_FAILED so callers can send
   the item to manual review instead of writing a wrong CAD coordinate.

Coordinates
-----------
``Registration.to_world`` maps page pixels -> DXF world X/Y (drawing units)
and ``Registration.to_photo`` is the inverse.
"""
import math
import os
import threading
import time

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - reported as a registration failure
    cv2 = None

try:
    import ezdxf
    from ezdxf import disassemble as _ezdisassemble
except ImportError:  # pragma: no cover
    ezdxf = None
    _ezdisassemble = None


# ----------------------------------------------------------------------------
# tunables (module constants so tests / deployments can override them)
# ----------------------------------------------------------------------------
MIN_INLIERS = int(os.getenv('ASBUILT_REG_MIN_INLIERS', '40'))
MIN_INLIER_RATIO = float(os.getenv('ASBUILT_REG_MIN_INLIER_RATIO', '0.15'))
MAX_MEDIAN_ERR_PX = 2.5          # median reprojection error of inliers (working px)
MAX_P90_ERR_PX = 6.0
MAX_ANISOTROPY = 1.35            # singular value ratio of the local Jacobian
MAX_SCALE_VARIATION = 1.40       # max/min local scale over the page
MIN_COVERAGE = 0.50              # share of inked page cells holding inliers
MIN_OVERLAP_PRECISION = 0.40     # photo ink within OVERLAP_TOL_PX of CAD ink
MIN_ALIAS_RATIO = 1.12           # overlap(aligned) / best overlap(shifted)
OVERLAP_TOL_PX = 3.0
MAX_WORLD_RESIDUAL_MM = float(os.getenv('ASBUILT_REG_MAX_RESIDUAL_MM', '250'))

_GEOM_CACHE = {}
_GEOM_LOCK = threading.Lock()


class RegistrationError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# CAD geometry
# ----------------------------------------------------------------------------
def load_cad_geometry(dxf_path, use_cache=True):
    """Return the drawing's linework as one flat vertex array.

    {'V': (N,2) float64, 'starts': (M+1,) int offsets, 'bbox': robust plan box,
     'count': M, 'load_seconds': float}
    """
    if ezdxf is None:
        raise RegistrationError('ezdxf is not installed (pip install ezdxf); cannot render the DXF.')
    path = os.path.abspath(str(dxf_path))
    st = os.stat(path)
    key = (path, int(st.st_mtime_ns), int(st.st_size))
    with _GEOM_LOCK:
        if use_cache and key in _GEOM_CACHE:
            return _GEOM_CACHE[key]
    t0 = time.time()
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    chunks = []
    starts = [0]
    total = 0
    for prim in _ezdisassemble.to_primitives(_ezdisassemble.recursive_decompose(msp)):
        try:
            if prim.is_empty:
                continue
            v = np.array([(p.x, p.y) for p in prim.vertices()], dtype=np.float64)
        except Exception:
            continue
        if len(v) < 2 or not np.isfinite(v).all():
            continue
        chunks.append(v)
        total += len(v)
        starts.append(total)
    if not chunks:
        raise RegistrationError('The DXF contains no drawable geometry.')
    V = np.vstack(chunks)
    lo = np.percentile(V, 0.2, axis=0)
    hi = np.percentile(V, 99.8, axis=0)
    span = hi - lo
    pad = 0.06 * span
    bbox = (float(lo[0] - pad[0]), float(hi[0] + pad[0]), float(lo[1] - pad[1]), float(hi[1] + pad[1]))
    geom = {'V': V, 'starts': np.asarray(starts, dtype=np.int64), 'bbox': bbox,
            'count': len(chunks), 'load_seconds': round(time.time() - t0, 1), 'path': path}
    with _GEOM_LOCK:
        if use_cache:
            _GEOM_CACHE[key] = geom
    return geom


def extend_bbox(bbox, points, margin=2000.0):
    """Grow a plan bbox so it contains all given world points (+margin)."""
    x0, x1, y0, y1 = bbox
    for x, y in points or []:
        x0 = min(x0, x - margin)
        x1 = max(x1, x + margin)
        y0 = min(y0, y - margin)
        y1 = max(y1, y + margin)
    return (x0, x1, y0, y1)


def _draw_polylines(img, pts_px, starts, thickness=1):
    """Draw every polyline of a flat vertex array onto ``img`` (255=bg, 0=ink)."""
    h, w = img.shape[:2]
    P = np.rint(pts_px).astype(np.int64)
    # skip polylines that are entirely out of frame (cheap vectorised cull)
    seg_ids = np.repeat(np.arange(len(starts) - 1), np.diff(starts))
    inside = (P[:, 0] >= -2) & (P[:, 0] < w + 2) & (P[:, 1] >= -2) & (P[:, 1] < h + 2)
    keep = np.zeros(len(starts) - 1, dtype=bool)
    keep[np.unique(seg_ids[inside])] = True
    P = np.clip(P, -30000, 30000).astype(np.int32)
    lines = []
    for i in np.nonzero(keep)[0]:
        lines.append(P[starts[i]:starts[i + 1]])
    if lines:
        cv2.polylines(img, lines, False, 0, thickness)
    return img


def render_cad_raster(geom, bbox, mm_per_px, thickness=1):
    """Render the DXF to a raster. Returns (image uint8 255/0, info)."""
    x0, x1, y0, y1 = bbox
    W = int(math.ceil((x1 - x0) / mm_per_px)) + 1
    H = int(math.ceil((y1 - y0) / mm_per_px)) + 1
    img = np.full((H, W), 255, np.uint8)
    V = geom['V']
    pts = np.empty_like(V)
    pts[:, 0] = (V[:, 0] - x0) / mm_per_px
    pts[:, 1] = (y1 - V[:, 1]) / mm_per_px
    _draw_polylines(img, pts, geom['starts'], thickness)
    # raster px (X,Y) -> world: wx = x0 + X*mm ; wy = y1 - Y*mm
    R2W = np.array([[mm_per_px, 0, x0], [0, -mm_per_px, y1], [0, 0, 1.0]])
    return img, {'mm_per_px': mm_per_px, 'bbox': bbox, 'raster_to_world': R2W, 'size': (W, H)}


def render_cad_in_photo_frame(geom, H_world_to_photo, size_wh, thickness=1):
    """Draw the DXF directly in the page's pixel frame (no resampling blur)."""
    w, h = size_wh
    img = np.full((h, w), 255, np.uint8)
    V = geom['V']
    ph = np.empty((len(V), 3))
    ph[:, :2] = V
    ph[:, 2] = 1.0
    q = ph @ np.asarray(H_world_to_photo, float).T
    z = q[:, 2:3]
    z[np.abs(z) < 1e-12] = 1e-12
    pts = q[:, :2] / z
    _draw_polylines(img, pts, geom['starts'], thickness)
    return img


# ----------------------------------------------------------------------------
# page image -> linework
# ----------------------------------------------------------------------------
def photo_linework(bgr):
    """Return (ink uint8 0/255 with 255=ink, coloured_mask uint8 0/1).

    Highlighter / ballpoint ink is removed (treated as paper) so that only the
    printed drawing linework remains; shading from a phone photo is flattened.
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    colored = ((hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 80)).astype(np.uint8)
    k = max(3, int(round(min(gray.shape) * 0.002)) | 1)
    colored = cv2.dilate(colored, np.ones((k, k), np.uint8))
    g = gray.copy()
    g[colored > 0] = 255
    ker = max(9, int(round(min(gray.shape) * 0.009)) | 1)
    bg = cv2.morphologyEx(g, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ker, ker)))
    bg = cv2.GaussianBlur(bg, (0, 0), max(6, min(gray.shape) * 0.006))
    norm = np.clip(g.astype(np.float32) / np.maximum(bg, 1).astype(np.float32) * 255, 0, 255).astype(np.uint8)
    block = max(15, int(round(min(gray.shape) * 0.016)) | 1)
    th = cv2.adaptiveThreshold(norm, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 14)
    return th, colored


# ----------------------------------------------------------------------------
# homography helpers
# ----------------------------------------------------------------------------
def _apply_h(H, pts):
    pts = np.asarray(pts, float).reshape(-1, 2)
    q = np.c_[pts, np.ones(len(pts))] @ np.asarray(H, float).T
    z = q[:, 2:3]
    z = np.where(np.abs(z) < 1e-12, 1e-12, z)
    return q[:, :2] / z


def _jacobian_stats(H, box, n=5):
    """Local scale/anisotropy of H over a grid inside ``box=(x0,y0,x1,y1)``."""
    x0, y0, x1, y1 = box
    xs = np.linspace(x0, x1, n)
    ys = np.linspace(y0, y1, n)
    scales, aniso, dets = [], [], []
    e = 1.0
    for y in ys:
        for x in xs:
            p = _apply_h(H, [[x, y], [x + e, y], [x, y + e]])
            J = np.array([[p[1, 0] - p[0, 0], p[2, 0] - p[0, 0]],
                          [p[1, 1] - p[0, 1], p[2, 1] - p[0, 1]]]) / e
            dets.append(np.linalg.det(J))
            s = np.linalg.svd(J, compute_uv=False)
            scales.append(math.sqrt(max(s[0] * s[1], 1e-18)))
            aniso.append(s[0] / max(s[1], 1e-12))
    return {'min_det': float(min(dets)), 'max_det': float(max(dets)), 'max_aniso': float(max(aniso)),
            'scale_var': float(max(scales) / max(min(scales), 1e-12)),
            'mean_scale': float(np.mean(scales))}


def _ink_box(ink, margin_frac=0.02):
    ys, xs = np.nonzero(ink)
    if len(xs) < 50:
        h, w = ink.shape
        return (0, 0, w - 1, h - 1)
    h, w = ink.shape
    mx, my = int(w * margin_frac), int(h * margin_frac)
    sel = (xs > mx) & (xs < w - mx) & (ys > my) & (ys < h - my)
    if sel.sum() < 50:
        return (0, 0, w - 1, h - 1)
    xs, ys = xs[sel], ys[sel]
    return (float(np.percentile(xs, 1)), float(np.percentile(ys, 1)),
            float(np.percentile(xs, 99)), float(np.percentile(ys, 99)))


def _coverage(ink, pts_photo, grid=4):
    """Share of inked page cells that contain >=2 homography inliers."""
    h, w = ink.shape
    cw, ch = w / grid, h / grid
    inked, hit = 0, 0
    pts_photo = np.asarray(pts_photo).reshape(-1, 2)
    for gy in range(grid):
        for gx in range(grid):
            cell = ink[int(gy * ch):int((gy + 1) * ch), int(gx * cw):int((gx + 1) * cw)]
            if cell.size == 0 or (cell > 0).mean() < 0.004:
                continue
            inked += 1
            m = ((pts_photo[:, 0] >= gx * cw) & (pts_photo[:, 0] < (gx + 1) * cw) &
                 (pts_photo[:, 1] >= gy * ch) & (pts_photo[:, 1] < (gy + 1) * ch))
            if m.sum() >= 2:
                hit += 1
    return (hit / inked) if inked else 0.0, inked, hit


def _blur(img01, sigma):
    return cv2.GaussianBlur(img01.astype(np.float32), (0, 0), sigma)


def _overlap_metrics(photo_ink, cad_img, mask=None):
    """Chamfer 'precision': share of photo ink lying within tolerance of CAD ink."""
    cad_ink = (cad_img < 128).astype(np.uint8)
    dist = cv2.distanceTransform(1 - cad_ink, cv2.DIST_L2, 3)
    pin = photo_ink > 0
    if mask is not None:
        pin = pin & (mask > 0)
    n = int(pin.sum())
    if n < 200:
        return 0.0, dist
    return float((dist[pin] <= OVERLAP_TOL_PX).mean()), dist


def _shifted_precision(photo_ink, cad_img, dx, dy):
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    h, w = cad_img.shape
    sh = cv2.warpAffine(cad_img, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=255)
    p, _ = _overlap_metrics(photo_ink, sh)
    return p


# ----------------------------------------------------------------------------
# SIFT + MAGSAC
# ----------------------------------------------------------------------------
def _sift_homography(photo_ink, cad_img, photo_long, nfeat=9000, ratio=0.80, thr=4.0):
    """Match ``photo_ink`` (255=ink) against ``cad_img`` (0=ink).

    Returns (H_photo_native -> cad_raster, stats dict) or (None, stats).
    """
    h, w = photo_ink.shape
    ps = photo_long / float(max(h, w))
    ps = min(ps, 3.0)
    ph = cv2.resize(255 - photo_ink, None, fx=ps, fy=ps,
                    interpolation=cv2.INTER_AREA if ps < 1 else cv2.INTER_CUBIC)
    a = cv2.GaussianBlur(ph, (0, 0), 1.2)
    b = cv2.GaussianBlur(cad_img, (0, 0), 1.2)
    sift = cv2.SIFT_create(nfeatures=nfeat, contrastThreshold=0.02, edgeThreshold=15)
    ka, da = sift.detectAndCompute(a, None)
    kb, db = sift.detectAndCompute(b, None)
    stats = {'photo_kp': len(ka or []), 'cad_kp': len(kb or []), 'photo_scale': ps}
    if da is None or db is None or len(ka) < 12 or len(kb) < 12:
        return None, stats
    bf = cv2.BFMatcher(cv2.NORM_L2)
    knn = bf.knnMatch(da, db, k=2)
    good = [m[0] for m in knn if len(m) == 2 and m[0].distance < ratio * m[1].distance]
    stats['good_matches'] = len(good)
    if len(good) < 12:
        return None, stats
    src = np.float32([ka[g.queryIdx].pt for g in good])
    dst = np.float32([kb[g.trainIdx].pt for g in good])
    method = getattr(cv2, 'USAC_MAGSAC', cv2.RANSAC)
    Hm, mask = cv2.findHomography(src, dst, method, thr, maxIters=10000, confidence=0.999)
    if Hm is None or mask is None:
        return None, stats
    mask = mask.ravel().astype(bool)
    proj = _apply_h(Hm, src)
    err = np.linalg.norm(proj - dst, axis=1)
    stats.update({'inliers': int(mask.sum()), 'inlier_ratio': float(mask.mean()),
                  'err_median_px': float(np.median(err[mask])) if mask.any() else 1e9,
                  'err_p90_px': float(np.percentile(err[mask], 90)) if mask.any() else 1e9,
                  'inlier_pts_photo': (src[mask] / ps).tolist()})
    S = np.diag([ps, ps, 1.0])
    return Hm @ S, stats


# ----------------------------------------------------------------------------
# ECC refinement in the page frame
# ----------------------------------------------------------------------------
def _ecc_refine(photo_ink, geom, Hw2p, max_corner_shift=40.0):
    """Refine world->photo homography with ECC on blurred linework.

    Returns (Hw2p_refined, info).  Falls back to the input on any failure.
    """
    h, w = photo_ink.shape
    info = {'ecc_used': False}
    try:
        cad = render_cad_in_photo_frame(geom, Hw2p, (w, h))
        W = np.eye(3, dtype=np.float32)
        for scale, sigma in ((0.25, 1.6), (0.5, 1.8)):
            s = np.diag([scale, scale, 1.0]).astype(np.float32)
            pi = cv2.resize(photo_ink, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            ci = cv2.resize(255 - cad, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            ta = _blur(pi / 255.0, sigma)
            ia = _blur(ci / 255.0, sigma)
            Wl = (s @ W @ np.linalg.inv(s)).astype(np.float32)
            crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 150, 1e-6)
            _, Wl = cv2.findTransformECC(ta, ia, Wl, cv2.MOTION_HOMOGRAPHY, crit, None, 5)
            W = (np.linalg.inv(s) @ Wl @ s).astype(np.float32)
        corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], float)
        shift = np.linalg.norm(_apply_h(W, corners) - corners, axis=1).max()
        info.update({'ecc_used': True, 'ecc_corner_shift_px': float(shift)})
        if not np.isfinite(shift) or shift > max_corner_shift:
            info['ecc_rejected'] = 'residual too large'
            return Hw2p, info
        # cad_in_photo(W x) ~ photo(x): a page px x sits at world  Hw2p^-1 (W x)
        Hp2w = np.linalg.inv(Hw2p) @ W.astype(float)
        Hw2p_new = np.linalg.inv(Hp2w)
        return Hw2p_new / Hw2p_new[2, 2], info
    except Exception as e:  # ECC non-convergence is normal on poor input
        info['ecc_error'] = str(e)[:120]
        return Hw2p, info


# ----------------------------------------------------------------------------
# result object
# ----------------------------------------------------------------------------
class Registration:
    """Page <-> DXF world transform plus the evidence it was validated with."""

    def __init__(self, ok, status, confidence, reasons, metrics,
                 Hp2w=None, size=None, plan_mask=None, local_quality_img=None, warnings=None):
        self.ok = bool(ok)
        self.status = status
        self.confidence = float(confidence)
        self.reasons = list(reasons or [])
        self.warnings = list(warnings or [])
        self.metrics = metrics or {}
        self.Hp2w = None if Hp2w is None else np.asarray(Hp2w, float)
        self.Hw2p = None if Hp2w is None else np.linalg.inv(self.Hp2w)
        self.size = size
        self._plan_mask = plan_mask
        self._lq = local_quality_img

    # -- transforms ---------------------------------------------------------
    def to_world(self, px):
        if self.Hp2w is None:
            raise RegistrationError('No valid registration.')
        return _apply_h(self.Hp2w, px)

    def to_photo(self, xy):
        if self.Hw2p is None:
            raise RegistrationError('No valid registration.')
        return _apply_h(self.Hw2p, xy)

    def world_mm_per_px(self, px):
        """Local world units per page pixel at ``px`` (isotropic estimate)."""
        px = np.asarray(px, float).reshape(2)
        p = self.to_world([px, px + [1, 0], px + [0, 1]])
        return float(math.sqrt(abs(np.linalg.det(np.array([p[1] - p[0], p[2] - p[0]]).T))))

    # -- plan / local quality ----------------------------------------------
    def on_printed_plan(self, px):
        """True when ``px`` lies on printed CAD linework of the sheet (not on a
        blank margin, handwriting-only area or legend)."""
        if self._plan_mask is None or self.size is None:
            return False
        x, y = int(round(px[0])), int(round(px[1]))
        h, w = self._plan_mask.shape[:2]
        sx, sy = self._plan_mask.shape[1] / float(self.size[0]), self._plan_mask.shape[0] / float(self.size[1])
        x, y = int(round(px[0] * sx)), int(round(px[1] * sy))
        if x < 0 or y < 0 or x >= w or y >= h:
            return False
        return bool(self._plan_mask[y, x] > 0)

    def local_precision(self, px, radius=120):
        """Overlap precision (0..1) of the registration around ``px``."""
        if self._lq is None or self.size is None:
            return 0.0
        photo_in, dist = self._lq
        sx = photo_in.shape[1] / float(self.size[0])
        sy = photo_in.shape[0] / float(self.size[1])
        cx, cy = int(round(px[0] * sx)), int(round(px[1] * sy))
        r = int(radius * sx)
        y0, y1 = max(0, cy - r), min(photo_in.shape[0], cy + r)
        x0, x1 = max(0, cx - r), min(photo_in.shape[1], cx + r)
        if y1 <= y0 or x1 <= x0:
            return 0.0
        win = photo_in[y0:y1, x0:x1] > 0
        if win.sum() < 80:
            return 0.0
        return float((dist[y0:y1, x0:x1][win] <= OVERLAP_TOL_PX * sx + 0.5).mean())

    def summary(self):
        """JSON-safe description for API responses / diagnostics."""
        m = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.metrics.items()
             if not k.startswith('_') and k != 'inlier_pts_photo'}
        return {'ok': self.ok, 'status': self.status, 'confidence': round(self.confidence, 3),
                'reasons': list(self.reasons), 'warnings': list(self.warnings), 'metrics': m,
                'H_photo_to_world': None if self.Hp2w is None else [[float(v) for v in r] for r in self.Hp2w]}


def _failed(reasons, metrics=None):
    return Registration(False, 'REGISTRATION_FAILED', 0.0, reasons, metrics or {})


# ----------------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------------
def register_page_to_dxf(page_bgr, geom, extra_world_points=None, attempts=None, log=None):
    """Register one page image to the DXF described by ``geom``.

    Never raises for ordinary failures; returns a Registration whose ``ok`` is
    False (status REGISTRATION_FAILED) with human-readable ``reasons``.
    """
    log = log or (lambda *_: None)
    if cv2 is None:
        return _failed(['OpenCV is not installed.'])
    if page_bgr is None or page_bgr.size == 0:
        return _failed(['Page image is empty.'])
    try:
        t_all = time.time()
        h, w = page_bgr.shape[:2]
        ink, colored = photo_linework(page_bgr)
        ink_frac = float((ink > 0).mean())
        if ink_frac < 0.002:
            return _failed(['The page has almost no printed linework to register against.'],
                           {'ink_fraction': ink_frac})
        bbox = extend_bbox(geom['bbox'], extra_world_points)
        longest = max(bbox[1] - bbox[0], bbox[3] - bbox[2])
        if attempts is None:
            attempts = [(1600, 1700), (2200, 2400), (1100, 1200), (2600, 3000)]
        best = None
        tried = []
        for photo_long, cad_long in attempts:
            mm = longest / float(cad_long)
            cad_img, cinfo = render_cad_raster(geom, bbox, mm, 1)
            H_p2r, st = _sift_homography(ink, cad_img, photo_long)
            rec = {'photo_long': photo_long, 'cad_long': cad_long, 'mm_per_px': round(mm, 2),
                   **{k: v for k, v in st.items() if k != 'inlier_pts_photo'}}
            tried.append(rec)
            if H_p2r is None:
                log('attempt', rec, 'no homography')
                continue
            # photo native px -> world
            Hp2w = cinfo['raster_to_world'] @ H_p2r
            cand = _validate(ink, colored, geom, Hp2w, st, mm, h, w)
            cand['attempt'] = rec
            log('attempt', rec, 'score', cand['score'], 'ok', cand['ok'])
            if best is None or cand['score'] > best['score']:
                best = cand
            if cand['ok'] and cand['score'] >= 0.75:
                break
        if best is None:
            return _failed(['Not enough matching architectural features between the page and the DXF '
                            '(SIFT found no consistent homography).'], {'attempts': tried})
        best['metrics']['attempts'] = tried
        best['metrics']['seconds'] = round(time.time() - t_all, 1)
        return Registration(best['ok'], 'CONFIRMED' if best['ok'] else 'REGISTRATION_FAILED',
                            best['confidence'], best['reasons'], best['metrics'],
                            Hp2w=best['Hp2w'], size=(w, h), plan_mask=best.get('plan_mask'),
                            local_quality_img=best.get('lq'), warnings=best.get('warnings'))
    except Exception as e:  # pragma: no cover - defensive
        return _failed([f'Registration crashed: {type(e).__name__}: {e}'])


def _validate(ink, colored, geom, Hp2w, st, mm, h, w):
    """Refine (ECC) and validate a candidate page->world homography."""
    reasons, warnings = [], []
    metrics = {k: v for k, v in st.items() if k not in ('photo_kp', 'cad_kp')}
    Hw2p = np.linalg.inv(Hp2w)
    Hw2p = Hw2p / Hw2p[2, 2]

    # --- geometry sanity of the coarse model --------------------------------
    box = _ink_box(ink)
    js = _jacobian_stats(Hp2w, box)
    metrics.update({'jac_min_det': js['min_det'], 'jac_max_aniso': js['max_aniso'],
                    'jac_scale_var': js['scale_var'], 'world_per_px': js['mean_scale']})
    # World Y runs up while image Y runs down, so a proper (non-mirrored) page maps
    # to the DXF with det(J) < 0 everywhere; a mirrored page gives det > 0.
    if js['max_det'] >= 0:
        reasons.append('The page maps to the DXF as a mirror image (not a valid sheet photo).')
    if js['max_aniso'] > MAX_ANISOTROPY:
        reasons.append(f"Registration is too anisotropic ({js['max_aniso']:.2f} > {MAX_ANISOTROPY}).")
    if js['scale_var'] > MAX_SCALE_VARIATION:
        reasons.append(f"Scale varies too much across the page ({js['scale_var']:.2f} > {MAX_SCALE_VARIATION}).")

    # --- statistical evidence ------------------------------------------------
    inl = int(st.get('inliers', 0))
    ratio = float(st.get('inlier_ratio', 0))
    if inl < MIN_INLIERS:
        reasons.append(f'Only {inl} inlier feature matches (need >= {MIN_INLIERS}).')
    if ratio < MIN_INLIER_RATIO:
        reasons.append(f'Inlier ratio {ratio:.2f} is below {MIN_INLIER_RATIO}.')
    if st.get('err_median_px', 1e9) > MAX_MEDIAN_ERR_PX:
        reasons.append(f"Median reprojection error {st.get('err_median_px', 0):.1f}px is too high.")
    if st.get('err_p90_px', 1e9) > MAX_P90_ERR_PX:
        reasons.append(f"90th-percentile reprojection error {st.get('err_p90_px', 0):.1f}px is too high.")
    metrics['err_median_mm'] = float(st.get('err_median_px', 0)) * mm
    metrics['err_p90_mm'] = float(st.get('err_p90_px', 0)) * mm
    cov, inked_cells, hit_cells = _coverage(ink, np.array(st.get('inlier_pts_photo') or [[0, 0]]))
    metrics.update({'coverage': cov, 'inked_cells': inked_cells, 'cells_with_inliers': hit_cells})
    if cov < MIN_COVERAGE:
        reasons.append(f'Feature inliers cover only {cov:.0%} of the inked page (need {MIN_COVERAGE:.0%}).')

    # --- ECC refinement + ink overlap ---------------------------------------
    cad0 = render_cad_in_photo_frame(geom, Hw2p, (w, h))
    prec0, _ = _overlap_metrics(ink, cad0, mask=(1 - colored))
    Hw2p_ref, einfo = _ecc_refine(ink, geom, Hw2p)
    cad1 = render_cad_in_photo_frame(geom, Hw2p_ref, (w, h))
    prec1, dist1 = _overlap_metrics(ink, cad1, mask=(1 - colored))
    use_ref = prec1 >= prec0 - 0.002
    if use_ref:
        Hw2p_fin, cad_fin, prec, dist = Hw2p_ref, cad1, prec1, dist1
    else:
        Hw2p_fin, cad_fin, prec = Hw2p, cad0, prec0
        _, dist = _overlap_metrics(ink, cad0, mask=(1 - colored))
    metrics.update({'overlap_precision_coarse': prec0, 'overlap_precision': prec,
                    'ecc_used': bool(einfo.get('ecc_used') and use_ref)})
    metrics.update({k: v for k, v in einfo.items() if k.startswith('ecc_') and k != 'ecc_used'})
    if prec < MIN_OVERLAP_PRECISION:
        reasons.append(f'Only {prec:.0%} of the page linework coincides with the DXF after registration '
                       f'(need {MIN_OVERLAP_PRECISION:.0%}).')

    # --- alias test: aligned overlap must be a clear peak -------------------
    shifts = [(14, 0), (-14, 0), (0, 14), (0, -14), (28, 0), (-28, 0), (0, 28), (0, -28)]
    scale_px = max(1.0, 14.0 * (w / 2354.0))
    alt = max(_shifted_precision(ink, cad_fin, dx * scale_px / 14.0, dy * scale_px / 14.0) for dx, dy in shifts)
    alias = prec / max(alt, 1e-6)
    metrics.update({'alias_best_shifted_precision': alt, 'alias_ratio': alias})
    if alias < MIN_ALIAS_RATIO:
        reasons.append(f'Alignment is not a distinct optimum (alias ratio {alias:.2f}); the plan may repeat '
                       'and the match could be shifted.')

    # --- final world-space residual check ------------------------------------
    Hp2w_fin = np.linalg.inv(Hw2p_fin)
    Hp2w_fin = Hp2w_fin / Hp2w_fin[2, 2]
    pts = np.array(st.get('inlier_pts_photo') or [])
    if len(pts):
        # compare final mapping with the SIFT model on the inlier keypoints (px -> world)
        d = np.linalg.norm(_apply_h(Hp2w_fin, pts) - _apply_h(Hp2w, pts), axis=1)
        metrics['refine_shift_median_mm'] = float(np.median(d))
        if np.median(d) > 4 * max(mm, 1.0) * 3:
            warnings.append('ECC refinement moved the SIFT model by a large amount; verify the overlay.')

    # --- plan mask (where the printed drawing actually is) -------------------
    near = (dist <= OVERLAP_TOL_PX).astype(np.uint8)
    explained = ((ink > 0) & (near > 0) & (colored == 0)).astype(np.uint8)
    small = cv2.resize(explained, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    small = (small > 0).astype(np.uint8)
    kk = max(7, int(round(min(small.shape) * 0.06)) | 1)
    plan_mask = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
    plan_mask = cv2.morphologyEx(plan_mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
    # sparse speckle is not "plan": require a minimum local density of explained ink
    dens = cv2.boxFilter(small.astype(np.float32), -1, (kk, kk))
    plan_mask = ((plan_mask > 0) & (dens > 0.04)).astype(np.uint8)
    metrics['plan_mask_fraction'] = float(plan_mask.mean())

    # local-quality images (half scale) for per-markup checks
    half = cv2.resize(ink, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    halfdist = cv2.resize(dist, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    lq = ((half > 100).astype(np.uint8), halfdist)

    # --- confidence ----------------------------------------------------------
    c_inl = min(1.0, inl / 150.0)
    c_ov = min(1.0, prec / 0.75)
    c_cov = min(1.0, cov)
    c_alias = min(1.0, (alias - 1.0) / 0.6)
    c_err = max(0.0, 1.0 - float(st.get('err_median_px', 5)) / MAX_MEDIAN_ERR_PX)
    confidence = 0.30 * c_ov + 0.25 * c_inl + 0.15 * c_cov + 0.15 * c_alias + 0.15 * c_err
    ok = not reasons
    return {'ok': ok, 'reasons': reasons, 'warnings': warnings, 'metrics': metrics,
            'Hp2w': Hp2w_fin, 'confidence': confidence if ok else min(confidence, 0.49),
            'score': confidence + (1.0 if ok else 0.0), 'plan_mask': plan_mask, 'lq': lq}

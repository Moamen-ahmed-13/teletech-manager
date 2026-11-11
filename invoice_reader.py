"""Reader for scanned Arabic supply INVOICES (فاتورة) used by the Documents / reconciliation panel.

Why a separate reader
---------------------
recon_vision.py reads *MIR forms*.  An invoice is a different document:

* a 6-column ruled table  (م | اسم الصنف | الوحدة | الكمية | سعر الوحدة | الإجمالي),
* numbers printed in Eastern Arabic-Indic digits, where the digit ZERO (٠) is a small DOT.
  OCR engines treat that dot as noise, so ٦١٠٠٠ becomes "٦١" or nothing - the cause of the
  empty cards in the Documents screen,
* every second page of a typical invoice pack is a signed certificate (شهادة) without a table.

What this module does
---------------------
1. finds the table grid (vertical rules -> columns, horizontal rules that cross the numeric
   columns -> rows);
2. reads each numeric cell glyph by glyph: dots at mid digit-height are zeros, every other
   glyph is matched against shipped digit templates (invoice_digit_templates.npz);
3. decodes quantity, unit price and amount JOINTLY using  qty x price = amount  as a
   consistency constraint, so a doubtful glyph is resolved by arithmetic instead of guessed;
4. reports every doubt as a flag; a line whose arithmetic does not close is never presented
   as verified.

Public API
----------
page_kind(gray)                      -> 'invoice' | 'other'
read_invoice_page(gray, tess_cmd, filename_hint=None) -> parsed dict (same shape recon expects)
"""
from __future__ import annotations

import itertools
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
DIGITS = '٠١٢٣٤٥٦٧٨٩'
TEMPLATE_FILE = HERE / 'invoice_digit_templates.npz'
ARA_DIR = HERE / 'tessdata_ara'
GLYPH_SIZE = (24, 36)

_TEMPLATES = None


# --------------------------------------------------------------------------
# glyph templates
# --------------------------------------------------------------------------
def _norm_glyph(binary):
    ys, xs = np.where(binary > 0)
    if len(xs) == 0:
        return None
    c = binary[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    h, w = c.shape
    s = min((GLYPH_SIZE[1] - 4) / h, (GLYPH_SIZE[0] - 4) / w)
    c = cv2.resize(c, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    out = np.zeros((GLYPH_SIZE[1], GLYPH_SIZE[0]), np.float32)
    y0, x0 = (GLYPH_SIZE[1] - c.shape[0]) // 2, (GLYPH_SIZE[0] - c.shape[1]) // 2
    out[y0:y0 + c.shape[0], x0:x0 + c.shape[1]] = c
    out = cv2.GaussianBlur(out, (0, 0), 1.0)
    return out / (np.linalg.norm(out) + 1e-6)


def build_digit_templates(out_path=TEMPLATE_FILE, font_paths=None):
    """(Re)generate the template file from fonts installed on this machine."""
    from PIL import Image, ImageDraw, ImageFont
    import glob
    if font_paths is None:
        pats = ['/usr/share/fonts/**/*.ttf', 'C:/Windows/Fonts/*.ttf', '/Library/Fonts/*.ttf',
                '/System/Library/Fonts/**/*.ttf']
        keys = ('DejaVuSans', 'DejaVuSerif', 'FreeSerif', 'arial', 'tahoma', 'times', 'segoeui', 'NotoSansArabic',
                'NotoNaskh', 'Amiri', 'simpo', 'trado')
        font_paths = [f for p in pats for f in glob.glob(p, recursive=True)
                      if any(k.lower() in os.path.basename(f).lower() for k in keys)]
    store = {i: [] for i in range(1, 10)}
    for fp in font_paths:
        try:
            ft = ImageFont.truetype(fp, 60)
        except Exception:
            continue
        for i in range(1, 10):
            im = Image.new('L', (140, 140), 0)
            ImageDraw.Draw(im).text((20, 10), DIGITS[i], fill=255, font=ft)
            g = _norm_glyph((np.array(im) > 128).astype(np.uint8))
            if g is not None:
                store[i].append(g)
    np.savez_compressed(out_path, **{f'd{i}': np.array(v, np.float32) for i, v in store.items() if v})
    return {i: len(v) for i, v in store.items()}


def _templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        z = np.load(TEMPLATE_FILE)
        _TEMPLATES = {int(k[1:]): z[k] for k in z.files}
    return _TEMPLATES


def classify_glyph(binary):
    """{digit: similarity 0..1} for a non-zero glyph mask."""
    g = _norm_glyph(binary)
    if g is None:
        return {}
    out = {}
    for d, arr in _templates().items():
        out[d] = float(max(0.0, np.max(arr.reshape(len(arr), -1) @ g.ravel())))
    return out


# --------------------------------------------------------------------------
# table grid
# --------------------------------------------------------------------------
def _peaks(profile, minsep, thr):
    idx = np.where(profile > thr)[0]
    groups = []
    for i in idx:
        if not groups or i - groups[-1][-1] > minsep:
            groups.append([i])
        else:
            groups[-1].append(i)
    return [int(np.mean(g)) for g in groups]


def deskew(gray):
    """Straighten a slightly rotated scan using its long ruled lines (|angle| <= 3 degrees)."""
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 15)
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 14), 1)))
    lines = cv2.HoughLinesP(hl, 1, np.pi / 1800, 200, minLineLength=w // 5, maxLineGap=25)
    if lines is None:
        return gray
    angs = [math.degrees(math.atan2(l[0][3] - l[0][1], l[0][2] - l[0][0])) for l in lines]
    angs = [a for a in angs if abs(a) <= 3.0]
    if not angs:
        return gray
    ang = float(np.median(angs))
    if abs(ang) < 0.2:
        return gray
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), ang, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def find_grid(gray):
    """Return dict(xs=[7 vertical rules], rows=[(y0,y1)...], header=(y0,y1)) or None."""
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 15)
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 12), 1)))
    vl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 28))))
    xs = _peaks(vl.sum(0) / 255.0, 8, h * 0.06)
    # a dark scanner edge / page border is not a table rule
    xs = [x for x in xs if 0.015 * w <= x <= 0.975 * w or len(xs) <= 7]
    if len(xs) > 7:
        xs = [x for x in xs if 0.015 * w <= x <= 0.975 * w]
    if len(xs) != 7:
        return None
    ys = _peaks(hl.sum(1) / 255.0, 8, w * 0.14)
    x_num0, x_num1 = xs[0], xs[4]
    bounds = []
    for y in ys:
        cov = float((hl[max(0, y - 4):y + 5, x_num0:x_num1].max(0) > 0).mean())
        if cov >= 0.3:
            bounds.append(y)
    if len(bounds) < 3:
        return None
    intervals = [(a, b) for a, b in zip(bounds[:-1], bounds[1:]) if b - a >= 0.012 * h]
    if len(intervals) < 2:
        return None
    return dict(xs=xs, header=intervals[0], rows=intervals[1:])


def page_kind(gray):
    return 'invoice' if find_grid(deskew(gray)) is not None else 'other'


# --------------------------------------------------------------------------
# numeric cells
# --------------------------------------------------------------------------
def _cell_binary(cell):
    b = cv2.adaptiveThreshold(cell, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    hh = cv2.morphologyEx(b, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(5, cell.shape[1] // 2), 1)))
    vv = cv2.morphologyEx(b, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(5, cell.shape[0] // 2))))
    b = cv2.subtract(b, cv2.dilate(cv2.bitwise_or(hh, vv), np.ones((3, 3), np.uint8)))
    return cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


def read_glyphs(cell_gray):
    """Segment a numeric cell into ordered glyphs.

    Returns (glyphs, flags).  glyph = ('0', None) for a dot-zero or ('G', {digit: score}).
    Numbers are written left-to-right.
    """
    b = (_cell_binary(cell_gray) > 0).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(b, 8)
    comps = [(int(st[i, 0]), int(st[i, 1]), int(st[i, 2]), int(st[i, 3]), int(st[i, 4]), i)
             for i in range(1, n) if st[i, 4] >= 8]
    if not comps:
        return [], []
    hmax = max(c[3] for c in comps)
    big = [c for c in comps if c[3] >= 0.55 * hmax]
    href = float(np.median([c[3] for c in big]))
    if href < 8:
        return [], ['cell_too_small']
    top = min(c[1] for c in big)
    bot = max(c[1] + c[3] for c in big)
    span = max(1.0, bot - top)
    glyphs, flags = [], []
    for x, y, w, h, a, i in sorted(comps):
        rel = (y + h / 2.0 - top) / span
        if rel < -0.25 or rel > 1.15:
            continue                                     # rule remnants / specks outside the number
        if h <= 0.5 * href and w <= 0.6 * href and 0.45 <= w / max(h, 1) <= 2.2:
            if 0.40 <= rel <= 0.85:
                glyphs.append(('0', None))               # the Arabic-Indic zero is a dot at mid height
            elif rel > 0.85:
                flags.append('decimal_point_ignored')
            continue
        if h < 0.5 * href:
            continue
        mask = (lab[y:y + h, x:x + w] == i).astype(np.uint8)
        glyphs.append(('G', classify_glyph(mask)))
    return glyphs, flags


# --------------------------------------------------------------------------
# joint arithmetic decoder
# --------------------------------------------------------------------------
def _cands(glyph, k=3):
    kind, sc = glyph
    if kind == '0':
        return [(0, 1.0)]
    top = sorted(sc.items(), key=lambda kv: -kv[1])[:k]
    return [(d, s) for d, s in top if s >= 0.15] or [(0, 0.0)]


def _expand(glyphs, k=3):
    """All candidate integers for a glyph list with their mean similarity."""
    if not glyphs:
        return []
    opts = [_cands(g, k) for g in glyphs]
    out = []
    for combo in itertools.product(*opts):
        val = 0
        for d, _ in combo:
            val = val * 10 + d
        out.append((val, [d for d, _ in combo], float(np.mean([s for _, s in combo]))))
    return out


def _digit_score(glyph, digit):
    kind, sc = glyph
    if kind == '0':
        return 1.0 if digit == 0 else 0.0
    if digit == 0:
        return 0.0
    return float(sc.get(digit, 0.0))


def decode_row(g_qty, g_price, g_total):
    """Jointly decode (qty, price, amount).  Returns dict(qty, price, amount, status, score, flags)."""
    flags = []
    best = None
    if g_qty and g_price and g_total:
        for q, qd, qs in _expand(g_qty):
            for p, pd, ps in _expand(g_price):
                prod = q * p
                s = str(prod)
                if len(s) != len(g_total):
                    continue
                ts = float(np.mean([_digit_score(g, int(ch)) for g, ch in zip(g_total, s)]))
                score = (qs * len(g_qty) + ps * len(g_price) + ts * len(g_total)) / (len(g_qty) + len(g_price) + len(g_total))
                if best is None or score > best[0]:
                    best = (score, q, p, prod, ts)
        if best and best[4] >= 0.45:
            return dict(qty=best[1], price=best[2], amount=best[3], status='verified', score=round(best[0], 2), flags=flags)
    # arithmetic could not close: read each number independently and say so
    def solo(gl):
        e = _expand(gl, 1)
        return (e[0][0], e[0][2]) if e else (None, 0.0)
    q, qs = solo(g_qty)
    p, ps = solo(g_price)
    t, ts = solo(g_total)
    status = 'unverified'
    if q is not None and p is not None and t is not None:
        if q * p == t:
            status = 'verified'
        else:
            flags.append('amount_mismatch')
    elif sum(v is not None for v in (q, p, t)) == 2:
        if t is None and q is not None and p is not None:
            t = q * p
        elif q is None and p and t is not None and t % p == 0:
            q = t // p
        elif p is None and q and t is not None and t % q == 0:
            p = t // q
        flags.append('one_number_derived_by_arithmetic')
        status = 'derived'
    else:
        flags.append('numbers_not_read')
    vals = [s for s in (qs, ps, ts) if s]
    return dict(qty=q, price=p, amount=t, status=status, score=round(float(np.mean(vals)) if vals else 0.0, 2), flags=flags)


# --------------------------------------------------------------------------
# text cells (English part of the description, unit word)
# --------------------------------------------------------------------------
def _tess(cmd, img, lang, psm, extra=None):
    fd, tmp = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    try:
        cv2.imwrite(tmp, img)
        args = [cmd, tmp, 'stdout', '-l', lang, '--psm', str(psm)]
        if lang == 'ara' and ARA_DIR.is_dir():
            args += ['--tessdata-dir', str(ARA_DIR)]
        args += extra or []
        r = subprocess.run(args, capture_output=True, text=True, timeout=90,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return (r.stdout or '').strip()
    except Exception:
        return ''
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


_LOWER_OK = {'box', 'cord', 'port', 'rack', 'data', 'cat', 'cat6', 'cat6a', 'ups', 'pdu', 'lan', 'wan'}


def _read_description(cmd, cell):
    """English / Latin part of the (bilingual) description: model names, P/N codes, port counts.

    The Arabic sentence is not OCR'd (its words would only add noise); confident Latin tokens
    are kept in reading order and the repeated paragraph is collapsed.
    """
    if cmd is None:
        return None, ['description_not_read']
    b = _cell_binary(cell)
    img = 255 - cv2.resize(b, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
    tsv = _tess(cmd, img, 'eng', 6, ['tsv'])
    toks = []
    for ln in tsv.splitlines()[1:]:
        c = ln.split('\t')
        if len(c) < 12:
            continue
        try:
            conf = float(c[10])
        except ValueError:
            continue
        w = c[11].strip()
        letters = len(re.findall(r'[A-Za-z]', w))
        if w.islower() and len(w) <= 4 and w not in _LOWER_OK:
            continue                                   # Arabic words mis-read as short lowercase Latin
        if conf >= 62 and re.fullmatch(r"[A-Za-z0-9/\\.\-()+#]{2,}", w) and (letters >= 3 or (letters >= 1 and re.search(r'\d', w))):
            toks.append(w)
    # collapse an immediately repeated phrase ("Access Switch ... Access Switch ...")
    n = len(toks)
    for k in range(n // 2, 0, -1):
        if n >= 2 * k and toks[:k] == toks[k:2 * k] or (n >= 2 * k and toks[-2 * k:-k] == toks[-k:]):
            toks = toks[:k] if toks[:k] == toks[k:2 * k] else toks[:-k]
            break
    if not toks:
        return None, ['description_english_not_read']
    return ' '.join(toks), ['description_english_part_only']


def _read_unit(cmd, cell):
    if cmd is None or not ARA_DIR.is_dir():
        return None, ['unit_not_read']
    b = _cell_binary(cell)
    ys, xs = np.where(b > 0)
    if len(xs) < 30:
        return None, ['unit_not_read']
    crop = b[max(0, ys.min() - 6):ys.max() + 7, max(0, xs.min() - 6):xs.max() + 7]
    img = 255 - cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    txt = _tess(cmd, img, 'ara', 7)
    if 'عد' in txt:
        return 'PC', []
    return None, ['unit_not_read']


# --------------------------------------------------------------------------
# page reader
# --------------------------------------------------------------------------
def _ref_from_filename(hint):
    m = re.search(r'رقم\s*[(\[]?\s*(\d{1,6})', str(hint or ''))
    return ('INV-' + m.group(1)) if m else None


def read_invoice_page(gray, tess_cmd=None, filename_hint=None):
    """Parse one invoice table page.  `gray` is a grayscale page image (any resolution >= ~150 dpi)."""
    flags = []
    scale = 1.0
    if min(gray.shape) < 1400:                      # keep digit glyphs large enough
        scale = 1400.0 / min(gray.shape)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = deskew(gray)
    grid = find_grid(gray)
    ref = _ref_from_filename(filename_hint)
    parsed = {'ref_no': ref, 'ref_source': 'file name' if ref else None, 'date': None, 'date_alternatives': [],
              'template': 'invoice', 'engine': 'invoice-table', 'supplier': None, 'status': {}, 'rows': [],
              'flags': flags, 'raw_text': ''}
    if ref:
        flags.append('ref_from_filename')
    if grid is None:
        parsed['template'] = 'invoice_attachment'
        flags.append('no_invoice_table_on_page')
        return parsed
    xs = grid['xs']
    cols = {'amount': (xs[0], xs[1]), 'price': (xs[1], xs[2]), 'qty': (xs[2], xs[3]), 'unit': (xs[3], xs[4]),
            'desc': (xs[4], xs[5]), 'item': (xs[5], xs[6])}
    for (y0, y1) in grid['rows']:
        def cell(name, pad=5):
            a, b = cols[name]
            return gray[y0 + pad:y1 - pad, a + pad:b - pad]
        gq, fq = read_glyphs(cell('qty'))
        gp, fp = read_glyphs(cell('price'))
        ga, fa = read_glyphs(cell('amount'))
        if not (gq or gp or ga):
            continue                                  # blank / footer row
        dec = decode_row(gq, gp, ga)
        rf = list(dict.fromkeys(fq + fp + fa + dec['flags']))
        desc, df = _read_description(tess_cmd, cell('desc', 8))
        unit, uf = _read_unit(tess_cmd, cell('unit'))
        rf += df + uf
        if dec['qty'] is None and not desc and not unit:
            flags.append('footer_row_skipped')          # grand-total / signature line, not an item
            continue
        verified = dec['status'] == 'verified'
        if not verified:
            rf.append('arithmetic_not_verified')
        extra = 'unit price %s · amount %s · qty x price %s' % (
            f"{dec['price']:,}" if dec['price'] is not None else '?',
            f"{dec['amount']:,}" if dec['amount'] is not None else '?',
            'checked OK' if verified else 'NOT verified')
        parsed['rows'].append({
            'description': desc, 'unit': unit, 'qty': float(dec['qty']) if dec['qty'] is not None else None,
            'total': None, 'conf': dec['score'] if verified else min(0.5, dec['score']),
            'invoice_no': (ref or '').replace('INV-', '') or None, 'extra': extra,
            'unit_price': dec['price'], 'amount': dec['amount'], 'arithmetic': dec['status'],
            'row_flags': list(dict.fromkeys(rf)), 'alts': None,
        })
    if not parsed['rows']:
        flags.append('invoice_table_has_no_numeric_rows')
    return parsed

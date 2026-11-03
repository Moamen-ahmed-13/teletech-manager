# -*- coding: utf-8 -*-
"""
recon_vision.py - layout-aware reading of scanned / photographed MIR forms.

Everything returned here is a DRAFT for a human reviewer. Nothing is guessed:
when a value cannot be read with reasonable evidence it is returned as None and
a flag explains why.

Pipeline per page
  1. prepare():   grayscale -> background (shadow) normalisation -> deskew
  2. read_page(): Tesseract word boxes -> table columns located from the printed
                  header words (UNIT / TOTAL), header fields (MIR no., date) read
                  from cropped regions, MIR status read from the ink density of
                  the four printed check-boxes.

Requires OpenCV + NumPy (already optional dependencies of server.py) and the
Tesseract executable. If OpenCV is missing, available() is False and
recon_module falls back to its plain line parser.
"""
import math
import os
import re
import subprocess
import tempfile

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - environment dependent
    cv2 = np = None


def available():
    return cv2 is not None


# ---------------------------------------------------------------------------
# image io (np.fromfile/imencode so non-ASCII Windows paths work)
# ---------------------------------------------------------------------------
def imread_gray(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError('Image could not be decoded: %s' % path)
    return img


def imwrite_png(path, img):
    ok, buf = cv2.imencode('.png', img)
    if not ok:
        raise ValueError('Image could not be encoded.')
    buf.tofile(str(path))


# ---------------------------------------------------------------------------
# 1. preprocessing
# ---------------------------------------------------------------------------
def _estimate_skew(gray):
    """Median angle (degrees) of long near-horizontal lines (form borders / table rules)."""
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 15)
    lines = cv2.HoughLinesP(bw, 1, np.pi / 1800, threshold=300, minLineLength=gray.shape[1] // 4, maxLineGap=20)
    if lines is None:
        return 0.0
    angs = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if abs(a) < 8:
            angs.append(a)
    return float(np.median(angs)) if angs else 0.0


def prepare(gray):
    """Return (normalised+deskewed gray image, skew angle in degrees)."""
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    bg = cv2.resize(cv2.medianBlur(small, 31), (gray.shape[1], gray.shape[0]))
    norm = cv2.divide(gray, bg, scale=255)
    ang = _estimate_skew(norm)
    if abs(ang) >= 0.05:
        m = cv2.getRotationMatrix2D((norm.shape[1] / 2, norm.shape[0] / 2), ang, 1.0)
        norm = cv2.warpAffine(norm, m, (norm.shape[1], norm.shape[0]), flags=cv2.INTER_CUBIC, borderValue=255)
    return norm, ang


# ---------------------------------------------------------------------------
# tesseract helpers (images are written to an ASCII temp dir: avoids Windows
# path/encoding problems inside Tesseract)
# ---------------------------------------------------------------------------
def _run_tesseract(cmd, img, psm, lang, tsv=False, whitelist=None, timeout=120):
    with tempfile.TemporaryDirectory(prefix='recon_ocr_') as td:
        p = os.path.join(td, 'in.png')
        imwrite_png(p, img)
        args = [cmd, p, 'stdout', '--psm', str(psm), '-l', lang]
        if whitelist:
            args += ['-c', 'tessedit_char_whitelist=' + whitelist]
        if tsv:
            args.append('tsv')
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')
    if out.returncode != 0:
        raise RuntimeError('Tesseract failed: %s' % out.stderr.strip()[:200])
    return out.stdout


def _words(cmd, img, psm=6, lang='eng'):
    res = []
    for row in _run_tesseract(cmd, img, psm, lang, tsv=True).splitlines()[1:]:
        p = row.split('\t')
        if len(p) < 12 or not p[11].strip():
            continue
        try:
            conf = float(p[10])
        except ValueError:
            conf = -1.0
        res.append({'t': p[11].strip(), 'x': int(p[6]), 'y': int(p[7]), 'w': int(p[8]), 'h': int(p[9]), 'c': conf})
    return res


def _text_lines(words):
    """Group words into reading-order lines (by vertical centre)."""
    if not words:
        return []
    hs = sorted(w['h'] for w in words)
    tol = max(10, 0.6 * hs[len(hs) // 2])
    lines = []
    for w in sorted(words, key=lambda w: w['y'] + w['h'] / 2):
        cy = w['y'] + w['h'] / 2
        if lines and abs(lines[-1]['cy'] - cy) <= tol:
            lines[-1]['w'].append(w)
            lines[-1]['cy'] = (lines[-1]['cy'] * (len(lines[-1]['w']) - 1) + cy) / len(lines[-1]['w'])
        else:
            lines.append({'cy': cy, 'w': [w]})
    for ln in lines:
        ln['w'].sort(key=lambda w: w['x'])
    return lines


# ---------------------------------------------------------------------------
# header fields
# ---------------------------------------------------------------------------
_REF_RE = re.compile(r'M\s*[I1l|]\s*R\s*[_#\-:.\s]*\s*0*(\d{1,4})\b', re.I)
_DATE_RE = re.compile(r'(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(20\d{2})(?!\d)')


def _find_ref(text):
    m = _REF_RE.search(text or '')
    return 'MIR-%03d' % int(m.group(1)) if m else None


def _valid_date(a, b, y):
    a, b = int(a), int(b)
    return 1 <= a <= 31 and 1 <= b <= 12  # forms are d/m/y


def _all_dates(text):
    out = []
    for m in _DATE_RE.finditer(text or ''):
        if _valid_date(*m.groups()):
            out.append('%s/%s/%s' % m.groups())
    return out


def _find_date(text):
    d = _all_dates(text)
    return d[0] if d else None


def _strip_rules(crop):
    """Remove long table/box rules from a crop so Tesseract is not confused by borders."""
    bw = cv2.adaptiveThreshold(crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 15)
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 30)))
    mask = cv2.dilate(cv2.bitwise_or(h, v), np.ones((3, 3), np.uint8))
    out = crop.copy()
    out[mask > 0] = 255
    return out


def _slide(cmd, img, x0f, x1f, y_start, y_end, win, step, psm, whitelist, finder, scales=(2, 3), lang='eng'):
    """Slide a window down a column of the page and return the first value `finder` accepts."""
    H, W = img.shape
    x0, x1 = int(W * x0f), int(W * x1f)
    y = max(0, y_start)
    while y + win <= min(H, y_end + win):
        crop = img[y:min(H, y + win), x0:x1]
        if crop.size:
            crop = _strip_rules(crop)
            for sc in scales:
                big = cv2.resize(crop, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
                try:
                    txt = _run_tesseract(cmd, big, psm, lang, whitelist=whitelist)
                except Exception:
                    continue
                val = finder(txt)
                if val:
                    return val
        y += step
    return None


def _read_header(cmd, img, title_y, template, lang):
    """Returns (ref_no, [date candidates in order of trust])."""
    H, W = img.shape
    ref = None
    dates = []
    if template == 'new' and title_y is not None:
        y0, y1 = max(0, title_y - 110), min(H, title_y + 160)
        crop = _strip_rules(img[y0:y1, int(W * 0.52):int(W * 0.99)])
        for sc in (2, 3):
            big = cv2.resize(crop, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
            try:
                txt = _run_tesseract(cmd, big, 6, lang)
            except Exception:
                continue
            ref = ref or _find_ref(txt)
            d = _find_date(txt)
            if d and d not in dates:
                dates.append(d)
    ys = (title_y - 110) if title_y is not None else int(H * 0.08)
    if not ref:
        ref = _slide(cmd, img, 0.55, 0.99, ys, ys + 300, 70, 30, 7, 'MIRmir#_-:.0123456789 ', _find_ref, lang=lang)
    if template != 'new' or len(dates) < 2:
        d = _slide(cmd, img, 0.55, 0.99, ys, ys + 300, 70, 30, 7, '0123456789/-. ', _find_date, lang=lang)
        if d:
            dates.append(d)
    return ref, dates


# ---------------------------------------------------------------------------
# table (new form: SN | MATERIAL DISCRETION | UNIT | Q.T | TOTAL)
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r'^[\d,]+(\.\d+)?$')


def _clean_num_token(t):
    t = re.sub(r'^[^\d]+|[^\d.,]+$', '', t)  # strip leading/trailing junk such as | or ]
    return t if t and _NUM_RE.match(t) else None


def _to_num(t):
    try:
        return float(t.replace(',', ''))
    except ValueError:
        return None


def _read_table_new(words, fuzzy_unit):
    """Returns (rows, flags). Columns come from the printed header words, not from guessed text splits."""
    H = max((w['y'] + w['h'] for w in words), default=0)
    mat = [w for w in words if re.match(r'^MATERR?IAL$', w['t'].strip('|'), re.I)]
    unit = [w for w in words if w['t'].strip('|').upper() == 'UNIT']
    tot = [w for w in words if w['t'].strip('|:').upper() == 'TOTAL']
    if not mat or not unit:
        return None, ['table_header_not_found']
    mat = mat[0]
    unit = min(unit, key=lambda w: abs(w['y'] - mat['y']))
    same_row = [w for w in tot if abs(w['y'] - unit['y']) < 45 and w['x'] > unit['x']]
    if not same_row:
        return None, ['table_header_not_found']
    tw = max(same_row, key=lambda w: w['x'])
    unit_c, tot_c = unit['x'] + unit['w'] / 2.0, tw['x'] + tw['w'] / 2.0
    pitch = (tot_c - unit_c) / 2.0
    if pitch < 60:
        return None, ['table_header_not_found']
    b_unit_l, b_unit_qt, b_qt_tot = unit_c - pitch / 2, unit_c + pitch / 2, tot_c - pitch / 2
    desc_left = mat['x'] - 60
    y_top = max(unit['y'], mat['y']) + max(unit['h'], mat['h']) + 8
    foot = [w for w in tot if w['y'] > y_top + 40 and w['x'] < desc_left + 900 and w['x'] < unit_c - 100]
    prep = [w for w in words if re.match(r'^(PREPARED|PROJECT)', w['t'], re.I) and w['y'] > y_top + 40]
    y_end = min([w['y'] for w in foot + prep] or [y_top + 450]) - 4
    zone = [w for w in words if y_top <= w['y'] + w['h'] / 2 <= y_end and (re.search(r'[A-Za-z0-9]', w['t']))]
    rows, flags = [], []
    for ln in _text_lines(zone):
        d, u, q, t, confs = [], [], [], [], []
        for w in ln['w']:
            cx = w['x'] + w['w'] / 2.0
            if cx < desc_left:
                continue  # SN column
            if cx < b_unit_l:
                d.append(w)
            elif cx < b_unit_qt:
                u.append(w)
            elif cx < b_qt_tot:
                q.append(w)
            else:
                t.append(w)
            if w['c'] >= 0:
                confs.append(w['c'])
        desc_tokens = [w['t'] for w in d if re.search(r'[A-Za-z0-9]', w['t'])]
        while desc_tokens and re.fullmatch(r'[\d\W]{1,3}', desc_tokens[0]):
            desc_tokens.pop(0)  # stray SN / border debris before the text
        desc = ' '.join(desc_tokens).strip()
        un, fz = (None, False)
        for w in u:
            un, fz = fuzzy_unit(w['t'])
            if un:
                break
        if not un and desc_tokens:  # unit text that drifted into the description column
            cand, fz2 = fuzzy_unit(desc_tokens[-1])
            if cand and re.fullmatch(r'[A-Za-z]{2,6}', desc_tokens[-1]) and desc_tokens[-1].lower() not in ('white', 'black', 'cable'):
                un, fz = cand, fz2
                desc = ' '.join(desc_tokens[:-1]).strip()
        qn = [n for n in (_clean_num_token(w['t']) for w in q) if n]
        tn = [n for n in (_clean_num_token(w['t']) for w in t) if n]
        qty = _to_num(qn[0]) if qn else None
        total = _to_num(tn[0]) if tn else None
        rflags = []
        if len(qn) > 1 or len(tn) > 1:
            rflags.append('qty_multiple_numbers')
        if un is None and qty is None and total is None:
            real = [w['t'] for w in d if len(re.sub(r'[^A-Za-z]', '', w['t'])) >= 4 and w['c'] >= 60]
            if rows and real:
                rows[-1]['extra'] = (rows[-1].get('extra', '') + ' ' + ' '.join(real)).strip()
            continue
        if sum(c.isalpha() for c in desc) < 3:
            continue
        rows.append({'sn': None, 'description': desc, 'unit': un, 'unit_fuzzy': fz, 'qty': qty, 'total': total,
                     'conf': round(sum(confs) / len(confs), 1) if confs else None, 'row_flags': rflags})
    return rows, flags


# ---------------------------------------------------------------------------
# old form (Arabic layout): only a lenient single-row draft is attempted
# ---------------------------------------------------------------------------
def _old_unit(tok, fuzzy_unit):
    """Old-form OCR often truncates the unit ('Ro,' / 'Roly'): accept an unambiguous prefix."""
    letters = re.sub(r'[^A-Za-z]', '', tok).lower()
    if not letters:
        return None
    if letters.startswith('ro'):
        return 'ROLL'
    if letters in ('pc', 'pcs'):
        return 'PC'
    u, _fz = fuzzy_unit(letters)
    return u


def _parse_old_line(line, fuzzy_unit):
    toks = [t for t in re.split(r'\s+', line.strip()) if t]
    for i, t in enumerate(toks[:-1]):
        n = re.sub(r'[^\d]', '', t)
        if not n or len(n) > 5 or re.search(r'[A-Za-z]', t):
            continue
        unit = _old_unit(toks[i + 1], fuzzy_unit)
        if not unit:
            continue
        invoice = None
        if i > 0:
            prev = re.sub(r'[^\d]', '', toks[i - 1])
            if len(prev) >= 3:
                invoice = prev
        desc_tokens = [x for x in toks[i + 2:] if re.search(r'[A-Za-z]', x) and len(re.sub(r'[^A-Za-z0-9]', '', x)) >= 3]
        desc = ' '.join(re.sub(r'[^A-Za-z0-9\-./()]', '', x) for x in desc_tokens).strip()
        return {'qty': float(n), 'unit': unit, 'invoice_no': invoice, 'description': desc or None}
    return None


def _read_table_old(cmd, img, title_y, fuzzy_unit, lang):
    H, W = img.shape
    ty = title_y if title_y is not None else int(H * 0.17)
    for y0, y1 in ((ty + 150, ty + 420), (ty + 230, ty + 560), (ty + 300, ty + 650)):
        crop = _strip_rules(img[max(0, y0):min(H, y1), int(W * 0.25):int(W * 0.92)])
        for sc in (2, 3):
            big = cv2.resize(crop, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
            try:
                txt = _run_tesseract(cmd, big, 6, lang)
            except Exception:
                continue
            for line in txt.splitlines():
                r = _parse_old_line(line, fuzzy_unit)
                if r:
                    return {'sn': None, 'description': r['description'], 'unit': r['unit'], 'unit_fuzzy': True, 'qty': r['qty'],
                            'total': None, 'conf': None, 'invoice_no': r['invoice_no'], 'row_flags': ['old_form_partial']}
    return None


# ---------------------------------------------------------------------------
# MIR status check-boxes (new form: A | B | C | D, left -> right)
# ---------------------------------------------------------------------------
def _read_status(img, words):
    """Return {'value','confidence','ink','note'}. Never returns a value unless one box clearly stands out."""
    H, W = img.shape
    labels = [w for w in words if re.match(r'^(Reject|Appr)', w['t'], re.I) and w['y'] > H * 0.4]
    if not labels:
        return {'value': None, 'confidence': 'none', 'ink': [], 'note': 'status labels not found'}
    ly = int(sorted(w['y'] for w in labels)[len(labels) // 2])
    bw = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 15)
    cnts, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cand = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if 66 <= w <= 140 and 34 <= h <= 95 and 1.2 <= w / float(h) <= 2.8 and ly - 60 <= y <= ly + 70:
            cand.append((x + w // 2, y + h // 2))
    if len(cand) < 4:
        return {'value': None, 'confidence': 'none', 'ink': [], 'note': 'check-boxes not found'}
    med_y = sorted(c[1] for c in cand)[len(cand) // 2]
    cand = sorted(c for c in cand if abs(c[1] - med_y) <= 20)
    clusters = []
    for c in cand:
        if clusters and c[0] - clusters[-1][-1][0] < 60:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    centres = [(int(np.mean([c[0] for c in k])), int(np.mean([c[1] for c in k]))) for k in clusters]
    if len(centres) != 4:
        return {'value': None, 'confidence': 'none', 'ink': [], 'note': '%d check-boxes found (expected 4)' % len(centres)}
    inks = []
    for cx, cy in centres:
        reg = bw[cy - 11:cy + 12, cx - 28:cx + 29]
        inks.append(round(float(reg.mean() / 255.0), 3))
    order = sorted(range(4), key=lambda i: -inks[i])
    top, second = inks[order[0]], inks[order[1]]
    if top < 0.05:
        return {'value': None, 'confidence': 'none', 'ink': inks, 'note': 'no box looks marked'}
    if top >= 1.5 * max(second, 0.001) and (second < 0.05 or top >= 2.0 * second):
        return {'value': 'ABCD'[order[0]], 'confidence': 'medium', 'ink': inks, 'note': 'ink density; verify on the page image'}
    return {'value': None, 'confidence': 'none', 'ink': inks, 'note': 'two boxes look marked (stamp or overwriting) - choose manually'}


# ---------------------------------------------------------------------------
# page reader
# ---------------------------------------------------------------------------
def _scale_words(ws, f):
    return [dict(w, x=int(w['x'] / f), y=int(w['y'] / f), w=int(w['w'] / f), h=int(w['h'] / f)) for w in ws]


def read_page(cmd, img, filename_hint=None, lang='eng', fuzzy_unit=None):
    """img: prepared (normalised, deskewed) gray image. Returns a dict of DRAFT values."""
    words = _words(cmd, img, 6, lang)
    alltxt = ' '.join(w['t'] for w in words)
    if re.search(r'MATERR?IAL\s+INSPECTION|DISCRETION', alltxt) or any(w['t'].strip('|').upper() == 'UNIT' for w in words):
        template = 'new'
    elif re.search(r'Inspection', alltxt, re.I):
        template = 'old'
    else:
        template = 'unknown'
    t_req = [w for w in words if re.fullmatch(r'REQUEST', w['t'], re.I) and w['y'] < img.shape[0] * 0.35]
    title_y = t_req[0]['y'] if t_req else None
    flags = []
    ref, date_cands = _read_header(cmd, img, title_y, template, lang)
    # Dates printed elsewhere on the page (the contractor's "From ... Date:" box repeats the MIR date): used as a cross-check.
    page_dates = []
    for ln in _text_lines(words):
        page_dates += _all_dates(' '.join(w['t'] for w in ln['w']))
    votes = {}
    for d in date_cands + page_dates:
        votes[d] = votes.get(d, 0) + 1
    date, date_alt = None, []
    if votes:
        ranked = sorted(votes.items(), key=lambda kv: (-kv[1], date_cands.index(kv[0]) if kv[0] in date_cands else 99))
        date = ranked[0][0]
        if len(ranked) > 1 and (ranked[0][1] < 2 or ranked[1][1] >= ranked[0][1]):
            flags.append('date_uncertain')
            date_alt = [k for k, _v in ranked[1:3]]
    hint = filename_hint or None
    ref_source = 'header' if ref else None
    if ref and hint and re.sub(r'\D', '', ref).lstrip('0') != re.sub(r'\D', '', hint).lstrip('0'):
        flags.append('ref_conflict_filename')
    if not ref and hint:
        ref, ref_source = hint, 'filename'
        flags.append('ref_from_filename')
    rows, status = None, {'value': None, 'confidence': 'none', 'ink': [], 'note': 'not attempted'}
    if template == 'new':
        # photographed pages: if the first reading finds no table, retry with other segmentation / scale
        tf = ['table_header_not_found']
        used = words
        for psm, sc in ((6, 1.0), (4, 1.0), (6, 1.5), (11, 1.0)):
            if (psm, sc) == (6, 1.0):
                ws = words
            else:
                big = img if sc == 1.0 else cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
                try:
                    ws = _scale_words(_words(cmd, big, psm, lang), sc)
                except Exception:
                    continue
            rows, tf = _read_table_new(ws, fuzzy_unit)
            if rows:
                used = ws
                break
        flags += tf or []
        status = _read_status(img, used if any(re.match(r'^(Reject|Appr)', w['t'], re.I) for w in used) else words)
    elif template == 'old':
        r = _read_table_old(cmd, img, title_y, fuzzy_unit, lang)
        rows = [r] if r else []
        flags.append('old_form_partial')
        status = {'value': None, 'confidence': 'none', 'ink': [], 'note': 'old Arabic form: status not read automatically'}
    lines = _text_lines(words)
    raw = '\n'.join(' '.join(w['t'] for w in ln['w']) for ln in lines)[:4000]
    return {'template': template, 'ref_no': ref, 'ref_source': ref_source, 'date': date, 'date_alternatives': date_alt,
            'rows': rows or [], 'status': status, 'flags': flags, 'raw_text': raw}

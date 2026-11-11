"""Colour-highlighter detection for scanned As-Built pages.

Pink and red are normalised to 'pink'.  Blue ballpoint ink is NOT a markup
status - it only draws the relocation arrow for orange markups and is handled
by asbuilt_arrow.py.

detect_markups(image) -> list of
    {color, bbox, center, area, legend, _mask}
``legend`` is True for the colour key printed/handwritten in the page corner
(a stack of coloured bars) - it must never be treated as a real markup.
``_mask`` is (x, y, w, h, bool array) of the blob; it is not JSON-serialisable.
"""
import cv2
import numpy as np


def detect_markups(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    height, width = image.shape[:2]
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    masks = {
        'orange': ((hue >= 6) & (hue <= 22) & (sat >= 95) & (val >= 150)),
        'yellow': ((hue >= 23) & (hue <= 38) & (sat >= 95) & (val >= 150)),
        'green': ((hue >= 40) & (hue <= 90) & (sat >= 95) & (val >= 150)),
        'pink': (((hue >= 160) | (hue <= 5)) & (sat >= 95) & (val >= 150)),
    }
    # blob size limits scale with the page (tuned on ~2300 px wide scans)
    s = max(0.5, min(3.0, min(height, width) / 2300.0))
    amin, amax = int(120 * s * s), int(9000 * s * s)
    out = []
    kernel = np.ones((9, 9), np.uint8)
    for color, mask in masks.items():
        closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
        count, labels, stats, centers = cv2.connectedComponentsWithStats(closed, 8)
        for k in range(1, count):
            x, y, w, h, area = [int(v) for v in stats[k]]
            if not amin <= area <= amax:
                continue
            blob = (labels[y:y + h, x:x + w] == k)
            out.append({'color': color, 'bbox': [x, y, w, h],
                        'center': [float(centers[k][0]), float(centers[k][1])],
                        'area': area, 'legend': False, '_mask': (x, y, w, h, blob)})
    _flag_legend(out, width, height)
    return out


def _flag_legend(marks, width, height):
    """The colour key is a vertical stack of >=3 elongated bars in the top-right
    corner with at least two different colours.  Mark every bar of that stack."""
    cand = [m for m in marks
            if m['center'][0] > width * 0.62 and m['center'][1] < height * 0.30
            and m['bbox'][2] / max(m['bbox'][3], 1) >= 1.7]
    if len(cand) < 3 or len({m['color'] for m in cand}) < 2:
        return
    xs = np.array([m['center'][0] for m in cand])
    # keep the dominant vertical column
    med = float(np.median(xs))
    col = [m for m in cand if abs(m['center'][0] - med) <= width * 0.06]
    if len(col) >= 3 and len({m['color'] for m in col}) >= 2:
        for m in col:
            m['legend'] = True

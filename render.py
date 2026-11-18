"""Rasterize flattened DXF geometry for As-Built registration."""
import numpy as np
import cv2

def render(polylines, bbox, width=3000, margin=24):
    x0, y0, x1, y1 = [float(v) for v in bbox]
    if x1 <= x0 or y1 <= y0:
        raise ValueError('Invalid CAD render bounds.')
    width = int(width)
    height = max(1, int(round(width * (y1-y0) / (x1-x0))))
    height = min(height, 12000)
    scale = min((width-2*margin)/(x1-x0), (height-2*margin)/(y1-y0))
    out = np.full((height, width, 3), 255, np.uint8)
    for line in polylines:
        if len(line) < 2:
            continue
        pts = [(int(round(margin+(float(x)-x0)*scale)),
                int(round(height-margin-(float(y)-y0)*scale))) for x, y in line]
        if len(pts) >= 2:
            cv2.polylines(out, [np.asarray(pts, np.int32)], False, (0,0,0), 1, cv2.LINE_AA)
    return out, float(scale)

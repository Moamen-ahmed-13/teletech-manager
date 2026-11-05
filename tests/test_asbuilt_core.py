import tempfile
import unittest
from pathlib import Path

import cv2
import ezdxf
import numpy as np

import asbuilt_arrow as AR
import asbuilt_markups as MK
from dxfedit import Dxf, Editor, verify_output


def _page_with_arrow(decoy=False):
    img = np.full((900, 700, 3), 252, np.uint8)
    cv2.circle(img, (300, 200), 26, (40, 150, 255), -1)                  # orange highlighter (BGR)
    pts = np.array([[300, 200], [262, 310], [236, 387]], np.int32)
    cv2.polylines(img, [pts], False, (120, 40, 25), 4)                    # blue ballpoint shaft
    cv2.fillPoly(img, [np.array([[225, 425], [249, 391], [223, 383]], np.int32)], (120, 40, 25))   # aligned head
    cv2.line(img, (500, 100), (500, 600), (90, 90, 95), 3)                # printed grey wall (must be ignored)
    if decoy:
        cv2.putText(img, 'abc', (320, 260), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (120, 40, 25), 2)
    return img


class ArrowTests(unittest.TestCase):
    def test_confirms_arrowhead_apex_not_far_pixel_or_wall(self):
        r = AR.detect_blue_arrow_tips(_page_with_arrow(), [{'bbox': [274, 174, 52, 52]}])[1]
        self.assertEqual(r['status'], 'CONFIRMED')
        self.assertLess(abs(r['tip_px'][0] - 225), 14)
        self.assertLess(abs(r['tip_px'][1] - 425), 14)

    def test_plain_blue_line_without_head_is_not_confirmed(self):
        img = np.full((900, 700, 3), 252, np.uint8)
        cv2.circle(img, (300, 200), 26, (40, 150, 255), -1)
        cv2.line(img, (300, 200), (240, 420), (120, 40, 25), 4)
        r = AR.detect_blue_arrow_tips(img, [{'bbox': [274, 174, 52, 52]}])[1]
        self.assertNotEqual(r['status'], 'CONFIRMED')

    def test_no_blue_stroke_is_not_found(self):
        img = np.full((900, 700, 3), 252, np.uint8)
        cv2.circle(img, (300, 200), 26, (40, 150, 255), -1)
        r = AR.detect_blue_arrow_tips(img, [{'bbox': [274, 174, 52, 52]}])[1]
        self.assertEqual(r['status'], 'NOT_FOUND')


class LegendTests(unittest.TestCase):
    def test_corner_colour_key_is_flagged(self):
        img = np.full((1000, 800, 3), 255, np.uint8)
        for i, col in enumerate([(180, 60, 255), (60, 220, 60), (40, 230, 240), (40, 150, 255)]):
            cv2.rectangle(img, (600, 60 + i * 55), (660, 80 + i * 55), col, -1)
        cv2.rectangle(img, (200, 600), (240, 630), (180, 60, 255), -1)     # real markup
        marks = MK.detect_markups(img)
        self.assertEqual(sum(m['legend'] for m in marks), 4)
        self.assertEqual(sum(not m['legend'] for m in marks), 1)


class RotatedNestedEditTests(unittest.TestCase):
    def make(self, path):
        doc = ezdxf.new()
        doc.blocks.new('EP', base_point=(5, 5)).add_line((0, 0), (1, 1))
        mid = doc.blocks.new('MID')
        mid.add_blockref('EP', (10, 0))
        mid.add_blockref('EP', (20, 0))
        msp = doc.modelspace()
        msp.add_blockref('MID', (100, 100), dxfattribs={'rotation': 90, 'xscale': 2, 'yscale': 2})
        msp.add_blockref('MID', (900, 900))
        doc.saveas(path)

    def test_rotated_scaled_nested_move_delete_are_instance_local_and_verified(self):
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td) / 'a.dxf', Path(td) / 'b.dxf'
            self.make(src)
            d = Dxf(src)
            inst = [i for i in d.instances({'EP'}) if i['depth'] == 1]
            first = [i for i in inst if i['world'][0] < 500]
            first.sort(key=lambda i: i['world'][1])
            ed = Editor(d)
            moved, gone = first[0], first[1]
            dest = (moved['world'][0] + 37.0, moved['world'][1] - 11.0)
            ed.move(moved, dest)
            ed.delete(gone)
            ed.write(out)
            res = verify_output(src, out, [{'path': [p['start'] for p in gone['path']]}],
                                [{'path': [p['start'] for p in moved['path']], 'delta': [37.0, -11.0]}])
            self.assertTrue(res['ok'], res['problems'])
            after = sorted(tuple(round(v, 1) for v in i['world']) for i in Dxf(out).instances({'EP'}))
            self.assertIn((round(dest[0], 1), round(dest[1], 1)), after)
            # the second MID instance (900,900) keeps both EP children
            self.assertEqual(sum(1 for w in after if w[0] > 500), 2)

    def test_refuses_to_overwrite_original(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'a.dxf'
            self.make(src)
            with self.assertRaises(ValueError):
                Editor(Dxf(src)).write(src)


if __name__ == '__main__':
    unittest.main()

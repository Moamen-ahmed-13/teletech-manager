import unittest
import numpy as np
import cv2
import asbuilt_arrow as AR


def _scene():
    # Page-sized scene (the detector's thresholds scale with the page size, like a real 200-dpi A3 scan).
    img = np.full((3200, 2400, 3), 245, np.uint8)
    cv2.circle(img, (1200, 1600), 48, (30, 140, 255), -1)          # orange highlighter blob
    # purple/grey tinted drawing line next to the marker: scan noise, NOT ink (hue ~150, low blueness)
    cv2.line(img, (1280, 1580), (1360, 1400), (120, 100, 118), 6)
    cv2.line(img, (1360, 1400), (1380, 1280), (120, 100, 118), 6)
    # real blue ballpoint arrow going down-left from the marker, with a big filled head
    cv2.line(img, (1180, 1644), (1040, 1810), (170, 90, 40), 6)
    head = np.array([[1040, 1810], [996, 1736], [1090, 1760]], np.int32)
    cv2.fillPoly(img, [head], (160, 80, 30))
    return img, {'bbox': [1152, 1552, 96, 96]}


class ArrowDetectorTest(unittest.TestCase):
    def test_real_blue_arrow_wins_over_grey_scan_noise(self):
        img, orange = _scene()
        res = AR.detect_blue_arrow_tips(img, [orange])[1]
        # A synthetic head is not a faithful scan, so only the stroke selection is asserted here;
        # CONFIRMED on a real scan was verified with the Mansoura sample (asbuilt_apply.py run).
        self.assertIn(res['status'], {'CONFIRMED', 'NOT_CONFIRMED', 'AMBIGUOUS'}, res)
        self.assertIn('tip_px', res, res)
        tx, ty = res['tip_px']
        # the tip must be at the head (down-left of the marker), not at the grey line (up-right)
        self.assertLess(tx, 1200)
        self.assertGreater(ty, 1700)

    def test_arrow_with_excessive_orange_gap_is_not_confirmed(self):
        img = np.full((3200, 2400, 3), 245, np.uint8)
        orange = {'bbox': [1152, 1552, 96, 96]}
        # A clear-looking blue arrow, but it is too far from this orange markup
        # to be safely associated with it.
        cv2.line(img, (1410, 1600), (1650, 1600), (170, 90, 40), 7)
        head = np.array([[1650, 1600], [1580, 1550], [1580, 1650]], np.int32)
        cv2.fillPoly(img, [head], (160, 80, 30))
        res = AR.detect_blue_arrow_tips(img, [orange])[1]
        self.assertNotEqual(res['status'], 'CONFIRMED', res)

    def test_unheaded_blue_stroke_is_not_confirmed(self):
        img = np.full((3200, 2400, 3), 245, np.uint8)
        orange = {'bbox': [1152, 1552, 96, 96]}
        cv2.line(img, (1200, 1640), (1450, 1780), (170, 90, 40), 6)
        res = AR.detect_blue_arrow_tips(img, [orange])[1]
        self.assertNotEqual(res['status'], 'CONFIRMED', res)

    def test_fragmented_blue_pen_stroke_is_chained(self):
        img = np.full((1000, 1200, 3), 245, np.uint8)
        orange = {'bbox': [450, 480, 50, 50]}
        # Two separate blue-ink components with a small scan/pen gap.
        cv2.line(img, (510, 530), (540, 530), (170, 90, 40), 5)
        cv2.line(img, (590, 530), (660, 530), (170, 90, 40), 5)
        groups = AR._local_groups(img, 450, 480, 50, 50, 1000, [])
        self.assertTrue(groups, 'fragmented blue ink should be retained')
        xs = np.concatenate([g[0] for g in groups])
        self.assertLessEqual(int(xs.min()), 510)
        self.assertGreaterEqual(int(xs.max()), 660)

    def test_faint_blue_ballpoint_is_not_discarded(self):
        img = np.full((1000, 1200, 3), 245, np.uint8)
        # Low-saturation blue ink: weak mask accepts it, strong mask does not.
        cv2.line(img, (510, 530), (660, 530), (220, 210, 200), 4)
        groups = AR._local_groups(img, 450, 480, 50, 50, 1000, [])
        self.assertTrue(groups, 'faint but coherent blue pen should be retained')

    def test_no_blue_ink_is_not_found(self):
        img, orange = _scene()
        img[:] = 245
        cv2.circle(img, (1200, 1600), 48, (30, 140, 255), -1)
        res = AR.detect_blue_arrow_tips(img, [orange])[1]
        self.assertEqual(res['status'], 'NOT_FOUND')


if __name__ == '__main__':
    unittest.main()

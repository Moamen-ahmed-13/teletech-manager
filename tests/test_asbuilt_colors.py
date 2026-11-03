import unittest
import cv2
import numpy as np
from asbuilt_apply import detect_markups

class MarkupColorTests(unittest.TestCase):
    def test_red_and_magenta_both_normalize_to_pink(self):
        image=np.full((240,320,3),255,np.uint8)
        cv2.rectangle(image,(20,40),(55,75),(0,0,255),-1)       # red
        cv2.rectangle(image,(100,40),(135,75),(255,0,180),-1)  # magenta
        marks=detect_markups(image)
        self.assertEqual(sum(m['color']=='pink' for m in marks),2)

    def test_blue_pen_is_not_a_markup_status(self):
        image=np.full((180,240,3),255,np.uint8)
        cv2.line(image,(20,20),(160,120),(255,0,0),5)
        self.assertEqual(detect_markups(image),[])

if __name__ == '__main__':
    unittest.main()

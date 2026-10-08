import unittest
import numpy as np
import cv2
from modules_gtk.cam_calibration_view import (
    extract_magnified_pixbuf,
    apply_edge_contrast_filter,
    apply_noise_inspector_filter,
    apply_difference_filter,
    detect_dart_tip,
    frame_to_pixbuf,
)
from core.board_detector import BoardROI


class TestCamCalibrationReview(unittest.TestCase):
    def setUp(self):
        # Create a synthetic 1280x960 test frame
        self.frame = np.full((960, 1280, 3), 120, dtype=np.uint8)
        # Draw some target wires and markings
        cv2.circle(self.frame, (640, 480), 300, (255, 255, 255), 2)
        cv2.circle(self.frame, (640, 480), 20, (0, 0, 255), -1)
        cv2.rectangle(self.frame, (620, 160), (660, 200), (0, 255, 0), 2)

    def test_extract_magnified_pixbuf_full(self):
        pix, crop_box = extract_magnified_pixbuf(
            self.frame,
            center=(640, 480),
            zoom_factor=1.0,
            max_w=560,
            max_h=380,
            filter_mode="normal",
            show_reticle=False,
        )
        self.assertIsNotNone(pix)
        self.assertEqual(crop_box, (0, 0, 1280, 960))
        self.assertLessEqual(pix.get_width(), 560)
        self.assertLessEqual(pix.get_height(), 380)

    def test_extract_magnified_pixbuf_zoom(self):
        pix, (x1, y1, cw, ch) = extract_magnified_pixbuf(
            self.frame,
            center=(640, 180),
            zoom_factor=2.5,
            max_w=560,
            max_h=380,
            filter_mode="normal",
            show_reticle=True,
        )
        self.assertIsNotNone(pix)
        self.assertEqual(cw, int(1280 / 2.5))
        self.assertEqual(ch, int(960 / 2.5))
        self.assertGreaterEqual(x1, 0)
        self.assertGreaterEqual(y1, 0)
        self.assertLessEqual(x1 + cw, 1280)
        self.assertLessEqual(y1 + ch, 960)

    def test_extract_magnified_pixbuf_edge_filter(self):
        pix, _ = extract_magnified_pixbuf(
            self.frame,
            center=(640, 480),
            zoom_factor=2.0,
            filter_mode="edge",
        )
        self.assertIsNotNone(pix)

    def test_extract_magnified_pixbuf_noise_filter(self):
        pix, _ = extract_magnified_pixbuf(
            self.frame,
            center=(640, 480),
            zoom_factor=2.0,
            filter_mode="noise",
        )
        self.assertIsNotNone(pix)

    def test_extract_magnified_pixbuf_diff_filter(self):
        dart_frame = self.frame.copy()
        cv2.line(dart_frame, (640, 480), (600, 300), (20, 20, 20), 4)
        pix, _ = extract_magnified_pixbuf(
            dart_frame,
            center=(640, 480),
            zoom_factor=2.0,
            filter_mode="diff",
            diff_reference_frame=self.frame,
        )
        self.assertIsNotNone(pix)

    def test_extract_magnified_pixbuf_boundary_clamping(self):
        # Click near the corner
        pix, (x1, y1, cw, ch) = extract_magnified_pixbuf(
            self.frame,
            center=(10, 10),
            zoom_factor=4.0,
            max_w=560,
            max_h=380,
        )
        self.assertIsNotNone(pix)
        self.assertEqual(x1, 0)
        self.assertEqual(y1, 0)
        self.assertEqual(cw, int(1280 / 4.0))
        self.assertEqual(ch, int(960 / 4.0))

    def test_apply_difference_filter(self):
        empty = np.full((480, 640, 3), 100, dtype=np.uint8)
        dart = empty.copy()
        cv2.circle(dart, (320, 240), 15, (255, 255, 255), -1)
        diff_img = apply_difference_filter(empty, dart)
        self.assertIsNotNone(diff_img)
        self.assertEqual(diff_img.shape, (480, 640, 3))

    def test_detect_dart_tip(self):
        empty = np.full((480, 640, 3), 100, dtype=np.uint8)
        dart = empty.copy()
        # Draw simulated dart shaft and tip pointing towards bullseye at (320, 240)
        cv2.line(dart, (320, 240), (280, 100), (20, 20, 20), 6)
        
        mask = np.full((480, 640), 255, dtype=np.uint8)
        roi = BoardROI(
            center=(320, 240),
            radius=200,
            board_mask=mask,
            wire_mask=mask,
            uniform_mask=mask,
            confidence=0.95,
            target_roi=(300, 150, 40, 40),
        )
        tip = detect_dart_tip(empty, dart, roi=roi)
        self.assertIsNotNone(tip)
        tx, ty = tip
        # Tip should be closest point to center (320, 240)
        self.assertAlmostEqual(tx, 320, delta=15)
        self.assertAlmostEqual(ty, 240, delta=15)


if __name__ == "__main__":
    unittest.main()

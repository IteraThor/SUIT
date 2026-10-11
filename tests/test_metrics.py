import unittest
import numpy as np
import cv2
from core.metrics import QualityEvaluator
from core.board_detector import BoardDetector


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.evaluator = QualityEvaluator()
        self.detector = BoardDetector()

        # Create a synthetic dartboard image (1000x1000)
        self.frame = np.full((1000, 1000, 3), 160, dtype=np.uint8)
        # Draw dartboard outer ring
        cv2.circle(self.frame, (500, 500), 380, (40, 40, 40), -1)
        # Draw some segments
        cv2.circle(self.frame, (500, 500), 350, (230, 230, 230), -1)
        # Draw sharp wire lines
        for angle in range(0, 360, 18):
            rad = np.deg2rad(angle)
            x2 = int(500 + 380 * np.cos(rad))
            y2 = int(500 + 380 * np.sin(rad))
            cv2.line(self.frame, (500, 500), (x2, y2), (10, 10, 10), 3)

    def test_edge_steepness_and_temporal_noise(self):
        roi = self.detector.detect_board(self.frame)
        self.assertTrue(roi.confidence > 0)

        # Generate a burst of 3 frames with small simulated noise
        burst = []
        for _ in range(3):
            noise = np.random.normal(0, 1.5, self.frame.shape).astype(np.int16)
            noisy_f = np.clip(self.frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
            burst.append(noisy_f)

        metrics = self.evaluator.evaluate(burst, roi)

        self.assertGreater(metrics.edge_steepness, 10.0)
        self.assertGreater(metrics.temporal_noise, 0.1)
        self.assertGreater(metrics.composite_score, 0.0)
        self.assertLessEqual(metrics.composite_score, 100.0)
        self.assertGreater(metrics.wire_fwhm, 0.0)
        self.assertGreaterEqual(metrics.halo_overshoot, 0.0)

    def test_wire_fwhm_sharp_vs_blurred(self):
        roi = self.detector.detect_board(self.frame)

        # Sharp frame FWHM
        fwhm_sharp = self.evaluator.calculate_wire_fwhm(self.frame, roi)

        # Blur frame heavily (simulate defocused camera)
        blurred_frame = cv2.GaussianBlur(self.frame, (11, 11), 3.5)
        fwhm_blurred = self.evaluator.calculate_wire_fwhm(blurred_frame, roi)

        # Sharp frame must have a thinner (lower) FWHM wire width than blurred frame
        self.assertLess(fwhm_sharp, fwhm_blurred)

    def test_halo_overshoot_penalizes_digital_ringing(self):
        roi = self.detector.detect_board(self.frame)

        # Create over-sharpened frame with artificial halos
        gaussian = cv2.GaussianBlur(self.frame, (0, 0), 2.0)
        unsharp = cv2.addWeighted(self.frame, 2.5, gaussian, -1.5, 0)

        metrics_clean = self.evaluator.evaluate([self.frame], roi)
        metrics_haloed = self.evaluator.evaluate([unsharp], roi)

        # Haloed frame should have higher halo overshoot
        self.assertGreaterEqual(metrics_haloed.halo_overshoot, metrics_clean.halo_overshoot)

    def test_calculate_dart_difference_metrics(self):
        roi = self.detector.detect_board(self.frame)

        empty_frame = self.frame.copy()
        dart_frame = self.frame.copy()

        # Draw 3 simulated darts in sector 20 (vertical bed at x=480)
        # Dart 1: T20
        cv2.line(dart_frame, (480, 360), (480, 420), (20, 20, 20), 4)
        # Dart 2: S20 outer
        cv2.line(dart_frame, (480, 220), (480, 280), (20, 20, 20), 4)
        # Dart 3: S20 inner
        cv2.line(dart_frame, (480, 440), (480, 480), (20, 20, 20), 4)

        tip_signal, glow_noise, ratio = self.evaluator.calculate_dart_difference_metrics(dart_frame, empty_frame, roi)
        self.assertGreater(tip_signal, 50.0)
        self.assertLess(glow_noise, 10.0)
        self.assertGreater(ratio, 5.0)

        metrics = self.evaluator.evaluate([dart_frame], roi, empty_frame=empty_frame)
        self.assertGreater(metrics.dart_separation, 50.0)
        self.assertLess(metrics.sisal_glow, 10.0)
        self.assertGreater(metrics.composite_score, 0.0)

    def test_cross_gamma_sisal_glow_remains_low(self):
        roi = self.detector.detect_board(self.frame)
        # Simulate non-linear gamma curve shift (gamma = 460 vs 300)
        gamma_lifted = np.clip(np.power(self.frame.astype(np.float32) / 255.0, 0.75) * 255.0, 0, 255).astype(np.uint8)
        _, glow, _ = self.evaluator.calculate_dart_difference_metrics(gamma_lifted, self.frame, roi)
        self.assertLess(glow, 6.0)


if __name__ == "__main__":
    unittest.main()


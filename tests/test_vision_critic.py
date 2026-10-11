import unittest
import numpy as np
import cv2
from core.board_detector import BoardDetector
from core.vision_critic import VisionCritic, CriticMetrics


class TestVisionCritic(unittest.TestCase):
    def setUp(self):
        self.detector = BoardDetector()
        self.critic = VisionCritic()

        # Create a synthetic dartboard image (1000x1000)
        self.base_frame = np.full((1000, 1000, 3), 160, dtype=np.uint8)
        cv2.circle(self.base_frame, (500, 500), 350, (230, 230, 230), -1)
        self.roi = self.detector.detect_board(self.base_frame)

        # Dart frame with inserted darts
        self.dart_frame = self.base_frame.copy()
        # Draw dart 1 (T20 shaft)
        cv2.line(self.dart_frame, (500, 320), (500, 420), (20, 20, 20), 4)
        # Draw dart 2 (S20 outer shaft)
        cv2.line(self.dart_frame, (500, 200), (500, 280), (20, 20, 20), 4)

    def test_extract_hough_shaft_lines(self):
        lines = self.critic.extract_hough_shaft_lines(self.dart_frame, self.roi)
        self.assertGreater(len(lines), 0)

    def test_specular_blooming_detection(self):
        # Clean frame without specular blowout
        clean_bloom = self.critic.calculate_specular_blooming(self.dart_frame, self.roi)
        self.assertEqual(clean_bloom, 0.0)

        # Bloomed frame with saturated metallic glints
        bloomed = self.dart_frame.copy()
        cv2.rectangle(bloomed, (495, 300), (505, 350), (255, 255, 255), -1)
        bloomed_score = self.critic.calculate_specular_blooming(bloomed, self.roi)
        self.assertGreater(bloomed_score, 0.0)

    def test_quantile_tone_match_diff(self):
        # Simulate candidate frame with higher gamma (lifted midtones)
        gamma_cand = np.clip(np.power(self.base_frame.astype(np.float32) / 255.0, 0.7) * 255.0, 0, 255).astype(np.uint8)
        _, raw_diff = self.critic.calculate_quantile_tone_match_diff(gamma_cand, self.base_frame, self.roi, apply_matching=False)
        _, matched_diff = self.critic.calculate_quantile_tone_match_diff(gamma_cand, self.base_frame, self.roi, apply_matching=True)
        self.assertLess(matched_diff, raw_diff)


if __name__ == "__main__":
    unittest.main()

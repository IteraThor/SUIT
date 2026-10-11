import unittest
from core.vision_critic import CriticMetrics
from core.recognition_arbiter import RecognitionArbiter, ArbiterVerdict


class TestRecognitionArbiter(unittest.TestCase):
    def setUp(self):
        self.arbiter = RecognitionArbiter()

        self.baseline_map = {
            "/dev/video0": CriticMetrics(hough_line_count=2, longest_shaft_len=120.0, specular_clip_ratio=0.05, halo_overshoot=0.01, sisal_difference_floor=5.0, needle_contrast_peak=140.0),
            "/dev/video2": CriticMetrics(hough_line_count=3, longest_shaft_len=180.0, specular_clip_ratio=0.04, halo_overshoot=0.01, sisal_difference_floor=4.5, needle_contrast_peak=180.0),
            "/dev/video4": CriticMetrics(hough_line_count=1, longest_shaft_len=80.0, specular_clip_ratio=0.03, halo_overshoot=0.01, sisal_difference_floor=6.0, needle_contrast_peak=100.0),
        }

    def test_approve_improved_candidate(self):
        candidate_map = {
            "/dev/video0": CriticMetrics(hough_line_count=3, longest_shaft_len=150.0, specular_clip_ratio=0.0, halo_overshoot=0.005, sisal_difference_floor=3.2, needle_contrast_peak=190.0),
            "/dev/video2": CriticMetrics(hough_line_count=3, longest_shaft_len=195.0, specular_clip_ratio=0.0, halo_overshoot=0.004, sisal_difference_floor=3.0, needle_contrast_peak=220.0),
            "/dev/video4": CriticMetrics(hough_line_count=2, longest_shaft_len=95.0, specular_clip_ratio=0.0, halo_overshoot=0.006, sisal_difference_floor=4.0, needle_contrast_peak=130.0),
        }
        verdict = self.arbiter.evaluate_verdict(candidate_map, self.baseline_map)
        self.assertTrue(verdict.approved)

    def test_reject_regressed_candidate(self):
        # Candidate with blown highlights and high sisal floor
        regressed_map = {
            "/dev/video0": CriticMetrics(hough_line_count=0, longest_shaft_len=0.0, specular_clip_ratio=0.08, halo_overshoot=0.05, sisal_difference_floor=18.0, needle_contrast_peak=80.0),
            "/dev/video2": CriticMetrics(hough_line_count=1, longest_shaft_len=50.0, specular_clip_ratio=0.07, halo_overshoot=0.04, sisal_difference_floor=15.0, needle_contrast_peak=90.0),
            "/dev/video4": CriticMetrics(hough_line_count=0, longest_shaft_len=0.0, specular_clip_ratio=0.09, halo_overshoot=0.06, sisal_difference_floor=20.0, needle_contrast_peak=60.0),
        }
        verdict = self.arbiter.evaluate_verdict(regressed_map, self.baseline_map)
        self.assertFalse(verdict.approved)


if __name__ == "__main__":
    unittest.main()

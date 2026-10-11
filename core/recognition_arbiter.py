"""Recognition Arbiter: Validates camera tuning against decompiled Autodarts criteria."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List
from core.vision_critic import CriticMetrics


@dataclass
class ArbiterVerdict:
    approved: bool
    composite_gain: float
    reasons: List[str]


class RecognitionArbiter:
    """Evaluates multi-camera candidate metrics against Autodarts recognition gates."""

    def evaluate_verdict(
        self,
        candidate_map: Dict[str, CriticMetrics],
        baseline_map: Dict[str, CriticMetrics],
    ) -> ArbiterVerdict:
        reasons = []
        approved_cams = 0
        total_cams = len(candidate_map)

        for dev, cand in candidate_map.items():
            base = baseline_map.get(dev)
            if not base:
                continue

            # Gate 1: Highlight blowout must not increase
            if cand.specular_clip_ratio > base.specular_clip_ratio + 0.005:
                reasons.append(f"{dev}: Specular clipping increased ({cand.specular_clip_ratio*100:.1f}%)")
                continue

            # Gate 2: Sisal floor noise must stay low (< 10 DN)
            if cand.sisal_difference_floor > 10.0 and cand.sisal_difference_floor > base.sisal_difference_floor:
                reasons.append(f"{dev}: Sisal floor noise high ({cand.sisal_difference_floor:.1f} DN)")
                continue

            # Gate 3: Needle contrast peak or Hough line count should improve
            contrast_improved = cand.needle_contrast_peak >= base.needle_contrast_peak
            hough_improved = cand.hough_line_count >= base.hough_line_count

            if contrast_improved or hough_improved:
                approved_cams += 1
            else:
                reasons.append(f"{dev}: Neither Hough lines nor needle contrast improved")

        # Require majority approval across cameras (at least 2 of 3 cameras)
        is_approved = approved_cams >= max(1, total_cams * 2 // 3)
        gain = (approved_cams / max(1, total_cams)) * 100.0

        return ArbiterVerdict(
            approved=is_approved,
            composite_gain=round(gain, 1),
            reasons=reasons,
        )

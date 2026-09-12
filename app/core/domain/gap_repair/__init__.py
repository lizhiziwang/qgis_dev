"""相邻面封闭缝隙修补领域。"""

from __future__ import annotations

from .service import Gap, detect_gaps, gap_contributors, merge_gap_into

__all__ = ["Gap", "detect_gaps", "gap_contributors", "merge_gap_into"]

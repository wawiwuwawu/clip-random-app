"""
ClipSession / ClipEntry — data model shared by the clip planning stage
(ClipPlanningWorker + preview dialog) and the rendering stage
(ClipRenderWorker).
"""

import os
import random
from dataclasses import dataclass, field


@dataclass
class ClipEntry:
    """One planned clip inside a compilation session."""

    video: str
    start: float
    duration: float
    thumb_path: str = ""
    excluded: bool = False

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class ClipSession:
    """Full planning result handed from the planner to the renderer."""

    clips: list[ClipEntry] = field(default_factory=list)
    target_total: float = 0.0
    temp_dir: str = ""
    portrait: bool = False
    encoder: str = ""
    output_folder: str = ""
    crossfade: float = 0.0
    # Re-roll support: video path -> candidate (start, end) pairs
    candidates_by_video: dict[str, list[tuple[float, float]]] = field(
        default_factory=dict
    )

    def included(self) -> list[ClipEntry]:
        return [c for c in self.clips if not c.excluded]

    def included_total(self) -> float:
        return sum(c.duration for c in self.included())

    def used_ranges(self, skip_index: int = -1) -> dict[str, list[tuple[float, float]]]:
        """Per-video occupied ranges, optionally ignoring one clip index."""
        ranges: dict[str, list[tuple[float, float]]] = {}
        for i, clip in enumerate(self.clips):
            if i == skip_index or clip.excluded:
                continue
            ranges.setdefault(clip.video, []).append((clip.start, clip.end))
        return ranges


def reroll_clip(session: ClipSession, index: int) -> ClipEntry | None:
    """
    Replace the clip at *index* with another random candidate that neither
    overlaps remaining clips nor duplicates an existing selection.

    Returns the refreshed entry, or ``None`` when no alternative exists.
    Thumbnail regeneration is the caller's responsibility.
    """
    if index < 0 or index >= len(session.clips):
        return None

    entry = session.clips[index]
    candidates = session.candidates_by_video.get(entry.video, [])
    if not candidates:
        return None

    remaining_ranges = session.used_ranges(skip_index=index).get(entry.video, [])
    taken_starts = {
        round(c.start, 3)
        for i, c in enumerate(session.clips)
        if i != index and c.video == entry.video and not c.excluded
    }

    pool = [
        (start, end)
        for start, end in candidates
        if round(start, 3) not in taken_starts
        and not any(start < used_end and used_start < end for used_start, used_end in remaining_ranges)
    ]
    if not pool:
        return None

    start, end = random.choice(pool)
    entry.start = start
    entry.duration = end - start
    entry.excluded = False
    return entry

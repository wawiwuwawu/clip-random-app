import unittest

from core.ffmpeg_engine import FFmpegEngine


class TestClipPlan(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = FFmpegEngine()

    def test_plan_clips_basic(self) -> None:
        segments = {
            "video1.mp4": [(0.0, 60.0)],
            "video2.mp4": [(0.0, 60.0)],
        }
        selected, candidates = self.engine.plan_clips(
            videos_segments=segments,
            clip_duration=5.0,
            total_target=20.0,
        )
        self.assertGreater(len(selected), 0)
        total_duration = sum(item["duration"] for item in selected)
        self.assertAlmostEqual(total_duration, 20.0, delta=1.0)

    def test_snap_to_scene(self) -> None:
        start = 10.0
        scene_times = [3.0, 10.2, 20.0]
        snapped = self.engine._snap_to_scene(start, scene_times, window=0.75)
        self.assertEqual(snapped, 10.2)

        # Outside window -> return original start
        snapped_far = self.engine._snap_to_scene(start, scene_times, window=0.1)
        self.assertEqual(snapped_far, 10.0)


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
import subprocess

from core.ffmpeg_engine import (
    FFmpegEngine,
    escape_filter_path,
    build_noise_filters,
    resolve_noise_model_path,
)


class TestFFmpegEngine(unittest.TestCase):

    def setUp(self) -> None:
        self.engine = FFmpegEngine()

    def test_short_path_and_escaping(self) -> None:
        model_path = resolve_noise_model_path()
        self.assertTrue(os.path.exists(model_path), f"Model file missing: {model_path}")

        escaped = escape_filter_path(model_path)
        # The drive-letter colon must be escaped as \: for FFmpeg's parser
        self.assertIn("\\:", escaped)
        # Backslashes (except the \: colon escape) must be normalized to /
        self.assertNotIn("\\\\", escaped)
        # Wrap form used by build_noise_filters must parse in FFmpeg
        self.assertTrue(escaped.startswith("C\\:") or escaped.startswith("/"))

    def test_build_noise_filters_ai(self) -> None:
        filters = build_noise_filters(mode="ai", strength="strong")
        self.assertEqual(len(filters), 2)
        self.assertTrue(filters[0].startswith("arnndn=m='"))
        self.assertIn("\\:", filters[0])  # colon escaped inside quotes

    def test_build_noise_filters_fft(self) -> None:
        filters = build_noise_filters(mode="fft", strength="medium", noise_floor_db=-35.0)
        self.assertTrue(any("afftdn=" in f for f in filters))

    def test_arnndn_filter_execution_live(self) -> None:
        """Test actual FFmpeg audio filtering using arnndn filter headlessly."""
        model_path = resolve_noise_model_path()
        escaped_path = escape_filter_path(model_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            out_file = os.path.join(temp_dir, "test_denoised.m4a")
            ffmpeg_bin = self.engine._ffmpeg_bin("ffmpeg")

            # Run 0.5s audio filter with arnndn (quoted path with escaped colon)
            cmd = [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                "-af", f"arnndn=m='{escaped_path}':mix=1.0",
                "-c:a", "aac", "-b:a", "128k",
                out_file,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(
                result.returncode, 0,
                f"FFmpeg arnndn filter failed (code {result.returncode}): {result.stderr}"
            )
            self.assertTrue(os.path.exists(out_file))
            self.assertGreater(os.path.getsize(out_file), 0)

    def test_detect_scene_changes_unicode(self) -> None:
        dummy_path = "かほとめぐです♡_non_existent.mp4"
        res = self.engine.detect_scene_changes(dummy_path)
        self.assertEqual(res, [])


if __name__ == "__main__":
    unittest.main()

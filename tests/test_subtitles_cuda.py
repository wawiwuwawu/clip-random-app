import unittest

from core.subtitles import _fmt_srt_time, _model_cached, is_model_cached
from core.cuda_setup import ensure_cuda_libs


class TestSubtitlesCUDA(unittest.TestCase):

    def test_srt_time_formatting(self) -> None:
        self.assertEqual(_fmt_srt_time(0.0), "00:00:00,000")
        self.assertEqual(_fmt_srt_time(3661.504), "01:01:01,504")
        self.assertEqual(_fmt_srt_time(59.999), "00:00:59,999")

    def test_model_cache_checking(self) -> None:
        # Bundled tiny model should always be cached
        self.assertTrue(is_model_cached("tiny"))
        # Non-existent model size
        self.assertFalse(_model_cached("non_existent_model_size_xyz"))

    def test_cuda_ensure_libs_callbacks(self) -> None:
        # Test 1-arg lambda callback
        cb1 = lambda frac: None
        res1 = ensure_cuda_libs(progress_cb=cb1)
        self.assertIsInstance(res1, bool)

        # Test 2-arg lambda callback
        cb2 = lambda frac, label: None
        res2 = ensure_cuda_libs(progress_cb=cb2)
        self.assertIsInstance(res2, bool)


if __name__ == "__main__":
    unittest.main()

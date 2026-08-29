import os
import tempfile
import unittest
from pathlib import Path

from core import history


class TestHistory(unittest.TestCase):

    def test_history_entry_capping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = Path(temp_dir) / "test_history.json"
            original = history._history_path
            try:
                history._history_path = lambda: test_file

                for i in range(60):
                    history.append_entry("test_kind", f"out_{i}.mp4", f"out_{i}.mp4", True)

                entries = history.load_entries()
                self.assertEqual(len(entries), 50)
                self.assertEqual(entries[0]["summary"], "out_59.mp4")
            finally:
                history._history_path = original


if __name__ == "__main__":
    unittest.main()

"""
Smart Video Compiler — Entry Point
==================================
Launches the PySide6 UI and wires it to the CompilationWorker and
SilenceRemovalWorker background threads.
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from core.worker import CompilationWorker, SilenceRemovalWorker


class Application:
    """Holds the top-level window and active worker reference."""

    def __init__(self) -> None:
        self.app = QApplication(sys.argv)
        self.app.setStyle("Fusion")

        # Force light palette — prevents Windows dark mode from bleeding through
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#F2F2F2"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#111827"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#2563EB"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        self.app.setPalette(palette)

        self.window = MainWindow()
        self._worker: CompilationWorker | SilenceRemovalWorker | None = None

        # Wire both UI signals to their handlers
        self.window.compilation_requested.connect(self._on_compilation_requested)
        self.window.silence_removal_requested.connect(self._on_silence_removal_requested)
        self.window.cancellation_requested.connect(self._on_cancellation_requested)

    # ------------------------------------------------------------------
    def _on_compilation_requested(
        self,
        input_folder: str,
        output_folder: str,
        total_duration: int,
        clip_duration: int,
        encoder: str,
        skip_silence: bool,
    ) -> None:
        """Create and launch a CompilationWorker (random clip mode)."""
        self._worker = CompilationWorker(
            input_folder=input_folder,
            output_folder=output_folder,
            total_duration=total_duration,
            clip_duration=clip_duration,
            encoder=encoder,
            skip_silence=skip_silence,
        )
        self._start_worker()

    # ------------------------------------------------------------------
    def _on_silence_removal_requested(
        self,
        video_path: str,
        output_folder: str,
        encoder: str,
        threshold_db: int,
        min_duration: float,
        padding: float,
    ) -> None:
        """Create and launch a SilenceRemovalWorker (single-file mode)."""
        self._worker = SilenceRemovalWorker(
            video_path=video_path,
            output_folder=output_folder,
            encoder=encoder,
            threshold_db=threshold_db,
            min_duration=min_duration,
            padding=padding,
        )
        self._start_worker()

    # ------------------------------------------------------------------
    def _on_cancellation_requested(self) -> None:
        """Request interruption of the running worker."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()

    # ------------------------------------------------------------------
    def _start_worker(self) -> None:
        """Wire common worker signals to UI slots and start the thread."""
        if self._worker is None:
            return
        self._worker.log_message.connect(self.window.append_log)
        self._worker.progress_percent.connect(self.window.update_progress)
        self._worker.encoding_complete.connect(self.window.on_compilation_finished)
        self._worker.error_occurred.connect(self.window.on_compilation_error)
        self._worker.finished.connect(self._cleanup_worker)
        self._worker.start()

    # ------------------------------------------------------------------
    def _cleanup_worker(self) -> None:
        """Release the worker after thread completion."""
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    # ------------------------------------------------------------------
    def run(self) -> int:
        """Show the window and enter the Qt event loop."""
        self.window.show()
        return self.app.exec()


if __name__ == "__main__":
    application = Application()
    sys.exit(application.run())

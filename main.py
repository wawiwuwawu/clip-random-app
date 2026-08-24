"""
Smart Video Compiler — Entry Point
==================================
Launches the PySide6 UI and wires it to the background workers.

Job flow
--------
* ``plan_requested``      → ClipPlanningWorker (always launched immediately)
* ``render_requested``    → ClipRenderWorker  (queued when busy)
* ``silence_removal_requested`` → SilenceRemovalWorker (queued when busy)

Every finished/failed job is written to the on-disk history file.
"""

import os
import sys

from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from core import history
from core.ffmpeg_engine import FFmpegEngine
from core.subtitles import app_data_dir  # sets HF_HOME on import
from core.worker import (
    ClipPlanningWorker,
    ClipRenderWorker,
    SilenceRemovalWorker,
)


class Application:
    """Holds the top-level window, the active worker and the job queue."""

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

        self._apply_ffmpeg_override()

        self.window = MainWindow()
        self._worker: (
            ClipPlanningWorker | ClipRenderWorker | SilenceRemovalWorker | None
        ) = None
        self._queue: list = []

        self.window.plan_requested.connect(self._on_plan_requested)
        self.window.render_requested.connect(self._on_render_requested)
        self.window.discard_session_requested.connect(self._on_discard_session)
        self.window.silence_removal_requested.connect(
            self._on_silence_removal_requested
        )
        self.window.cancellation_requested.connect(self._on_cancellation_requested)
        self.window.ffmpeg_override_changed.connect(self._on_ffmpeg_override_changed)

    # ------------------------------------------------------------------
    @staticmethod
    def _apply_ffmpeg_override() -> None:
        """Load the Settings-dialog FFmpeg override into the engine class."""
        override = QSettings("SmartVideoCompiler", "MainWindow").value(
            "ffmpeg_dir", "", type=str
        )
        FFmpegEngine.default_binary_dir = override or None

    # ------------------------------------------------------------------
    def _on_ffmpeg_override_changed(self, directory: str) -> None:
        FFmpegEngine.default_binary_dir = directory or None
        self.window.start_detection()

    # ------------------------------------------------------------------
    # Job submission
    # ------------------------------------------------------------------

    def _submit(self, worker) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._queue.append(worker)
            self.window.update_queue_count(len(self._queue))
            self.window.begin_job(worker.kind)
            self.window.append_log(
                f"Job queued (position {len(self._queue)})."
            )
        else:
            self._launch(worker)

    def _launch(self, worker) -> None:
        self._worker = worker
        worker.log_message.connect(self.window.append_log)
        worker.phase_message.connect(self.window.set_phase)
        worker.progress_percent.connect(self.window.update_progress)
        if hasattr(worker, "plan_ready"):
            worker.plan_ready.connect(self.window.on_plan_ready)
        else:
            worker.encoding_complete.connect(self._on_job_complete)
        worker.error_occurred.connect(self._on_job_error)
        worker.finished.connect(self._on_worker_finished)
        self.window.begin_job(worker.kind)
        worker.start()

    # ------------------------------------------------------------------
    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not None:
            worker.deleteLater()
        if self._worker is worker:
            self._worker = None

        if self._queue:
            nxt = self._queue.pop(0)
            self.window.update_queue_count(len(self._queue))
            self._launch(nxt)

    # ------------------------------------------------------------------
    def _record_history(self, worker, output_path: str, ok: bool) -> None:
        summary = os.path.basename(output_path) if output_path else ""
        history.append_entry(
            mode=worker.kind.replace("-", " ").title(),
            summary=summary,
            output_path=output_path,
            ok=ok,
        )

    def _on_job_complete(self, output_path: str) -> None:
        worker = self._worker
        subtitle_result = getattr(worker, "subtitle_result", None)
        if worker is not None:
            self._record_history(worker, output_path, ok=True)
        self.window.on_compilation_finished(output_path, subtitle_result)

    def _on_job_error(self, error_message: str) -> None:
        worker = self._worker
        cancelled = error_message.strip() == "Cancelled by user."
        if worker is not None and not cancelled:
            self._record_history(worker, "", ok=False)
        self.window.on_compilation_error(error_message)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_plan_requested(
        self,
        sources: list[tuple[str, str]],
        output_folder: str,
        total_duration: int,
        clip_duration: int,
        encoder: str,
        portrait: bool,
        scene_cut: bool,
        crossfade: float,
    ) -> None:
        self._submit(
            ClipPlanningWorker(
                sources=sources,
                output_folder=output_folder,
                total_duration=total_duration,
                clip_duration=clip_duration,
                encoder=encoder,
                portrait=portrait,
                scene_cut=scene_cut,
                crossfade=crossfade,
            )
        )

    def _on_render_requested(self, session) -> None:
        self._submit(ClipRenderWorker(session=session))

    def _on_discard_session(self, session) -> None:
        engine = FFmpegEngine()
        engine.log_callback = self.window.append_log
        engine.cleanup_temp(session.temp_dir)

    def _on_silence_removal_requested(
        self,
        video_path: str,
        output_folder: str,
        encoder: str,
        threshold_db: int,
        min_duration: float,
        padding: float,
        denoise: dict | None = None,
        subtitle: dict | None = None,
    ) -> None:
        self._submit(
            SilenceRemovalWorker(
                video_path=video_path,
                output_folder=output_folder,
                encoder=encoder,
                threshold_db=threshold_db,
                min_duration=min_duration,
                padding=padding,
                denoise=denoise or {},
                subtitle=subtitle or {},
            )
        )

    def _on_cancellation_requested(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()

    # ------------------------------------------------------------------
    def run(self) -> int:
        self.window.show()
        return self.app.exec()


if __name__ == "__main__":
    application = Application()
    sys.exit(application.run())

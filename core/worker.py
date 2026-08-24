"""
CompilationWorker / SilenceRemovalWorker — QThread-based background workers
that drive the Smart Video Compiler pipelines.

CompilationWorker pipeline
--------------------------
1. Expand and scan sources (files and/or folders)      ( 0-10 %)
2. Read full duration of each video                    (10-50 %)
3. Plan random non-overlapping clips                   (50-55 %)
4. Cut individual clips from source videos             (55-85 %)
5. Concatenate clips into final video                  (85-95 %)
6. Clean up temporary files                            (95-100 %)

SilenceRemovalWorker pipeline
-----------------------------
1. Validate the input media file                       ( 0- 5 %)
2. Detect silence                                      ( 5-50 %)
3. Cut non-silent segments                             (50-80 %)
4. Concatenate segments                                (80-95 %)
5. Clean up temporary files                            (95-100 %)
"""

import os
import tempfile
import traceback
from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.ffmpeg_engine import (
    FFmpegEngine,
    compiler_output_name,
    map_encoder_display,
    make_timestamp,
    silence_output_name,
)

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv"})


class CompilationWorker(QThread):
    """Runs the video compilation pipeline on a background thread."""

    # -- Signals ---------------------------------------------------------
    log_message = Signal(str)
    phase_message = Signal(str)
    progress_percent = Signal(int)
    encoding_complete = Signal(str)   # final output path
    error_occurred = Signal(str)

    # ------------------------------------------------------------------
    def __init__(
        self,
        sources: list[tuple[str, str]],
        output_folder: str,
        total_duration: int,
        clip_duration: int,
        encoder: str,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.sources = list(sources)
        self.output_folder = output_folder
        self.total_duration = float(total_duration)
        self.clip_duration = float(clip_duration)
        self.encoder = encoder

        self.engine = FFmpegEngine()
        self.engine.log_callback = self.log_message.emit

        self._temp_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_video_file(path: str) -> bool:
        """Return ``True`` if *path* has a recognised video extension."""
        _, ext = os.path.splitext(path)
        return ext.lower() in VIDEO_EXTENSIONS

    def _expand_sources(self) -> list[str]:
        """Expand mixed file/folder sources into a deduplicated video list."""
        expanded: list[str] = []
        seen: set[str] = set()

        num_folders = sum(1 for kind, _ in self.sources if kind == "dir")
        num_files = len(self.sources) - num_folders
        self.phase_message.emit("Scanning sources...")
        self.log_message.emit(
            f"Scanning {len(self.sources)} source(s): "
            f"{num_folders} folder(s), {num_files} file(s)..."
        )

        for kind, path in self.sources:
            if kind == "dir":
                try:
                    entries = [os.path.join(path, name) for name in os.listdir(path)]
                except OSError as exc:
                    self.log_message.emit(f"Warning: cannot read folder \"{path}\": {exc}")
                    continue
            else:
                entries = [path]

            for entry in entries:
                if not self._is_video_file(entry):
                    continue
                absolute = os.path.abspath(entry)
                if absolute in seen:
                    continue
                seen.add(absolute)
                expanded.append(absolute)

        return expanded

    def _resolve_output_path(self) -> str:
        """
        Return a timestamped, non-colliding output path in the output folder.

        Pattern: ``compiled_video_<YYYY-mm-dd_HHMMSS>.mp4`` with a numeric
        suffix appended when a job finishes within the same second.
        """
        os.makedirs(self.output_folder, exist_ok=True)
        stamp = make_timestamp()
        base_name = compiler_output_name(stamp)
        candidate = os.path.join(self.output_folder, f"{base_name}.mp4")

        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(
                self.output_folder, f"{base_name}_{counter}.mp4"
            )
            counter += 1
        return candidate

    # ------------------------------------------------------------------
    # Pipeline  (called from QThread.run)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main entry-point — runs the full compilation pipeline."""
        self._temp_dir = None

        try:
            self._pipeline()
        except Exception as exc:
            tb = traceback.format_exc()
            self.log_message.emit(f"Unexpected error: {exc}\n{tb}")
            self.error_occurred.emit(str(exc))
        finally:
            self._cleanup_temp_dir()

    def _pipeline(self) -> None:
        # --------------------------------------------------------------
        # 1. Scan phase (0-10 %)
        # --------------------------------------------------------------
        self.progress_percent.emit(0)

        video_files = self._expand_sources()

        if not video_files:
            msg = (
                "No video files found in the selected sources. "
                f"Supported extensions: {', '.join(sorted(VIDEO_EXTENSIONS))}"
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(f"Found {len(video_files)} unique video file(s).")
        self.progress_percent.emit(10)

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # --------------------------------------------------------------
        # 2. Duration phase (10-50 %)
        # --------------------------------------------------------------
        self.phase_message.emit("Analyzing videos...")
        videos_segments: dict[str, list[tuple[float, float]]] = {}
        total_videos = len(video_files)

        for idx, video_path in enumerate(video_files):
            filename = os.path.basename(video_path)
            duration = self.engine.get_video_duration(video_path)
            if duration > 1.0:
                videos_segments[video_path] = [(0.0, duration)]
                self.log_message.emit(f"Full duration of \"{filename}\": {duration:.1f}s")
            else:
                videos_segments[video_path] = []
                self.log_message.emit(
                    f"Warning: \"{filename}\" too short ({duration:.1f}s), skipping."
                )

            progress = 10 + int((idx + 1) / total_videos * 40)
            self.progress_percent.emit(progress)

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # --------------------------------------------------------------
        # 3. Clip planning phase (50-55 %)
        # --------------------------------------------------------------
        self.phase_message.emit("Planning random clips...")
        self.progress_percent.emit(50)
        self.log_message.emit("Planning random clips...")

        self._temp_dir = tempfile.mkdtemp(prefix="svc_")

        planned_clips = self.engine.extract_random_clips(
            videos_segments=videos_segments,
            clip_duration=self.clip_duration,
            total_target=self.total_duration,
            temp_dir=self._temp_dir,
            log_callback=None,
        )

        if not planned_clips:
            msg = (
                "Could not plan any clips from the selected sources. "
                "The video(s) may be too short."
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        total_planned = len(planned_clips)
        source_videos = len({c["video"] for c in planned_clips})
        self.log_message.emit(
            f"Planned {total_planned} clip(s) from {source_videos} video(s)."
        )
        self.progress_percent.emit(55)

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # --------------------------------------------------------------
        # 4. Clip cutting phase (55-85 %)
        # --------------------------------------------------------------
        extracted_paths: list[str] = []

        for idx, clip in enumerate(planned_clips):
            if self.isInterruptionRequested():
                self._emit_cancelled()
                return

            clip_number = idx + 1
            filename = os.path.basename(clip["video"])
            start_s = clip["start"]
            duration_s = clip["duration"]

            self.phase_message.emit(
                f"Cutting clips ({clip_number}/{total_planned})..."
            )
            self.log_message.emit(
                f"Extracting clip [{clip_number}/{total_planned}]: "
                f"\"{filename}\" {start_s:.1f}s-{start_s + duration_s:.1f}s"
            )

            success = self.engine.cut_clip(
                video_path=clip["video"],
                start=start_s,
                duration=duration_s,
                output_path=clip["output"],
            )

            if success:
                extracted_paths.append(clip["output"])
            else:
                self.log_message.emit(
                    f"Warning: failed to extract clip [{clip_number}/{total_planned}], skipping."
                )

            progress = 55 + int((idx + 1) / total_planned * 30)
            self.progress_percent.emit(progress)

        if not extracted_paths:
            msg = "No clips were successfully extracted. Aborting."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        # --------------------------------------------------------------
        # 5. Concatenation phase (85-95 %)
        # --------------------------------------------------------------
        self.phase_message.emit("Concatenating & encoding...")
        self.progress_percent.emit(85)

        output_path = self._resolve_output_path()
        self.log_message.emit(f"Encoding final video with \"{self.encoder}\" encoder...")

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        concat_ok = self.engine.concat_clips(
            clip_paths=extracted_paths,
            output_path=output_path,
            encoder=map_encoder_display(self.encoder),
        )

        if not concat_ok:
            msg = "Final video concatenation / encoding failed."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(f"Successfully created \"{output_path}\"")
        self.progress_percent.emit(95)

        # --------------------------------------------------------------
        # 6. Cleanup (95-100 %)
        # --------------------------------------------------------------
        self.phase_message.emit("Cleaning up...")
        self._cleanup_temp_dir()
        self.progress_percent.emit(100)
        self.encoding_complete.emit(output_path)

    # ------------------------------------------------------------------
    def _emit_cancelled(self) -> None:
        self.log_message.emit("Cancelled by user.")
        self.error_occurred.emit("Cancelled by user.")

    def _cleanup_temp_dir(self) -> None:
        """Remove the temporary directory if it exists."""
        if self._temp_dir and os.path.isdir(self._temp_dir):
            self.engine.cleanup_temp(self._temp_dir)
            self._temp_dir = None


class SilenceRemovalWorker(QThread):
    """Runs the silence-removal pipeline on a background thread.

    Takes a single media file, detects silent regions, cuts out all
    non-silent segments, and concatenates them into a single output file.
    """

    log_message = Signal(str)
    phase_message = Signal(str)
    progress_percent = Signal(int)
    encoding_complete = Signal(str)
    error_occurred = Signal(str)

    # ------------------------------------------------------------------
    def __init__(
        self,
        video_path: str,
        output_folder: str,
        encoder: str,
        threshold_db: int = -30,
        min_duration: float = 0.5,
        padding: float = 0.0,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.video_path = video_path
        self.output_folder = output_folder
        self.encoder = encoder
        self.threshold_db = threshold_db
        self.min_duration = min_duration
        self.padding = padding

        self.engine = FFmpegEngine()
        self.engine.log_callback = self.log_message.emit
        self._temp_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_output_path(self, audio_only: bool) -> str:
        """Timestamped, non-colliding output path for this run."""
        os.makedirs(self.output_folder, exist_ok=True)
        extension = ".m4a" if audio_only else ".mp4"
        base_stem = os.path.splitext(os.path.basename(self.video_path))[0]
        stamp = make_timestamp()
        base_name = silence_output_name(base_stem, extension, stamp)
        candidate = os.path.join(self.output_folder, base_name)

        counter = 1
        while os.path.exists(candidate):
            stem_no_ext = base_name[: -len(extension)]
            candidate = os.path.join(
                self.output_folder, f"{stem_no_ext}_{counter}{extension}"
            )
            counter += 1
        return candidate

    def _cleanup_temp_dir(self) -> None:
        if self._temp_dir and os.path.isdir(self._temp_dir):
            self.engine.cleanup_temp(self._temp_dir)
            self._temp_dir = None

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._temp_dir = None
        try:
            self._pipeline()
        except Exception as exc:
            tb = traceback.format_exc()
            self.log_message.emit(f"Unexpected error: {exc}\n{tb}")
            self.error_occurred.emit(str(exc))
        finally:
            self._cleanup_temp_dir()

    def _pipeline(self) -> None:
        # 1. Validate phase (0-5 %) --------------------------------------
        self.progress_percent.emit(0)
        self.phase_message.emit("Validating input file...")
        self.log_message.emit("Validating input media file...")

        if not os.path.isfile(self.video_path):
            msg = f"Media file not found: \"{self.video_path}\""
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        filename = os.path.basename(self.video_path)
        self.log_message.emit(f"Input: \"{filename}\"")
        self.progress_percent.emit(5)

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # 2. Silence detection (5-50 %) ----------------------------------
        self.phase_message.emit("Analyzing audio...")
        self.log_message.emit(f"Analyzing \"{filename}\"...")

        segments = self.engine.detect_silence(
            self.video_path,
            noise_threshold=float(self.threshold_db),
            min_duration=self.min_duration,
            padding=self.padding,
        )
        self.log_message.emit(
            f"Found {len(segments)} non-silent segment(s) in \"{filename}\""
        )
        self.progress_percent.emit(50)

        audio_only = self.engine.is_audio_only(self.video_path)
        self.log_message.emit(f"Input type: {'audio-only' if audio_only else 'video'}")

        if not segments:
            msg = (
                f"No non-silent segments found in \"{filename}\". "
                "The file may be silent or too short."
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # 3. Cut segments (50-80 %) --------------------------------------
        self._temp_dir = tempfile.mkdtemp(prefix="svc_silence_")
        os.makedirs(self.output_folder, exist_ok=True)

        clip_paths: list[str] = []
        num_segments = len(segments)

        for seg_idx, (seg_start, seg_end) in enumerate(segments):
            if self.isInterruptionRequested():
                self._emit_cancelled()
                return

            clip_path = os.path.join(self._temp_dir, f"seg_{seg_idx:03d}.mkv")
            self.phase_message.emit(
                f"Cutting segments ({seg_idx + 1}/{num_segments})..."
            )
            self.log_message.emit(
                f"Cutting segment {seg_idx + 1}/{num_segments}: "
                f"{seg_start:.1f}s–{seg_end:.1f}s"
            )

            success = self.engine.cut_clip(
                video_path=self.video_path,
                start=seg_start,
                duration=seg_end - seg_start,
                output_path=clip_path,
            )

            if success:
                clip_paths.append(clip_path)
            else:
                self.log_message.emit(
                    f"Warning: failed to cut segment {seg_idx + 1}, skipping."
                )

            progress = 50 + int((seg_idx + 1) / num_segments * 30)
            self.progress_percent.emit(progress)

        if not clip_paths:
            msg = "No segments were successfully extracted. Aborting."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # 4. Concat (80-95 %) --------------------------------------------
        self.phase_message.emit("Concatenating & encoding...")
        self.progress_percent.emit(80)

        out_path = self._resolve_output_path(audio_only)
        self.log_message.emit(f"Encoding \"{os.path.basename(out_path)}\"...")

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        if audio_only:
            concat_ok = self.engine.concat_audio_clips(
                clip_paths=clip_paths,
                output_path=out_path,
            )
        else:
            concat_ok = self.engine.concat_clips(
                clip_paths=clip_paths,
                output_path=out_path,
                encoder=map_encoder_display(self.encoder),
            )

        if not concat_ok:
            msg = "Final concatenation / encoding failed."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(f"Saved: \"{os.path.basename(out_path)}\"")
        self.progress_percent.emit(95)

        # 5. Cleanup (95-100 %) ------------------------------------------
        self.phase_message.emit("Cleaning up...")
        self._cleanup_temp_dir()
        self.progress_percent.emit(100)

        summary = f"Silence removal complete — \"{filename}\" processed."
        self.log_message.emit(summary)
        self.encoding_complete.emit(out_path)

    # ------------------------------------------------------------------
    def _emit_cancelled(self) -> None:
        self.log_message.emit("Cancelled by user.")
        self.error_occurred.emit("Cancelled by user.")

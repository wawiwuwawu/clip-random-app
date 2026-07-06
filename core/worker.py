"""
CompilationWorker — QThread-based background worker that drives the full
Smart Video Compiler pipeline.

Pipeline steps
--------------
1. Scan input folder for supported video files        ( 0-10 %)
2. Detect silence in each video                        (10-50 %)
3. Plan random non-overlapping clips                   (50-55 %)
4. Cut individual clips from source videos             (55-85 %)
5. Concatenate clips into final video                  (85-95 %)
6. Clean up temporary files                            (95-100 %)
"""

import glob
import os
import tempfile
import traceback
from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.ffmpeg_engine import FFmpegEngine


class CompilationWorker(QThread):
    """Runs the video compilation pipeline on a background thread."""

    # -- Signals ---------------------------------------------------------
    log_message = Signal(str)
    progress_percent = Signal(int)
    encoding_complete = Signal(str)   # final output path
    error_occurred = Signal(str)

    # -- Supported video extensions --------------------------------------
    VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv"})

    # ------------------------------------------------------------------
    def __init__(
        self,
        input_folder: str,
        output_folder: str,
        total_duration: int,
        clip_duration: int,
        encoder: str,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.input_folder = input_folder
        self.output_folder = output_folder
        self.total_duration = float(total_duration)
        self.clip_duration = float(clip_duration)
        self.encoder = encoder

        # Create the engine and wire logging
        self.engine = FFmpegEngine()
        self.engine.log_callback = self.log_message.emit

        # Temporary directory — created during run()
        self._temp_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_video_file(self, path: str) -> bool:
        """Return ``True`` if *path* has a recognised video extension."""
        _, ext = os.path.splitext(path)
        return ext.lower() in self.VIDEO_EXTENSIONS

    @staticmethod
    def _map_encoder(display_name: str) -> str:
        """Map the UI combo-box display name to the short encoder code."""
        mapping = {
            "NVIDIA NVENC (h264_nvenc)": "nvenc",
            "Intel QSV (h264_qsv)": "qsv",
            "AMD AMF (h264_amf)": "amf",
            "CPU Software (libx264)": "cpu",
        }
        return mapping.get(display_name, "cpu")

    def _resolve_output_path(self) -> str:
        """
        Return a non-colliding output path in ``self.output_folder``.

        If ``compiled_video.mp4`` already exists, suffixes ``_1``, ``_2``, …
        are tried.
        """
        os.makedirs(self.output_folder, exist_ok=True)
        base = os.path.join(self.output_folder, "compiled_video")
        if not os.path.isfile(f"{base}.mp4"):
            return f"{base}.mp4"
        counter = 1
        while os.path.isfile(f"{base}_{counter}.mp4"):
            counter += 1
        return f"{base}_{counter}.mp4"

    # ------------------------------------------------------------------
    # Pipeline  (called from QThread.run)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main entry-point — runs the full compilation pipeline."""
        self._temp_dir = None  # will be set once we know we need it

        try:
            self._pipeline()
        except Exception as exc:
            tb = traceback.format_exc()
            self.log_message.emit(f"Unexpected error: {exc}\n{tb}")
            self.error_occurred.emit(str(exc))
        finally:
            # Ensure temp directory is cleaned up even on failure
            self._cleanup_temp_dir()

    def _pipeline(self) -> None:
        """Internal pipeline implementation."""
        # --------------------------------------------------------------
        # 1. Scan phase (0-10 %)
        # --------------------------------------------------------------
        self.progress_percent.emit(0)
        self.log_message.emit("Scanning input folder for video files...")

        all_files = [os.path.join(self.input_folder, f) for f in os.listdir(self.input_folder)]
        video_files = [f for f in all_files if self._is_video_file(f)]

        if not video_files:
            msg = (
                f"No video files found in \"{self.input_folder}\". "
                f"Supported extensions: {', '.join(sorted(self.VIDEO_EXTENSIONS))}"
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(f"Found {len(video_files)} video file(s).")
        self.progress_percent.emit(10)

        if self.isInterruptionRequested():
            self.log_message.emit("Cancelled by user.")
            self.error_occurred.emit("Cancelled by user.")
            return

        # --------------------------------------------------------------
        # 2. Silence detection phase (10-50 %)
        # --------------------------------------------------------------
        videos_segments: dict[str, list[tuple[float, float]]] = {}

        total_videos = len(video_files)

        self.log_message.emit(
            "Using full video duration for compilation..."
        )
        for idx, video_path in enumerate(video_files):
            filename = os.path.basename(video_path)
            duration = self.engine.get_video_duration(video_path)
            if duration > 1.0:
                videos_segments[video_path] = [(0.0, duration)]
                self.log_message.emit(
                    f"Full duration of \"{filename}\": {duration:.1f}s"
                )
            else:
                videos_segments[video_path] = []
                self.log_message.emit(
                    f"Warning: \"{filename}\" too short ({duration:.1f}s), skipping."
                )

            # Progress: 10 % → 50 % across all videos
            progress = 10 + int((idx + 1) / total_videos * 40)
            self.progress_percent.emit(progress)

        if self.isInterruptionRequested():
            self.log_message.emit("Cancelled by user.")
            self.error_occurred.emit("Cancelled by user.")
            return

        # --------------------------------------------------------------
        # 3. Clip planning phase (50-55 %)
        # --------------------------------------------------------------
        self.progress_percent.emit(50)
        self.log_message.emit("Planning random clips...")

        # Create temp dir now — needed for generated clip paths
        self._temp_dir = tempfile.mkdtemp(prefix="svc_")

        planned_clips = self.engine.extract_random_clips(
            videos_segments=videos_segments,
            clip_duration=self.clip_duration,
            total_target=self.total_duration,
            temp_dir=self._temp_dir,
            log_callback=self.log_message.emit,
        )

        if not planned_clips:
            msg = (
                "Could not plan any clips from the detected non-silent segments. "
                "The video(s) may be too short or contain no non-silent content."
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        total_planned = len(planned_clips)
        # Count distinct source videos used
        source_videos = len({c["video"] for c in planned_clips})
        self.log_message.emit(
            f"Planned {total_planned} clip(s) from {source_videos} video(s)."
        )
        self.progress_percent.emit(55)

        # --------------------------------------------------------------
        # 4. Clip cutting phase (55-85 %)
        # --------------------------------------------------------------
        extracted_paths: list[str] = []

        for idx, clip in enumerate(planned_clips):
            if self.isInterruptionRequested():
                self.log_message.emit("Cancelled by user.")
                self.error_occurred.emit("Cancelled by user.")
                return

            clip_number = idx + 1
            filename = os.path.basename(clip["video"])
            start_s = clip["start"]
            duration_s = clip["duration"]

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
                    f"Warning: failed to extract clip [{clip_number}/{total_planned}], "
                    f"skipping."
                )

            # Progress: 55 % → 85 %
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
        self.progress_percent.emit(85)
        output_path = self._resolve_output_path()
        self.log_message.emit(
            f"Encoding final video with \"{self.encoder}\" encoder..."
        )

        if self.isInterruptionRequested():
            self.log_message.emit("Cancelled by user.")
            self.error_occurred.emit("Cancelled by user.")
            return

        concat_ok = self.engine.concat_clips(
            clip_paths=extracted_paths,
            output_path=output_path,
            encoder=self._map_encoder(self.encoder),
        )

        if not concat_ok:
            msg = "Final video concatenation / encoding failed."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(
            f"Successfully created \"{output_path}\""
        )
        self.progress_percent.emit(95)

        # --------------------------------------------------------------
        # 6. Cleanup (95-100 %)
        # --------------------------------------------------------------
        self._cleanup_temp_dir()
        self.progress_percent.emit(100)
        self.encoding_complete.emit(output_path)

    # ------------------------------------------------------------------
    def _cleanup_temp_dir(self) -> None:
        """Remove the temporary directory if it exists."""
        if self._temp_dir and os.path.isdir(self._temp_dir):
            self.engine.cleanup_temp(self._temp_dir)
            self._temp_dir = None


class SilenceRemovalWorker(QThread):
    """Runs the silence-removal pipeline on a background thread.

    Takes a single video file, detects silent regions, cuts out all non-silent
    segments, and concatenates them into a single output file with silence
    removed.
    """

    log_message = Signal(str)
    progress_percent = Signal(int)
    encoding_complete = Signal(str)   # summary message
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

    @staticmethod
    def _map_encoder(display_name: str) -> str:
        mapping = {
            "NVIDIA NVENC (h264_nvenc)": "nvenc",
            "Intel QSV (h264_qsv)": "qsv",
            "AMD AMF (h264_amf)": "amf",
            "CPU Software (libx264)": "cpu",
        }
        return mapping.get(display_name, "cpu")

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
        self.log_message.emit("Validating input video file...")

        if not os.path.isfile(self.video_path):
            msg = f"Video file not found: \"{self.video_path}\""
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        filename = os.path.basename(self.video_path)
        self.log_message.emit(f"Input: \"{filename}\"")
        self.progress_percent.emit(5)

        if self.isInterruptionRequested():
            self.log_message.emit("Cancelled by user.")
            self.error_occurred.emit("Cancelled by user.")
            return

        # 2. Silence detection (5-50 %) ----------------------------------
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
                "The video may be silent or too short."
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        if self.isInterruptionRequested():
            self.log_message.emit("Cancelled by user.")
            self.error_occurred.emit("Cancelled by user.")
            return

        # 3. Cut segments (50-80 %) --------------------------------------
        self._temp_dir = tempfile.mkdtemp(prefix="svc_silence_")
        os.makedirs(self.output_folder, exist_ok=True)

        clip_paths: list[str] = []
        num_segments = len(segments)

        for seg_idx, (seg_start, seg_end) in enumerate(segments):
            if self.isInterruptionRequested():
                self.log_message.emit("Cancelled by user.")
                self.error_occurred.emit("Cancelled by user.")
                return

            clip_path = os.path.join(
                self._temp_dir,
                f"seg_{seg_idx:03d}.mkv",
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

            # Progress: 50 % → 80 % across all segments
            progress = 50 + int((seg_idx + 1) / num_segments * 30)
            self.progress_percent.emit(progress)

        if not clip_paths:
            msg = "No segments were successfully extracted. Aborting."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        if self.isInterruptionRequested():
            self.log_message.emit("Cancelled by user.")
            self.error_occurred.emit("Cancelled by user.")
            return

        # 4. Concat (80-95 %) --------------------------------------------
        self.progress_percent.emit(80)

        base_name = os.path.splitext(os.path.basename(self.video_path))[0]
        out_ext = ".m4a" if audio_only else ".mp4"
        out_path = os.path.join(self.output_folder, f"{base_name}_cleaned{out_ext}")

        # Avoid overwriting
        counter = 1
        while os.path.isfile(out_path):
            out_path = os.path.join(
                self.output_folder,
                f"{base_name}_cleaned_{counter}{out_ext}",
            )
            counter += 1

        self.log_message.emit(
            f"Encoding \"{os.path.basename(out_path)}\"..."
        )

        if self.isInterruptionRequested():
            self.log_message.emit("Cancelled by user.")
            self.error_occurred.emit("Cancelled by user.")
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
                encoder=self._map_encoder(self.encoder),
            )

        if not concat_ok:
            msg = "Final video concatenation / encoding failed."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(f"Saved: \"{os.path.basename(out_path)}\"")
        self.progress_percent.emit(95)

        # 5. Cleanup (95-100 %) ------------------------------------------
        self._cleanup_temp_dir()
        self.progress_percent.emit(100)

        summary = f"Silence removal complete — \"{filename}\" processed."
        self.log_message.emit(summary)
        self.encoding_complete.emit(summary)
        self._temp_dir = None

"""
Background workers for Smart Video Compiler.

Clip compilation runs in two stages so the user can review the plan:

1. ``ClipPlanningWorker``  — expand sources, read durations, plan random
   clips, generate thumbnails            (progress 0-100 %)
2. ``ClipRenderWorker``    — cut included clips, concatenate, clean up
                                         (progress 0-100 %)

``SilenceRemovalWorker`` keeps its single-stage pipeline.
"""

import os
import sys
import tempfile
import traceback
from typing import Optional

from PySide6.QtCore import QThread, Signal

from core import subtitles as subtitles_mod
from core.clip_plan import ClipEntry, ClipSession
from core.ffmpeg_engine import (
    FFmpegEngine,
    build_noise_filters,
    compiler_output_name,
    map_encoder_display,
    make_timestamp,
    silence_output_name,
)

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv"})


class ClipPlanningWorker(QThread):
    """Stage 1: scan sources, plan random clips, generate thumbnails."""

    kind = "clip-plan"

    log_message = Signal(str)
    phase_message = Signal(str)
    progress_percent = Signal(int)
    plan_ready = Signal(object)       # ClipSession
    error_occurred = Signal(str)

    # ------------------------------------------------------------------
    def __init__(
        self,
        sources: list[tuple[str, str]],
        output_folder: str,
        total_duration: int,
        clip_duration: int,
        encoder: str,
        portrait: bool = False,
        scene_cut: bool = False,
        crossfade: float = 0.0,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.sources = list(sources)
        self.output_folder = output_folder
        self.total_duration = float(total_duration)
        self.clip_duration = float(clip_duration)
        self.encoder = encoder
        self.portrait = portrait
        self.scene_cut = scene_cut
        self.crossfade = crossfade

        self.engine = FFmpegEngine()
        self.engine.log_callback = self.log_message.emit

        self._temp_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_video_file(path: str) -> bool:
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

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            session = self._pipeline()
            if session is not None:
                self.plan_ready.emit(session)
            else:
                self._cleanup_temp_dir()
        except Exception as exc:
            tb = traceback.format_exc()
            self.log_message.emit(f"Unexpected error: {exc}\n{tb}")
            self.error_occurred.emit(str(exc))
            self._cleanup_temp_dir()

    def _pipeline(self) -> Optional[ClipSession]:
        # 1. Scan (0-15 %) -------------------------------------------------
        self.progress_percent.emit(0)

        video_files = self._expand_sources()
        if not video_files:
            msg = (
                "No video files found in the selected sources. "
                f"Supported extensions: {', '.join(sorted(VIDEO_EXTENSIONS))}"
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return None

        self.log_message.emit(f"Found {len(video_files)} unique video file(s).")
        self.progress_percent.emit(15)

        if self.isInterruptionRequested():
            self._cancelled()
            return None

        # 2. Durations (+ optional scenes) (15-55 %) -----------------------
        self.phase_message.emit("Analyzing videos...")
        videos_segments: dict[str, list[tuple[float, float]]] = {}
        scene_times: dict[str, list[float]] = {}

        for idx, video_path in enumerate(video_files):
            if self.isInterruptionRequested():
                self._cancelled()
                return None

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

            if self.scene_cut and duration > 1.0:
                cuts = self.engine.detect_scene_changes(video_path)
                scene_times[video_path] = cuts
                if cuts:
                    self.log_message.emit(
                        f"{len(cuts)} scene change(s) in \"{filename}\""
                    )

            progress = 15 + int((idx + 1) / len(video_files) * 40)
            self.progress_percent.emit(progress)

        # 3. Plan clips (55-65 %) ------------------------------------------
        self.phase_message.emit("Planning random clips...")
        self.progress_percent.emit(55)

        self._temp_dir = tempfile.mkdtemp(prefix="svc_")

        selected, candidates_by_video = self.engine.plan_clips(
            videos_segments=videos_segments,
            clip_duration=self.clip_duration,
            total_target=self.total_duration,
            temp_dir=self._temp_dir,
            scene_times=scene_times,
        )

        if not selected:
            msg = (
                "Could not plan any clips from the selected sources. "
                "The video(s) may be too short."
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            self._cleanup_temp_dir()
            return None

        self.progress_percent.emit(65)

        # 4. Thumbnails (65-100 %) -----------------------------------------
        self.phase_message.emit("Generating previews...")
        session = ClipSession(
            target_total=self.total_duration,
            temp_dir=self._temp_dir,
            portrait=self.portrait,
            encoder=self.encoder,
            output_folder=self.output_folder,
            crossfade=self.crossfade,
        )
        session.candidates_by_video = candidates_by_video

        total = len(selected)
        for idx, item in enumerate(selected):
            entry = ClipEntry(
                video=item["video"],
                start=item["start"],
                duration=item["duration"],
                thumb_path=os.path.join(self._temp_dir, f"thumb_{idx:03d}.jpg"),
            )
            mid = entry.start + entry.duration / 2
            self.phase_message.emit(f"Generating previews ({idx + 1}/{total})...")
            self.engine.generate_thumbnail(
                entry.video, mid, entry.thumb_path, portrait=self.portrait
            )
            session.clips.append(entry)

            progress = 65 + int((idx + 1) / total * 35)
            self.progress_percent.emit(progress)

        self.log_message.emit(
            f"Planned {total} clip(s) — review them before rendering."
        )
        self.progress_percent.emit(100)
        return session

    # ------------------------------------------------------------------
    def _cancelled(self) -> None:
        self.log_message.emit("Cancelled by user.")
        self.error_occurred.emit("Cancelled by user.")

    def _cleanup_temp_dir(self) -> None:
        if self._temp_dir and os.path.isdir(self._temp_dir):
            self.engine.cleanup_temp(self._temp_dir)
            self._temp_dir = None


class ClipRenderWorker(QThread):
    """Stage 2: cut the reviewed clips and encode the final video."""

    kind = "clip-render"

    log_message = Signal(str)
    phase_message = Signal(str)
    progress_percent = Signal(int)
    encoding_complete = Signal(str)
    error_occurred = Signal(str)

    # ------------------------------------------------------------------
    def __init__(
        self,
        session: ClipSession,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.engine = FFmpegEngine()
        self.engine.log_callback = self.log_message.emit

    # ------------------------------------------------------------------
    def _resolve_output_path(self) -> str:
        os.makedirs(self.session.output_folder, exist_ok=True)
        stamp = make_timestamp()
        base_name = compiler_output_name(stamp)
        candidate = os.path.join(self.session.output_folder, f"{base_name}.mp4")

        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(
                self.session.output_folder, f"{base_name}_{counter}.mp4"
            )
            counter += 1
        return candidate

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._pipeline()
        except Exception as exc:
            tb = traceback.format_exc()
            self.log_message.emit(f"Unexpected error: {exc}\n{tb}")
            self.error_occurred.emit(str(exc))
        finally:
            self.engine.cleanup_temp(self.session.temp_dir)

    def _pipeline(self) -> None:
        clips = [c for c in self.session.clips if not c.excluded]
        if not clips:
            msg = "No clips were selected for rendering."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        # 1. Cut clips (0-70 %) --------------------------------------------
        extracted: list[str] = []
        durations: list[float] = []
        renumbered: dict[int, str] = {}
        render_index = 0

        for idx, clip in enumerate(clips):
            if self.isInterruptionRequested():
                self._cancelled()
                return

            render_index += 1
            filename = os.path.basename(clip.video)

            # Re-map planned output name to a contiguous numbering so the
            # concat order matches the reviewed order even after exclusions.
            new_output = renumbered.get(idx)
            if new_output is None:
                new_output = os.path.join(
                    self.session.temp_dir, f"render_{render_index:03d}.mkv"
                )
                renumbered[idx] = new_output

            self.phase_message.emit(f"Cutting clips ({render_index}/{len(clips)})...")
            self.log_message.emit(
                f"Extracting clip [{render_index}/{len(clips)}]: "
                f"\"{filename}\" {clip.start:.1f}s-{clip.end:.1f}s"
            )

            success = self.engine.cut_clip(
                video_path=clip.video,
                start=clip.start,
                duration=clip.duration,
                output_path=new_output,
            )
            if success:
                extracted.append(new_output)
                durations.append(clip.duration)
            else:
                self.log_message.emit(
                    f"Warning: failed to extract clip [{render_index}/{len(clips)}], skipping."
                )

            progress = int((idx + 1) / len(clips) * 70)
            self.progress_percent.emit(progress)

        if not extracted:
            msg = "No clips were successfully extracted. Aborting."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        # 2. Concat & encode (70-95 %) --------------------------------------
        self.phase_message.emit("Concatenating & encoding...")
        self.progress_percent.emit(70)

        output_path = self._resolve_output_path()
        self.log_message.emit(
            f"Encoding final video with \"{self.session.encoder}\" encoder..."
        )

        if self.isInterruptionRequested():
            self._cancelled()
            return

        concat_ok = self.engine.concat_clips(
            clip_paths=extracted,
            output_path=output_path,
            encoder=map_encoder_display(self.session.encoder),
            durations=durations if self.session.crossfade > 0 else None,
            aspect="portrait" if self.session.portrait else "landscape",
            crossfade=self.session.crossfade,
        )

        if not concat_ok:
            msg = "Final video concatenation / encoding failed."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(f"Successfully created \"{output_path}\"")
        self.progress_percent.emit(95)

        # 3. Cleanup (95-100 %) ---------------------------------------------
        self.phase_message.emit("Cleaning up...")
        self.progress_percent.emit(100)
        self.encoding_complete.emit(output_path)

    # ------------------------------------------------------------------
    def _cancelled(self) -> None:
        self.log_message.emit("Cancelled by user.")
        self.error_occurred.emit("Cancelled by user.")


class TranscribeWorker(QThread):
    """Standalone transcription: any media file → .srt + .txt."""

    kind = "transcribe"

    log_message = Signal(str)
    phase_message = Signal(str)
    progress_percent = Signal(int)
    encoding_complete = Signal(str)   # srt path
    error_occurred = Signal(str)

    # ------------------------------------------------------------------
    def __init__(
        self,
        media_path: str,
        output_folder: str,
        model_size: str = "small",
        language: str | None = None,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.media_path = media_path
        self.output_folder = output_folder
        self.model_size = model_size
        self.language = language

    # ------------------------------------------------------------------
    @staticmethod
    def _unique_path(path: str) -> str:
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(f"{stem}_{counter}{ext}"):
            counter += 1
        return f"{stem}_{counter}{ext}"

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._pipeline()
        except Exception as exc:
            tb = traceback.format_exc()
            self.log_message.emit(f"Unexpected error: {exc}\n{tb}")
            self.error_occurred.emit(str(exc))

    def _pipeline(self) -> None:
        # 1. Validate (0-5 %) --------------------------------------------
        self.progress_percent.emit(0)
        self.phase_message.emit("Validating input file...")
        self.log_message.emit("Validating input media file...")

        if not os.path.isfile(self.media_path):
            msg = f"Media file not found: \"{self.media_path}\""
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        filename = os.path.basename(self.media_path)
        self.log_message.emit(f"Input: \"{filename}\"")
        self.progress_percent.emit(5)

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # 2. Model prep (5-15 %, busy while downloading) ------------------
        if not subtitles_mod.is_available():
            detail = subtitles_mod.import_error_detail()
            reason = (
                "faster-whisper failed to import.\n" + (detail or "")
                if detail
                else "faster-whisper is not available in this interpreter."
            )
            self.log_message.emit(reason)
            self.error_occurred.emit(reason)
            return

        os.makedirs(self.output_folder, exist_ok=True)

        # 3. Transcribe (15-90 %) ----------------------------------------
        try:
            cues, detected = subtitles_mod.transcribe(
                self.media_path,
                model_size=self.model_size,
                language=self.language,
                cancel_check=self.isInterruptionRequested,
                log=self.log_message.emit,
                status_cb=self.phase_message.emit,
                progress_cb=lambda frac: self.progress_percent.emit(
                    -1 if frac < 0 else 15 + int(min(1.0, max(0.0, frac)) * 75)
                ),
                dl_progress_cb=lambda frac: self.progress_percent.emit(
                    5 + int(min(1.0, max(0.0, frac)) * 10)
                ),
            )
        except Exception as exc:
            msg = f"Transcription failed: {exc}"
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        if not cues:
            msg = (
                f"No speech detected in \"{filename}\". "
                "Try a different Whisper model or check the audio."
            )
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(
            f"{len(cues)} cue(s) transcribed (language={detected or '?'})"
        )

        # 4. Write outputs (90-95 %) -------------------------------------
        self.phase_message.emit("Writing subtitle files...")
        self.progress_percent.emit(90)

        stem = os.path.splitext(filename)[0]
        srt_path = self._unique_path(os.path.join(self.output_folder, stem + ".srt"))
        txt_path = self._unique_path(os.path.join(self.output_folder, stem + ".txt"))

        subtitles_mod.write_srt(cues, srt_path)
        subtitles_mod.write_txt(cues, txt_path)

        self.log_message.emit(f"Saved: \"{os.path.basename(srt_path)}\"")
        self.log_message.emit(f"Saved: \"{os.path.basename(txt_path)}\"")
        self.progress_percent.emit(95)

        # 5. Done (95-100 %) ----------------------------------------------
        self.phase_message.emit("Done.")
        self.progress_percent.emit(100)
        self.encoding_complete.emit(srt_path)

    # ------------------------------------------------------------------
    def _emit_cancelled(self) -> None:
        self.log_message.emit("Cancelled by user.")
        self.error_occurred.emit("Cancelled by user.")


class SilenceRemovalWorker(QThread):
    """Runs the silence-removal pipeline on a background thread.

    Takes a single media file, detects silent regions, cuts out all
    non-silent segments, and concatenates them into a single output file.
    """

    kind = "silence"

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
        denoise: Optional[dict] = None,
        subtitle: Optional[dict] = None,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.video_path = video_path
        self.output_folder = output_folder
        self.encoder = encoder
        self.threshold_db = threshold_db
        self.min_duration = min_duration
        self.padding = padding
        self.denoise = denoise or {}
        self.subtitle = subtitle or {}
        self.subtitle_result: Optional[dict] = None

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
        self.progress_percent.emit(40)

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

            progress = 40 + int((seg_idx + 1) / num_segments * 22)
            self.progress_percent.emit(progress)

        if not clip_paths:
            msg = "No segments were successfully extracted. Aborting."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        # 4. Concat (62-70 %) --------------------------------------------
        self.phase_message.emit("Concatenating & encoding...")
        self.progress_percent.emit(62)

        audio_filters = None
        if self.denoise.get("enabled"):
            mode = self.denoise.get("mode", "fft")
            strength = self.denoise.get("strength", "medium")
            noise_floor = None
            if mode != "ai":
                noise_floor = self.engine.measure_noise_floor(
                    self.video_path, segments
                )
                if noise_floor is not None:
                    nf_preview = max(-55.0, min(-22.0, noise_floor + 5.0))
                    self.log_message.emit(
                        f"Measured noise floor: {noise_floor:.1f} dB "
                        f"\u2192 denoise floor set to {nf_preview:.1f} dB"
                    )
                else:
                    self.log_message.emit(
                        "No silent gap long enough to measure \u2014 "
                        "using default denoise preset."
                    )
            audio_filters = build_noise_filters(mode, strength, noise_floor)
            self.log_message.emit(
                f"Applying noise removal (mode={mode}"
                + (f", strength={strength}" if mode != "ai" else "")
                + ")..."
            )

        out_path = self._resolve_output_path(audio_only)
        self.log_message.emit(f"Encoding \"{os.path.basename(out_path)}\"...")

        if self.isInterruptionRequested():
            self._emit_cancelled()
            return

        if audio_only:
            concat_ok = self.engine.concat_audio_clips(
                clip_paths=clip_paths,
                output_path=out_path,
                audio_filters=audio_filters,
            )
        else:
            concat_ok = self.engine.concat_clips(
                clip_paths=clip_paths,
                output_path=out_path,
                encoder=map_encoder_display(self.encoder),
                audio_filters=audio_filters,
            )

        if not concat_ok:
            msg = "Final concatenation / encoding failed."
            self.log_message.emit(msg)
            self.error_occurred.emit(msg)
            return

        self.log_message.emit(f"Saved: \"{os.path.basename(out_path)}\"")
        self.progress_percent.emit(70)

        # 5. Subtitles (70-92 %) -----------------------------------------
        if self.subtitle.get("enabled"):
            self._generate_subtitles(out_path)

        # 6. Cleanup (92-100 %) ------------------------------------------
        self.phase_message.emit("Cleaning up...")
        self._cleanup_temp_dir()
        self.progress_percent.emit(100)

        summary = f"Silence removal complete — \"{filename}\" processed."
        self.log_message.emit(summary)
        self.encoding_complete.emit(out_path)

    # ------------------------------------------------------------------
    def _generate_subtitles(self, output_path: str) -> dict:
        """Transcribe the FINAL output file so SRT timing matches it."""
        result = {"ok": False, "reason": "", "srt": ""}

        if not subtitles_mod.is_available():
            detail = subtitles_mod.import_error_detail()
            reason = (
                "faster-whisper failed to import.\n" + (detail or "")
                if detail
                else "faster-whisper is not available in this interpreter."
            )
            self.log_message.emit("Subtitles skipped — see completion dialog.")
            result["reason"] = reason
            self.subtitle_result = result
            return result

        srt_path = os.path.splitext(output_path)[0] + ".srt"
        try:
            count, detected = subtitles_mod.transcribe_to_srt(
                output_path,
                srt_path,
                model_size=self.subtitle.get("model", "small"),
                language=self.subtitle.get("language"),
                cancel_check=self.isInterruptionRequested,
                log=self.log_message.emit,
                status_cb=self.phase_message.emit,
                progress_cb=lambda frac: self.progress_percent.emit(
                    -1 if frac < 0 else 84 + int(min(1.0, max(0.0, frac)) * 8)
                ),
                dl_progress_cb=lambda frac: self.progress_percent.emit(
                    70 + int(min(1.0, max(0.0, frac)) * 8)
                ),
            )
            self.log_message.emit(
                f"Subtitles saved: \"{os.path.basename(srt_path)}\" "
                f"({count} cue(s), language={detected or '?'})"
            )
            result.update(ok=True, srt=srt_path)
        except Exception as exc:
            reason = str(exc)
            self.log_message.emit(f"Subtitle generation failed: {reason}")
            result["reason"] = reason

        self.subtitle_result = result
        return result

    # ------------------------------------------------------------------
    def _emit_cancelled(self) -> None:
        self.log_message.emit("Cancelled by user.")
        self.error_occurred.emit("Cancelled by user.")

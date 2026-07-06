"""
FFmpegEngine — Encapsulates all FFmpeg/FFprobe operations for the Smart Video Compiler.

All calls use ``subprocess.run()`` directly against the ``ffmpeg`` / ``ffprobe``
binaries.  At development time they are resolved from PATH; when the app is
frozen with PyInstaller they are resolved from ``sys._MEIPASS`` or the
executable directory (``--onedir`` mode).  No third-party wrappers
(e.g. ffmpeg-python) are used.
"""

import os
import re
import random
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional


class FFmpegEngine:
    """Stateless engine that wraps common FFmpeg workflows."""

    def __init__(self) -> None:
        # External callers may replace this with a logging function.
        # Signature: log_callback(message: str) -> None
        self.log_callback: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ffmpeg_bin(name: str) -> str:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
            candidate = os.path.join(base, f"{name}.exe")
            if os.path.isfile(candidate):
                return candidate
        if hasattr(sys, "_MEIPASS"):
            candidate = os.path.join(sys._MEIPASS, f"{name}.exe")
            if os.path.isfile(candidate):
                return candidate
        return name

    def _log(self, message: str) -> None:
        """Emit a log message through the external callback, if set."""
        if self.log_callback is not None:
            self.log_callback(message)

    @staticmethod
    def _run(cmd: list[str], description: str = "") -> subprocess.CompletedProcess:
        """Thin wrapper around ``subprocess.run`` with logging."""
        # Build a human-friendly command string for logging
        cmd_str = " ".join(str(c) for c in cmd)
        if description:
            full_desc = f"[{description}] {cmd_str}"
        else:
            full_desc = cmd_str
        # (The engine itself does not log here unless the caller's callback is set)
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.CalledProcessError as exc:
            raise  # let callers handle

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_silence(
        self,
        video_path: str,
        noise_threshold: float = -30,
        min_duration: float = 0.5,
        padding: float = 0.0,
    ) -> list[tuple[float, float]]:
        """
        Analyse the audio stream of *video_path* with FFmpeg's ``silencedetect``
        filter and return a list of NON-SILENT segments.

        Parameters
        ----------
        video_path : str
            Absolute or relative path to a video file.
        noise_threshold : float
            Noise threshold in dB for silence detection (default: ``-30``).
        min_duration : float
            Minimum duration of silence in seconds (default: ``0.5``).
        padding : float
            Extra time in seconds to add around each non-silent segment.
            Subtracted from the start (clamped to 0) and added to the end
            (clamped to video duration).  Default ``0.0``.

        Returns
        -------
        list[tuple[float, float]]
            Sorted list of ``(start_seconds, end_seconds)`` tuples representing
            segments that contain audio (non-silence).  Each segment is at least
            1 second long.
        """
        filter_str = f"silencedetect=noise={noise_threshold}dB:d={min_duration}"
        cmd = [
            self._ffmpeg_bin("ffmpeg"),
            "-y",
            "-i", video_path,
            "-af", filter_str,
            "-f", "null",
            "-",
        ]
        self._log(f"Running silence detection: ffmpeg -y -i \"{video_path}\" -af "
                   f"\"{filter_str}\" -f null -")

        try:
            result = self._run(cmd, description="silencedetect")
        except subprocess.CalledProcessError as exc:
            self._log(f"Silence detection failed for \"{video_path}\": {exc}")
            return []
        except FileNotFoundError:
            self._log("ffmpeg binary not found on PATH — is FFmpeg installed?")
            return []

        stderr = result.stderr  # silencedetect writes to stderr

        # Parse silence_start / silence_end timestamps
        start_pattern = re.compile(r"silence_start:\s*([\d.]+)")
        end_pattern = re.compile(r"silence_end:\s*([\d.]+)")

        starts = [float(m.group(1)) for m in start_pattern.finditer(stderr)]
        ends = [float(m.group(1)) for m in end_pattern.finditer(stderr)]

        if not starts and not ends:
            # No silence at all — entire video is non-silent
            duration = self.get_video_duration(video_path)
            if duration > 0:
                return [(0.0, duration)]
            return []

        # Build non-silent segments from the silence events
        non_silent: list[tuple[float, float]] = []

        # Determine the full video duration (used for the tail segment)
        video_duration = self.get_video_duration(video_path)
        if video_duration <= 0:
            self._log(f"Could not determine duration for \"{video_path}\"")
            return []

        # Interleave the two event arrays by walking through them together.
        # We keep two pointers: next silence_start, next silence_end.
        i, j = 0, 0
        current_time = 0.0

        while i < len(starts) or j < len(ends):
            # Next silence_start (if any)
            next_start = starts[i] if i < len(starts) else float("inf")
            # Next silence_end (if any)
            next_end = ends[j] if j < len(ends) else float("inf")

            if next_start < next_end:
                # A silence period begins at next_start — the region from
                # current_time to next_start is non-silent.
                if next_start > current_time + 1.0:
                    non_silent.append((current_time, next_start))
                current_time = next_start
                i += 1
            else:
                # A silence period ends at next_end — the region from
                # current_time to next_end is silent, so we skip ahead.
                current_time = next_end
                j += 1

        # Tail segment: from the last timestamp to end of video
        if current_time < video_duration - 1.0:
            non_silent.append((current_time, video_duration))

        # Merge adjacent / overlapping segments (belt-and-suspenders)
        merged: list[tuple[float, float]] = []
        for seg in sorted(non_silent, key=lambda x: x[0]):
            if not merged:
                merged.append(seg)
                continue
            last_start, last_end = merged[-1]
            seg_start, seg_end = seg
            if seg_start <= last_end:
                # Overlap / touch — merge
                merged[-1] = (last_start, max(last_end, seg_end))
            else:
                merged.append(seg)

        # Filter out any remaining sub-1-second segments
        merged = [(s, e) for s, e in merged if e - s >= 1.0]

        # Apply padding around each non-silent segment
        if padding > 0.0:
            padded: list[tuple[float, float]] = []
            for seg_start, seg_end in merged:
                p_start = max(0.0, seg_start - padding)
                p_end = min(video_duration, seg_end + padding)
                padded.append((p_start, p_end))
            # Re-merge segments that may now touch/overlap after padding
            padded.sort(key=lambda x: x[0])
            merged = []
            for seg in padded:
                if not merged:
                    merged.append(seg)
                    continue
                last_start, last_end = merged[-1]
                seg_start, seg_end = seg
                if seg_start <= last_end:
                    merged[-1] = (last_start, max(last_end, seg_end))
                else:
                    merged.append(seg)
            # Re-filter sub-1-second segments after padding
            merged = [(s, e) for s, e in merged if e - s >= 1.0]

        return merged

    # ------------------------------------------------------------------
    def get_video_duration(self, video_path: str) -> float:
        """
        Return the duration (in seconds) of *video_path* using ffprobe.

        Returns ``0.0`` if the duration could not be retrieved.
        """
        cmd = [
            self._ffmpeg_bin("ffprobe"),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            video_path,
        ]
        try:
            result = self._run(cmd, description="get_video_duration")
            output = result.stdout.strip()
            if output:
                return float(output)
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
            self._log(f"Failed to get duration for \"{video_path}\": {exc}")
        return 0.0

    # ------------------------------------------------------------------
    def is_audio_only(self, file_path: str) -> bool:
        """
        Check whether *file_path* contains any video streams.

        Uses ffprobe to probe the file.  Returns ``True`` if no video stream
        is found (i.e. the file is audio-only).

        Parameters
        ----------
        file_path : str
            Path to the media file to probe.

        Returns
        -------
        bool
            ``True`` if the file has no video stream.
        """
        cmd = [
            self._ffmpeg_bin("ffprobe"),
            "-v", "error",
            "-select_streams", "v",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            file_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return result.stdout.strip() == ""
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    # ------------------------------------------------------------------
    def extract_random_clips(
        self,
        videos_segments: dict[str, list[tuple[float, float]]],
        clip_duration: float,
        total_target: float,
        temp_dir: Optional[str] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> list[dict]:
        """
        Plan a compilation by randomly selecting non-overlapping clips from the
        provided non-silent segments.

        Parameters
        ----------
        videos_segments : dict[str, list[tuple[float, float]]]
            Mapping video path -> list of non-silent (start, end) segments.
        clip_duration : float
            Desired duration (seconds) of each clip in the output.
        total_target : float
            Minimum total duration (seconds) the compilation should aim for.
        temp_dir : str, optional
            Directory for generated clip output paths. If ``None``, one is
            created automatically.
        log_callback : Callable, optional
            Override / additional log receiver for this method.

        Returns
        -------
        list[dict]
            Each dict has keys: ``video``, ``start``, ``duration``, ``output``.
        """
        log = log_callback or self._log

        # ---- 1. Build the candidate pool ---------------------------------
        candidates: list[dict] = []
        # Track used ranges per video path (list of (start, end) tuples)
        used_ranges: dict[str, list[tuple[float, float]]] = {}

        for video_path, segments in videos_segments.items():
            used_ranges.setdefault(video_path, [])
            for seg_start, seg_end in segments:
                seg_duration = seg_end - seg_start
                margin = seg_duration * 0.05
                trimmed_start = seg_start + margin
                trimmed_end = seg_end - margin
                if trimmed_end - trimmed_start < clip_duration:
                    continue  # segment too short after trimming
                # Every possible offset where a clip of clip_duration fits
                offset = 0.0
                while trimmed_start + offset + clip_duration <= trimmed_end + 0.001:
                    cand_start = trimmed_start + offset
                    cand_end = cand_start + clip_duration
                    candidates.append({
                        "video": video_path,
                        "start": cand_start,
                        "end": cand_end,
                        "duration": clip_duration,
                    })
                    # Step by half a second for variety (smooth sliding window)
                    offset += 0.5

        if not candidates:
            log("No valid clip candidates could be generated from the segments.")
            return []

        # ---- 2. Shuffle --------------------------------------------------
        random.shuffle(candidates)

        # ---- 3. Greedy selection -----------------------------------------
        selected: list[dict] = []
        accumulated = 0.0

        if temp_dir is None:
            temp_dir = tempfile.mkdtemp(prefix="svc_")

        clip_index = 0
        for cand in candidates:
            if accumulated >= total_target:
                break

            path = cand["video"]
            # Overlap check
            overlaps = False
            for used_start, used_end in used_ranges[path]:
                # Two intervals [a, b) and [c, d) overlap if a < d and c < b
                if cand["start"] < used_end and used_start < cand["end"]:
                    overlaps = True
                    break

            if overlaps:
                continue

            # Accept
            used_ranges[path].append((cand["start"], cand["end"]))
            clip_index += 1
            output_path = os.path.join(temp_dir, f"clip_{clip_index:03d}.mkv")
            selected.append({
                "video": cand["video"],
                "start": cand["start"],
                "duration": cand["duration"],
                "output": output_path,
            })
            accumulated += cand["duration"]

        log(f"Planned {len(selected)} clips from {len(videos_segments)} videos "
            f"(accumulated {accumulated:.1f}s, target {total_target:.1f}s)")

        return selected

    # ------------------------------------------------------------------
    def cut_clip(
        self,
        video_path: str,
        start: float,
        duration: float,
        output_path: str,
    ) -> bool:
        """
        Extract a single clip from *video_path* using re-encode with a
        guaranteed keyframe at frame 0 for clean concatenation.

        Parameters
        ----------
        video_path : str
            Source video.
        start : float
            Seek position in seconds.
        duration : float
            Clip length in seconds.
        output_path : str
            Destination path for the extracted clip.

        Returns
        -------
        bool
            ``True`` on success, ``False`` on failure.
        """
        # Ensure the output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        cmd = [
            self._ffmpeg_bin("ffmpeg"),
            "-y",
            "-fflags", "+genpts",
            "-ss", f"{start:.3f}",
            "-i", video_path,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-force_key_frames", "expr:eq(n,0)",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            output_path,
        ]
        cmd_str = " ".join(str(c) for c in cmd)
        self._log(f"Cutting clip: {cmd_str}")

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            # Verify output file is not truncated
            if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
                self._log(f"Clip \"{output_path}\" was truncated (size < 1 KB)")
                return False
            return True
        except subprocess.CalledProcessError as exc:
            self._log(f"Failed to cut clip \"{output_path}\": {exc}")
            if exc.stderr:
                stderr_lines = exc.stderr.strip().split("\n")
                tail = stderr_lines[-min(10, len(stderr_lines)):]
                self._log("FFmpeg stderr:\n" + "\n".join(tail))
            return False
        except FileNotFoundError:
            self._log("ffmpeg binary not found on PATH")
            return False

    # ------------------------------------------------------------------
    def concat_clips(
        self,
        clip_paths: list[str],
        output_path: str,
        encoder: str,
    ) -> bool:
        """
        Concatenate clips using ffmpeg's concat filter (filter_complex).
        Each clip is decoded independently, scaled to a common resolution,
        then concatenated at the frame level — zero PTS discontinuity.
        """
        encoder_map: dict[str, dict] = {
            "nvenc": {
                "codec": "h264_nvenc",
                "args": ["-preset", "p4", "-rc", "vbr", "-cq", "23"],
            },
            "qsv": {
                "codec": "h264_qsv",
                "args": ["-preset", "medium", "-global_quality", "23"],
            },
            "amf": {
                "codec": "h264_amf",
                "args": ["-quality", "quality"],
            },
            "cpu": {
                "codec": "libx264",
                "args": ["-preset", "fast", "-crf", "23"],
            },
        }

        enc_info = encoder_map.get(encoder.lower())
        if enc_info is None:
            self._log(f'Unknown encoder "{encoder}". Falling back to libx264.')
            enc_info = encoder_map["cpu"]

        # --- Probe resolution of first clip as target ---
        try:
            r = subprocess.run(
                [
                    self._ffmpeg_bin("ffprobe"), "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=s=x:p=0",
                    clip_paths[0],
                ],
                capture_output=True, text=True, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            target_w, target_h = r.stdout.strip().split("x")
        except Exception:
            target_w, target_h = "1920", "1080"

        n = len(clip_paths)

        # --- Build filter_complex ---
        filter_lines = []
        for i in range(n):
            # Scale each clip to target resolution with letterbox + square pixels
            filter_lines.append(
                f"[{i}:v]scale={target_w}:{target_h}"
                f":force_original_aspect_ratio=decrease"
                f",pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2"
                f",setsar=1[v{i}]"
            )

        # concat filter requires INTERLEAVED inputs: [v0][0:a][v1][1:a]...
        concat_inputs = "".join(f"[v{i}][{i}:a]" for i in range(n))
        filter_lines.append(
            f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]"
        )

        filter_complex = ";".join(filter_lines)

        # --- Run concat ---
        def _run_concat(codec: str, extra_args: list[str], label: str) -> bool:
            cmd = [self._ffmpeg_bin("ffmpeg"), "-y"]
            for clip in clip_paths:
                cmd.extend(["-i", clip])
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[outv]",
                "-map", "[outa]",
                "-c:v", codec,
                *extra_args,
                "-c:a", "aac",
                "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                output_path,
            ])
            self._log(f"Concatenating clips ({label}): {' '.join(str(c) for c in cmd[:6])}...")

            try:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                return True
            except subprocess.CalledProcessError as exc:
                self._log(f"Concatenation failed ({label}): exit code {exc.returncode}")
                if exc.stderr:
                    # Show last 40 lines — the actual error is at the end
                    stderr_lines = exc.stderr.strip().split("\n")
                    tail = stderr_lines[-min(40, len(stderr_lines)):]
                    self._log(f"FFmpeg stderr (last {len(tail)} lines):\n" + "\n".join(tail))
                return False
            except FileNotFoundError:
                self._log("ffmpeg binary not found on PATH")
                return False

        # --- Attempt 1: requested encoder ---
        is_gpu = encoder.lower() in ("nvenc", "qsv", "amf")
        success = _run_concat(enc_info["codec"], enc_info["args"], encoder.lower())

        # --- Attempt 2: GPU -> CPU fallback ---
        if not success and is_gpu:
            self._log("GPU encoding failed — falling back to CPU (libx264)...")
            cpu_info = encoder_map["cpu"]
            success = _run_concat(cpu_info["codec"], cpu_info["args"], "cpu (fallback)")

        return success

    # ------------------------------------------------------------------
    def concat_audio_clips(
        self,
        clip_paths: list[str],
        output_path: str,
    ) -> bool:
        """
        Concatenate audio-only clips into a single audio file using ffmpeg's
        concat demuxer.  No video encoding is performed.

        Parameters
        ----------
        clip_paths : list[str]
            Ordered list of audio clip file paths to concatenate.
        output_path : str
            Destination path for the final audio file (e.g. ``.m4a``).

        Returns
        -------
        bool
            ``True`` on success, ``False`` on failure.
        """
        # ---- Create concat list file ------------------------------------
        try:
            fd, concat_list_path = tempfile.mkstemp(
                suffix=".txt", prefix="concat_list_", text=True
            )
            with os.fdopen(fd, "w") as f:
                for clip_path in clip_paths:
                    escaped = clip_path.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
        except OSError as exc:
            self._log(f"Failed to create concat list file: {exc}")
            return False

        cmd = [
            self._ffmpeg_bin("ffmpeg"),
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c:a", "aac",
            "-b:a", "128k",
            "-vn",
            output_path,
        ]
        cmd_str = " ".join(str(c) for c in cmd)
        self._log(f"Concatenating audio clips: {cmd_str}")

        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return True
        except subprocess.CalledProcessError as exc:
            self._log(f"Audio concatenation failed: {exc}")
            return False
        except FileNotFoundError:
            self._log("ffmpeg binary not found on PATH")
            return False
        finally:
            try:
                os.unlink(concat_list_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def cleanup_temp(self, temp_dir: str) -> None:
        """
        Recursively delete *temp_dir* and all its contents.

        Parameters
        ----------
        temp_dir : str
            Path to the temporary directory to remove.
        """
        if not temp_dir or not os.path.isdir(temp_dir):
            return
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self._log(f"Cleaned up temporary directory: \"{temp_dir}\"")
        except Exception as exc:
            self._log(f"Cleanup warning for \"{temp_dir}\": {exc}")

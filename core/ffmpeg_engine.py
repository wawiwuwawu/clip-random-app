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
from datetime import datetime
from typing import Callable, Optional

_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

AUTO_ENCODER_LABEL = "Auto (Detect)"
CPU_ENCODER_DISPLAY = "CPU Software (libx264)"

ENCODER_DISPLAY_TO_KEY: dict[str, str] = {
    "NVIDIA NVENC (h264_nvenc)": "nvenc",
    "Intel QSV (h264_qsv)": "qsv",
    "AMD AMF (h264_amf)": "amf",
    CPU_ENCODER_DISPLAY: "cpu",
    "NVIDIA NVENC HEVC (hevc_nvenc)": "nvenc_hevc",
    "CPU HEVC (libx265)": "cpu_hevc",
}

GPU_ENCODER_KEYS = ("nvenc", "qsv", "amf")
GPU_CODEC_BY_KEY = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"}
GPU_DISPLAY_BY_KEY = {
    "nvenc": "NVIDIA NVENC (h264_nvenc)",
    "qsv": "Intel QSV (h264_qsv)",
    "amf": "AMD AMF (h264_amf)",
}


def map_encoder_display(display_name: str) -> str:
    """Map a UI combo-box display name to the short encoder key."""
    return ENCODER_DISPLAY_TO_KEY.get((display_name or "").strip(), "cpu")


def make_timestamp(moment: datetime | None = None) -> str:
    """Filesystem-safe timestamp used in output file names."""
    moment = moment or datetime.now()
    return moment.strftime("%Y-%m-%d_%H%M%S")


def compiler_output_name(stamp: str | None = None) -> str:
    """Fixed-pattern stem for Clip Compiler outputs."""
    return f"compiled_video_{stamp or make_timestamp()}"


def silence_output_name(source_basename: str, extension: str, stamp: str | None = None) -> str:
    """Fixed-pattern stem for Silence Removal outputs."""
    return f"{source_basename}_cleaned_{stamp or make_timestamp()}{extension}"


NOISE_MODEL_FILENAME = "mp.rnnn"


def escape_filter_path(path: str) -> str:
    """Make a Windows path safe inside an FFmpeg filtergraph option value."""
    return path.replace("\\", "/").replace(":", "\\:")


def resolve_noise_model_path(name: str = NOISE_MODEL_FILENAME) -> str:
    """Locate the bundled RNNoise model (dev cwd, frozen dir, or _MEIPASS)."""
    search_dirs: list[str] = [os.path.join(os.getcwd(), "assets", "models")]
    if getattr(sys, "frozen", False):
        search_dirs.append(
            os.path.join(os.path.dirname(sys.executable), "assets", "models")
        )
    if hasattr(sys, "_MEIPASS"):
        search_dirs.append(os.path.join(sys._MEIPASS, "assets", "models"))
        search_dirs.append(sys._MEIPASS)
    for directory in search_dirs:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return name


def build_noise_filters(
    mode: str,
    strength: str,
    noise_floor_db: float | None = None,
) -> list[str]:
    """
    Build the audio-filter chain for background-noise removal.

    ``mode="fft"`` uses the built-in afftdn denoiser. When *noise_floor_db*
    (measured from the file's silent gaps) is provided, the filter's noise
    floor is auto-calibrated so the reduction actually bites regardless of
    how clean or noisy the recording is; otherwise conservative static
    presets are used. ``mode="ai"`` chains the RNNoise neural filter —
    Strong runs it twice for maximum clarity.
    """
    s = (strength or "medium").lower()

    if (mode or "").lower() == "ai":
        model = resolve_noise_model_path()
        escaped = escape_filter_path(model)
        mix_map = {"light": "0.7", "medium": "1.0", "strong": "1.0"}
        mix = mix_map.get(s, "1.0")
        chain = [f"arnndn=m='{escaped}':mix={mix}"]
        if s == "strong":
            chain.append(f"arnndn=m='{escaped}'")
        return chain

    nr_map = {"light": 14, "medium": 24, "strong": 34}
    nr = nr_map.get(s, 24)

    if noise_floor_db is not None:
        nf = max(-55.0, min(-22.0, noise_floor_db + 5.0))
        filters = ["highpass=f=80" if s == "light" else "highpass=f=90"]
        if s == "strong":
            filters.append("lowpass=f=14000")
        filters.append(f"afftdn=nr={nr}:nf={nf:.1f}:tr=1")
        return filters

    presets = {
        "light": ["highpass=f=80", "afftdn=nr=14:nf=-45"],
        "medium": ["highpass=f=90", "afftdn=nr=24:nf=-40:tn=1:tr=1"],
        "strong": [
            "highpass=f=100",
            "lowpass=f=14000",
            "afftdn=nr=34:nf=-36:tn=1:tr=1",
        ],
    }
    return presets.get(s, presets["medium"])


class FFmpegEngine:
    """Stateless engine that wraps common FFmpeg workflows."""

    default_binary_dir: str | None = None

    def __init__(self) -> None:
        # External callers may replace this with a logging function.
        # Signature: log_callback(message: str) -> None
        self.log_callback: Optional[Callable[[str], None]] = None
        self.binary_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ffmpeg_bin(self, name: str) -> str:
        """Resolve an ffmpeg/ffprobe binary path.

        Order: instance override, class-wide override (Settings dialog),
        PyInstaller --onedir directory, PyInstaller --onefile temp dir,
        then the system PATH.
        """
        search_dirs: list[str] = []
        if self.binary_dir:
            search_dirs.append(self.binary_dir)
        if FFmpegEngine.default_binary_dir:
            search_dirs.append(FFmpegEngine.default_binary_dir)
        if getattr(sys, "frozen", False):
            search_dirs.append(os.path.dirname(sys.executable))
        if hasattr(sys, "_MEIPASS"):
            search_dirs.append(sys._MEIPASS)
        for directory in search_dirs:
            candidate = os.path.join(directory, f"{name}.exe")
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
            return subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", check=True,
                creationflags=_CREATE_NO_WINDOW,
            )
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

        stderr = result.stderr or ""  # silencedetect writes to stderr

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
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", check=True,
                creationflags=_CREATE_NO_WINDOW,
            )
            return (result.stdout or "").strip() == ""
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    # ------------------------------------------------------------------
    def plan_clips(
        self,
        videos_segments: dict[str, list[tuple[float, float]]],
        clip_duration: float,
        total_target: float,
        temp_dir: Optional[str] = None,
        scene_times: Optional[dict[str, list[float]]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> tuple[list[dict], dict[str, list[tuple[float, float]]]]:
        """
        Plan a compilation by randomly selecting non-overlapping clips from
        the provided segments.

        Returns ``(selected, candidates_by_video)`` where *selected* entries
        have keys ``video``/``start``/``duration``/``output`` and
        *candidates_by_video* holds every possible (start, end) candidate per
        video for later re-rolls.
        """
        log = log_callback or self._log
        scene_map = scene_times or {}

        candidates: list[dict] = []
        for video_path, segments in videos_segments.items():
            snaps = scene_map.get(video_path) or []
            for seg_start, seg_end in segments:
                margin = (seg_end - seg_start) * 0.05
                trimmed_start = seg_start + margin
                trimmed_end = seg_end - margin
                if trimmed_end - trimmed_start < clip_duration:
                    continue
                offset = 0.0
                while trimmed_start + offset + clip_duration <= trimmed_end + 0.001:
                    cand_start = self._snap_to_scene(trimmed_start + offset, snaps)
                    if cand_start + clip_duration > trimmed_end + 0.001:
                        cand_start = trimmed_start + offset
                    candidates.append({
                        "video": video_path,
                        "start": cand_start,
                        "end": cand_start + clip_duration,
                        "duration": clip_duration,
                    })
                    offset += 0.5

        if not candidates:
            log("No valid clip candidates could be generated from the sources.")
            return [], {}

        random.shuffle(candidates)

        if temp_dir is None:
            temp_dir = tempfile.mkdtemp(prefix="svc_")

        selected: list[dict] = []
        accumulated = 0.0
        used_ranges: dict[str, list[tuple[float, float]]] = {}
        clip_index = 0

        for cand in candidates:
            if accumulated >= total_target:
                break
            path = cand["video"]
            ranges = used_ranges.setdefault(path, [])
            if any(cand["start"] < e and s < cand["end"] for s, e in ranges):
                continue
            ranges.append((cand["start"], cand["end"]))
            clip_index += 1
            selected.append({
                "video": path,
                "start": cand["start"],
                "duration": cand["duration"],
                "output": os.path.join(temp_dir, f"clip_{clip_index:03d}.mkv"),
            })
            accumulated += cand["duration"]

        candidates_by_video: dict[str, list[tuple[float, float]]] = {}
        for cand in candidates:
            candidates_by_video.setdefault(cand["video"], []).append(
                (cand["start"], cand["end"])
            )

        log(f"Planned {len(selected)} clips "
            f"(accumulated {accumulated:.1f}s, target {total_target:.1f}s)")

        return selected, candidates_by_video

    @staticmethod
    def _snap_to_scene(
        start: float, scene_times: list[float], window: float = 0.75
    ) -> float:
        """Snap *start* to the nearest scene boundary within *window* seconds."""
        if not scene_times:
            return start
        nearest = min(scene_times, key=lambda t: abs(t - start))
        if abs(nearest - start) <= window:
            return max(0.0, nearest)
        return start

    # ------------------------------------------------------------------
    def detect_scene_changes(
        self, video_path: str, threshold: float = 0.4
    ) -> list[float]:
        """Return timestamps (seconds) of detected scene changes."""
        cmd = [
            self._ffmpeg_bin("ffmpeg"), "-hide_banner", "-nostats",
            "-i", video_path,
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=600,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self._log(f"Scene detection failed for \"{video_path}\": {exc}")
            return []
        stderr_text = result.stderr or ""
        times = [
            float(m.group(1))
            for m in re.finditer(r"pts_time:([\d.]+)", stderr_text)
        ]
        return sorted(set(times))

    # ------------------------------------------------------------------
    THUMB_W = 240
    THUMB_H_PORTRAIT = 426
    PORTRAIT_ZOOM = 1.5

    def generate_thumbnail(
        self,
        video_path: str,
        at_second: float,
        output_path: str,
        portrait: bool = False,
    ) -> bool:
        """Extract one small preview frame from *video_path*."""
        if portrait:
            th_w, th_h = self.THUMB_W, self.THUMB_H_PORTRAIT
            fg_w = int(th_w * self.PORTRAIT_ZOOM)
            vf = (
                f"split[bga][fga];"
                f"[bga]scale={th_w}:{th_h}:force_original_aspect_ratio=increase,"
                f"crop={th_w}:{th_h},gblur=sigma=4[bg];"
                f"[fga]scale={fg_w}:-2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
            )
        else:
            vf = f"scale={self.THUMB_W}:-2"
        cmd = [
            self._ffmpeg_bin("ffmpeg"), "-y",
            "-ss", f"{max(0.0, at_second):.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", "5",
            output_path,
        ]
        try:
            subprocess.run(
                cmd, capture_output=True, text=True, check=True,
                creationflags=_CREATE_NO_WINDOW,
            )
            return os.path.isfile(output_path) and os.path.getsize(output_path) > 0
        except Exception as exc:
            self._log(f"Thumbnail failed for \"{video_path}\": {exc}")
            return False

    # ------------------------------------------------------------------
    def cut_clip(
        self,
        video_path: str,
        start: float,
        duration: float,
        output_path: str,
        encoder: str = "cpu",
    ) -> bool:
        """
        Extract a single clip from *video_path* using re-encode with a
        guaranteed keyframe at frame 0 for clean concatenation.

        ``encoder="nvenc"`` cuts with hardware h264_nvenc (p1 / low-latency)
        and returns False so callers can fall back to CPU per clip.
        """
        # Ensure the output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        cut_encoder_map = {
            "cpu": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"],
            "nvenc": [
                "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                "-rc", "vbr", "-cq", "26",
            ],
        }
        video_args = cut_encoder_map.get(encoder, cut_encoder_map["cpu"])

        cmd = [
            self._ffmpeg_bin("ffmpeg"),
            "-y",
            "-fflags", "+genpts",
            "-ss", f"{start:.3f}",
            "-i", video_path,
            "-t", f"{duration:.3f}",
            *video_args,
            "-force_key_frames", "expr:eq(n,0)",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            output_path,
        ]
        cmd_str = " ".join(str(c) for c in cmd)
        self._log(f"Cutting clip ({encoder}): {cmd_str}")

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=_CREATE_NO_WINDOW)
        except subprocess.CalledProcessError as exc:
            if encoder == "nvenc":
                self._log(
                    f"NVENC cut failed (exit {exc.returncode}) — "
                    "falling back to CPU for this clip."
                )
            else:
                self._log(f"Failed to cut clip \"{output_path}\": {exc}")
            if exc.stderr:
                stderr_lines = exc.stderr.strip().split("\n")
                tail = stderr_lines[-min(10, len(stderr_lines)):]
                self._log("FFmpeg stderr:\n" + "\n".join(tail))
            return False
        except FileNotFoundError:
            self._log("ffmpeg binary not found on PATH")
            return False

        # Verify output file is not truncated
        if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
            self._log(f"Clip \"{output_path}\" was truncated (size < 1 KB)")
            return False
        return True

    # ------------------------------------------------------------------
    def concat_clips(
        self,
        clip_paths: list[str],
        output_path: str,
        encoder: str,
        durations: Optional[list[float]] = None,
        aspect: str = "landscape",
        crossfade: float = 0.0,
        audio_filters: Optional[list[str]] = None,
    ) -> bool:
        """
        Concatenate clips using ffmpeg's concat filter (filter_complex).

        ``aspect="portrait"`` renders 1080x1920 with a blurred background fill;
        ``crossfade > 0`` joins clips with an xfade/acrossfade chain instead of
        a hard cut (requires *durations*). HEVC encoder keys emit ``hvc1``
        tagged output for broader player support. ``audio_filters`` (e.g.
        noise-removal chains) wrap every input's audio stream before mixing.
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
            "nvenc_hevc": {
                "codec": "hevc_nvenc",
                "args": ["-preset", "p4", "-rc", "vbr", "-cq", "24", "-tag:v", "hvc1"],
            },
            "cpu_hevc": {
                "codec": "libx265",
                "args": ["-preset", "fast", "-crf", "26", "-tag:v", "hvc1"],
            },
        }
        fallback_for = {
            "nvenc": "cpu", "qsv": "cpu", "amf": "cpu", "nvenc_hevc": "cpu_hevc",
        }

        enc_info = encoder_map.get(encoder.lower())
        if enc_info is None:
            self._log(f'Unknown encoder "{encoder}". Falling back to libx264.')
            enc_info = encoder_map["cpu"]

        use_xfade = (
            crossfade > 0
            and durations is not None
            and len(clip_paths) >= 2
            and len(durations) == len(clip_paths)
        )

        # --- Target resolution ---
        n = len(clip_paths)
        if aspect == "portrait":
            target_w, target_h = "1080", "1920"
        else:
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
                    creationflags=_CREATE_NO_WINDOW,
                )
                target_w, target_h = r.stdout.strip().split("x")
            except Exception:
                target_w, target_h = "1920", "1080"

        # --- Build filter_complex ---
        filter_lines: list[str] = []

        def _src_audio(i: int) -> str:
            return f"[af{i}]" if audio_filters else f"[{i}:a]"

        if audio_filters:
            chain = ",".join(audio_filters)
            for i in range(n):
                filter_lines.append(f"[{i}:a]{chain}[af{i}]")

        for i in range(n):
            if aspect == "portrait":
                zoom_w = int(int(target_w) * self.PORTRAIT_ZOOM)
                chain = (
                    f"[{i}:v]split=2[bga{i}][fga{i}]"
                    f";[bga{i}]scale={target_w}:{target_h}"
                    f":force_original_aspect_ratio=increase,crop={target_w}:{target_h}"
                    f",gblur=sigma=12[bg{i}]"
                    f";[fga{i}]scale={zoom_w}:-2[fg{i}]"
                    f";[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1"
                )
            else:
                chain = (
                    f"[{i}:v]scale={target_w}:{target_h}"
                    f":force_original_aspect_ratio=decrease"
                    f",pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
                )
            if use_xfade:
                chain += ",fps=30,settb=AVTB"
            filter_lines.append(f"{chain}[v{i}]")

        if use_xfade:
            fade = float(crossfade)
            length = (durations or [])[0]
            prev_v, prev_a = "[v0]", _src_audio(0)
            for i in range(1, n):
                is_last = i == n - 1
                vx = "[outv]" if is_last else f"[vx{i}]"
                ax = "[outa]" if is_last else f"[ax{i}]"
                offset = max(0.0, length - fade)
                filter_lines.append(
                    f"{prev_v}[v{i}]xfade=transition=fade"
                    f":duration={fade:.3f}:offset={offset:.3f}{vx}"
                )
                filter_lines.append(
                    f"{prev_a}{_src_audio(i)}acrossfade=d={fade:.3f}{ax}"
                )
                length = offset + (durations or [])[i]
                prev_v, prev_a = vx, ax
        else:
            # concat filter requires INTERLEAVED inputs: [v0][a0][v1][a1]...
            concat_inputs = "".join(
                f"[v{i}]{_src_audio(i)}" for i in range(n)
            )
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
                subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=_CREATE_NO_WINDOW)
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
        key = encoder.lower()
        success = _run_concat(enc_info["codec"], enc_info["args"], key)

        # --- Attempt 2: GPU -> CPU fallback ---
        if not success and key in fallback_for:
            fb_key = fallback_for[key]
            fb = encoder_map[fb_key]
            self._log(f"Hardware encoding failed — falling back to CPU ({fb['codec']})...")
            success = _run_concat(fb["codec"], fb["args"], f"{fb_key} (fallback)")

        return success

    # ------------------------------------------------------------------
    def concat_audio_clips(
        self,
        clip_paths: list[str],
        output_path: str,
        audio_filters: Optional[list[str]] = None,
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
        audio_filters : list[str], optional
            Audio filter chain (e.g. noise removal) applied once after the
            concatenated stream is decoded.

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
        ]
        if audio_filters:
            cmd.extend(["-af", ",".join(audio_filters)])
        cmd.extend([
            "-c:a", "aac",
            "-b:a", "128k",
            "-vn",
            output_path,
        ])
        cmd_str = " ".join(str(c) for c in cmd)
        self._log(f"Concatenating audio clips: {cmd_str}")

        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=_CREATE_NO_WINDOW)
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

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_resolved_ffmpeg_path(self) -> str:
        """Return the resolved ffmpeg binary path (for diagnostics)."""
        return self._ffmpeg_bin("ffmpeg")

    def get_ffmpeg_version(self) -> str:
        """Return the first line of ``ffmpeg -version`` for display."""
        try:
            result = subprocess.run(
                [self._ffmpeg_bin("ffmpeg"), "-version"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
            lines = result.stdout.strip().splitlines()
            if lines:
                return lines[0].strip()
        except Exception as exc:
            self._log(f"Could not query ffmpeg version: {exc}")
        return "ffmpeg not found"

    # ------------------------------------------------------------------
    # GPU encoder detection
    # ------------------------------------------------------------------

    def detect_gpu_encoder(self) -> str:
        """Detect the best usable hardware H.264 encoder.

        Two-step check:
        1. The ffmpeg binary must list ``h264_<key>`` among its encoders.
        2. A real 0.2-second encode must succeed (driver / session check).

        Returns one of ``"nvenc"``, ``"qsv"``, ``"amf"`` or ``"cpu"``.
        """
        binary = self._ffmpeg_bin("ffmpeg")
        self._log(f"Detecting GPU encoder (ffmpeg: {binary})...")

        try:
            result = subprocess.run(
                [binary, "-hide_banner", "-encoders"],
                capture_output=True, text=True, errors="replace", timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
            listing = ((result.stdout or "") + (result.stderr or "")).lower()
        except Exception as exc:
            self._log(f"Encoder detection failed ({exc}) — using CPU.")
            return "cpu"

        present = [key for key in GPU_ENCODER_KEYS if f"h264_{key}" in listing]
        if not present:
            self._log("No hardware encoders in this ffmpeg build — using CPU.")
            return "cpu"

        for key in present:
            if self._probe_encoder(key, binary):
                self._log(f"GPU encoder detected and verified: {GPU_DISPLAY_BY_KEY[key]}")
                return key

        self._log("Hardware encoders listed but none initialized — using CPU.")
        return "cpu"

    def _probe_encoder(self, key: str, binary: str) -> bool:
        """Run a tiny real encode to verify a hardware encoder works."""
        codec = GPU_CODEC_BY_KEY[key]
        cmd = [
            binary, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", "color=c=black:s=256x256:d=0.2:r=30",
            "-c:v", codec,
            "-frames:v", "3",
            "-f", "null", "-",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                self._log(f"Probe {codec}: OK")
                return True
            else:
                err_lines = (result.stderr or "").strip().splitlines()
                reason = err_lines[-1] if err_lines else f"exit code {result.returncode}"
                self._log(f"Probe {codec} failed: {reason}")
                return False
        except Exception as exc:
            self._log(f"Probe {codec} failed: {exc}")
            return False

    # ------------------------------------------------------------------
    def measure_noise_floor(
        self,
        video_path: str,
        avoid_ranges: list[tuple[float, float]],
        probe_max: float = 1.5,
        min_gap: float = 0.4,
    ) -> float | None:
        """
        Estimate the recording's noise floor from its silent gaps.

        Finds the longest window outside *avoid_ranges* (the non-silent
        segments) and measures ``mean_volume`` there via volumedetect.
        Returns dBFS, or ``None`` when no usable gap exists.
        """
        duration = self.get_video_duration(video_path)
        if duration <= 0:
            return None

        ranges = sorted(avoid_ranges)
        gaps: list[tuple[float, float]] = []
        cursor = 0.0
        for start, end in ranges:
            if start > cursor:
                gaps.append((cursor, min(start, duration)))
            cursor = max(cursor, end)
        if cursor < duration:
            gaps.append((cursor, duration))

        gaps = [g for g in gaps if g[1] - g[0] >= min_gap]
        if gaps:
            gap_start, gap_end = max(gaps, key=lambda g: g[1] - g[0])
            gap_len = gap_end - gap_start
            probe_len = min(probe_max, gap_len * 0.8)
            probe_at = gap_start + (gap_len - probe_len) / 2
        else:
            # No usable silent gap (noise floor defeats silence detection) —
            # approximate with the quietest 0.5s block of the whole file.
            return self._measure_floor_astats(video_path)

        cmd = [
            self._ffmpeg_bin("ffmpeg"),
            "-ss", f"{probe_at:.3f}",
            "-t", f"{probe_len:.3f}",
            "-i", video_path,
            "-af", "volumedetect",
            "-f", "null", "-",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace",
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self._log(f"Noise floor measurement failed: {exc}")
            return None

        for line in (result.stderr or "").splitlines():
            if "mean_volume:" in line:
                try:
                    return float(line.split(":")[1].replace("dB", "").strip())
                except ValueError:
                    return None
        return None

    def _measure_floor_astats(self, video_path: str) -> float | None:
        """
        Fallback floor estimation: one pass with astats printing per-block
        RMS levels; the quietest block approximates the noise floor even
        when the recording never goes truly silent.
        """
        cmd = [
            self._ffmpeg_bin("ffmpeg"),
            "-hide_banner", "-nostats",
            "-i", video_path,
            "-vn",
            "-af",
            ("asetnsamples=n=22050:p=0,"
             "astats=metadata=1:reset=1,"
             "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"),
            "-f", "null", "-",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self._log(f"astats floor scan failed: {exc}")
            return None

        values: list[float] = []
        for line in (result.stdout or "").splitlines():
            if "RMS_level=" not in line:
                continue
            raw = line.rsplit("=", 1)[1].strip()
            try:
                value = float(raw)
            except ValueError:
                continue
            if -90.0 < value < 0.0:
                values.append(value)
        return min(values) if values else None

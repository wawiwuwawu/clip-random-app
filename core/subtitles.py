"""
Speech-to-text transcription → SRT via faster-whisper (lazy import).

The application works without this package installed; callers should
check ``is_available`` and surface install instructions when False.

GPU-first strategy: CUDA float16 is attempted first. When the CUDA
libraries are missing, ``core.cuda_setup.ensure_cuda_libs`` downloads
them once into the app-data folder; if that fails too, we fall back to
CPU int8 silently.
"""

import os
import sys

from pathlib import Path

# Redirect the HuggingFace cache into our app-data folder BEFORE anything
# imports huggingface_hub, so uninstalling the app cleans everything.
os.environ.setdefault(
    "HF_HOME",
    str(Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")))
        / "SmartVideoCompiler" / "hf"),
)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def patch_ssl_windows() -> bool:
    """Bypass corrupt Windows Certificate Store using certifi's CA bundle for SSL downloads."""
    try:
        import ssl
        import urllib.request
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        handler = urllib.request.HTTPSHandler(context=ctx)
        urllib.request.install_opener(urllib.request.build_opener(handler))
        return True
    except Exception:
        return False


patch_ssl_windows()

SUPPORTED_LANGUAGES = {
    "Auto-detect": None,
    "Indonesian": "id",
    "English": "en",
    "Japanese": "ja",
    "Korean": "ko",
    "Arabic": "ar",
    "Spanish": "es",
}

MODEL_SIZES_MB = {"tiny": 75, "base": 145, "small": 461, "medium": 1531}
MODEL_MIN_BYTES = {
    "tiny": 25 * 1024 * 1024,      # ~39 MB model.bin
    "base": 90 * 1024 * 1024,      # ~142 MB model.bin
    "small": 300 * 1024 * 1024,    # ~466 MB model.bin
    "medium": 1000 * 1024 * 1024,  # ~1.5 GB model.bin
}
_MODEL_REPO_PREFIX = "Systran/faster-whisper-"


def app_data_dir() -> Path:
    directory = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")))
    directory = directory / "SmartVideoCompiler"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def import_error_detail() -> str | None:
    """
    Try importing faster_whisper. Returns ``None`` when it works, otherwise
    a detailed explanation including the interpreter path and the exact
    failing module — so "not installed" never misleads again.
    """
    try:
        import faster_whisper  # noqa: F401
        return None
    except Exception:
        import traceback

        tb = traceback.format_exc(limit=3)
        executable = sys.executable
        return (
            f"import failed under \"{executable}\"\n{tb.strip()}\n"
            f"Fix: \"{executable}\" -m pip install faster-whisper"
        )


def resolve_bundled_model(model_size: str) -> str | None:
    """Return the bundled model directory for *model_size* if shipped."""
    relative = os.path.join("assets", "models", f"faster-whisper-{model_size}")
    search_dirs = [os.getcwd()]
    if getattr(sys, "frozen", False):
        search_dirs.insert(0, os.path.dirname(sys.executable))
    if hasattr(sys, "_MEIPASS"):
        search_dirs.insert(0, sys._MEIPASS)
        search_dirs.append(sys._MEIPASS)
    for base in search_dirs:
        candidate = os.path.join(base, relative)
        if os.path.isdir(candidate) and os.listdir(candidate):
            return candidate
    return None


def _fmt_srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _purge_model_cache(model_size: str) -> None:
    """Safely remove corrupted HF cache directory for a given model size."""
    import shutil
    cache_root = Path(os.environ.get("HF_HOME", "")) / "hub"
    folder_name = f"models--{_MODEL_REPO_PREFIX.replace('/', '--')}{model_size}"
    target = cache_root / folder_name
    if target.exists():
        try:
            shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass


def is_model_cached(model_size: str) -> bool:
    """True when the HF snapshot already exists locally (or is bundled)."""
    return _model_cached(model_size)


def _find_cached_model_bin(model_size: str) -> Path | None:
    """Find local model.bin file in HF cache directory if present."""
    hf_home = os.environ.get("HF_HOME")
    if not hf_home:
        return None
    cache_root = Path(hf_home) / "hub"
    folder_name = f"models--{_MODEL_REPO_PREFIX.replace('/', '--')}{model_size}"
    target = cache_root / folder_name
    if not target.exists():
        return None
    try:
        for model_bin in target.glob("**/model.bin"):
            if model_bin.is_file():
                return model_bin
    except Exception:
        pass
    return None


def download_model(
    model_size: str,
    progress_cb=None,
    cancel_check=None,
) -> bool:
    """
    Fetch the HF snapshot for *model_size* into local cache with SSL patch & retries.

    ``progress_cb(-1)`` signals an indeterminate download; ``1.0`` fires on
    completion. Returns True when a local snapshot path is available.
    """
    patch_ssl_windows()
    repo = _MODEL_REPO_PREFIX + model_size
    progress_cb = progress_cb or (lambda fraction: None)

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(f"huggingface_hub missing: {exc}") from exc

    if cancel_check and cancel_check():
        return False

    progress_cb(-1)
    last_error = None

    for attempt in range(1, 4):
        if cancel_check and cancel_check():
            return False
        try:
            path = snapshot_download(repo_id=repo, max_workers=2)
            if path and _model_cached(model_size):
                progress_cb(1.0)
                return True
        except Exception as exc:
            last_error = exc

    model_bin = _find_cached_model_bin(model_size)
    if model_bin and model_bin.is_file():
        cur_mb = int(model_bin.stat().st_size / (1024 * 1024))
        tot_mb = MODEL_SIZES_MB.get(model_size, "?")
        raise RuntimeError(
            f"Unduhan model \"{model_size}\" terputus ({cur_mb} MB / ~{tot_mb} MB). "
            f"Silakan klik 'Download' kembali untuk melanjutkan sisa unduhan."
        )

    raise RuntimeError(
        f"Gagal mengunduh model Whisper \"{model_size}\": {last_error or 'Koneksi terputus'}"
    )


def _load_model(
    model_size: str,
    log,
    dl_progress_cb=None,
    status_cb=None,
    cancel_check=None,
) -> object:
    from core.cuda_setup import ensure_cuda_libs
    from faster_whisper import WhisperModel

    log = log or (lambda msg: None)
    status_cb = status_cb or (lambda msg: None)
    dl_progress_cb = dl_progress_cb or (lambda fraction: None)

    # 1. Ensure model is downloaded & verified 100%
    if not _model_cached(model_size):
        size_mb = MODEL_SIZES_MB.get(model_size)
        msg = (
            f"Downloading Whisper model \"{model_size}\" "
            f"(~{size_mb} MB, one time only)..."
            if size_mb else
            f"Downloading Whisper model \"{model_size}\" (one time only)..."
        )
        status_cb(msg)
        log(msg)
        download_model(model_size, progress_cb=dl_progress_cb, cancel_check=cancel_check)

    bundled = resolve_bundled_model(model_size)
    source = bundled or (_MODEL_REPO_PREFIX + model_size)

    # 2. Try CUDA first if available
    status_cb("Checking CUDA libraries...")
    if ensure_cuda_libs(log_cb=log, progress_cb=dl_progress_cb,
                        cancel_check=cancel_check):
        try:
            status_cb("Loading speech model on CUDA...")
            model = WhisperModel(source, device="cuda", compute_type="float16")
            origin = "bundled" if bundled else "cache"
            log(f"Whisper backend: CUDA float16 (model={model_size}, {origin})")
            return model
        except Exception as exc:
            log(f"CUDA load failed ({exc.__class__.__name__}: {exc}) — using CPU fallback.")
    else:
        log("CUDA unavailable — using CPU.")

    # 3. CPU Fallback
    status_cb("Loading speech model on CPU...")
    model = WhisperModel(source, device="cpu", compute_type="int8")
    log(f"Whisper backend: CPU int8 (model={model_size})")
    return model


def _model_cached(model_size: str) -> bool:
    """Pure query check: True when local HF snapshot contains complete model.bin."""
    bundled = resolve_bundled_model(model_size)
    if bundled:
        bin_file = Path(bundled) / "model.bin"
        if bin_file.is_file() and bin_file.stat().st_size > 1024 * 1024:
            return True

    min_bytes = MODEL_MIN_BYTES.get(model_size, 10 * 1024 * 1024)

    # 1. Try huggingface_hub snapshot_download with local_files_only
    try:
        from huggingface_hub import snapshot_download
        path = snapshot_download(
            _MODEL_REPO_PREFIX + model_size,
            local_files_only=True,
        )
        model_file = Path(path) / "model.bin"
        if model_file.is_file() and model_file.stat().st_size >= min_bytes:
            return True
    except Exception:
        pass

    # 2. Direct filesystem fallback check for Windows cache/symlink quirks
    model_bin = _find_cached_model_bin(model_size)
    if model_bin and model_bin.stat().st_size >= min_bytes:
        return True

    return False


def transcribe(
    media_path: str,
    model_size: str = "small",
    language: str | None = None,
    progress_cb=None,
    cancel_check=None,
    log=None,
    status_cb=None,
    dl_progress_cb=None,
) -> tuple[list[tuple[float, float, str]], str | None]:
    """
    Transcribe *media_path* into ``(cues, detected_language)``.

    Each cue is ``(start_sec, end_sec, text)``. ``progress_cb`` receives
    segment completion as 0..1, and ``-1`` while a download is in progress
    (busy indicator). ``dl_progress_cb`` receives the CUDA library download
    fraction as 0..1. Raises RuntimeError when faster-whisper is missing.
    """
    log = log or (lambda msg: None)
    status_cb = status_cb or (lambda msg: None)
    progress_cb = progress_cb or (lambda fraction: None)

    if not is_available():
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper"
        )

    lang_code = SUPPORTED_LANGUAGES.get(language)
    if language is not None and lang_code is None:
        lang_code = language  # allow raw codes too

    status_cb("Loading speech model...")
    model = _load_model(
        model_size, log,
        dl_progress_cb=dl_progress_cb,
        status_cb=status_cb,
        cancel_check=cancel_check,
    )

    status_cb("Transcribing audio...")
    segments, info = model.transcribe(
        media_path,
        language=lang_code,
        vad_filter=True,
    )

    cues: list[tuple[float, float, str]] = []
    for segment in segments:
        if cancel_check and cancel_check():
            log("Transcription cancelled.")
            break
        text = (segment.text or "").strip()
        if not text:
            continue
        cues.append((float(segment.start), float(segment.end), text))
        progress_cb(min(1.0, segment.end / max(info.duration, 0.1)))

    detected = getattr(info, "language", None)
    return cues, detected


def write_srt(cues: list[tuple[float, float, str]], srt_path: str) -> None:
    lines: list[str] = []
    for index, (start, end, text) in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}")
        lines.append(text)
        lines.append("")
    Path(srt_path).write_text("\n".join(lines), encoding="utf-8")


def write_txt(cues: list[tuple[float, float, str]], txt_path: str) -> None:
    """Plain-text transcript: one line per cue."""
    Path(txt_path).write_text(
        "\n".join(text for _, _, text in cues) + "\n", encoding="utf-8"
    )


def transcribe_to_srt(
    media_path: str,
    srt_path: str,
    model_size: str = "small",
    language: str | None = None,
    progress_cb=None,
    cancel_check=None,
    log=None,
    status_cb=None,
    dl_progress_cb=None,
) -> tuple[int, str | None]:
    """
    Backward-compatible wrapper: transcribe *media_path* and write an SRT
    file to *srt_path*. Returns ``(cue_count, detected_language_or_None)``.
    """
    cues, detected = transcribe(
        media_path, model_size=model_size, language=language,
        progress_cb=progress_cb, cancel_check=cancel_check,
        log=log, status_cb=status_cb, dl_progress_cb=dl_progress_cb,
    )
    write_srt(cues, srt_path)
    return len(cues), detected

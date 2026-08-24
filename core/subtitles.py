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

from pathlib import Path

# Redirect the HuggingFace cache into our app-data folder BEFORE anything
# imports huggingface_hub, so uninstalling the app cleans everything.
os.environ.setdefault(
    "HF_HOME",
    str(Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")))
        / "SmartVideoCompiler" / "hf"),
)

SUPPORTED_LANGUAGES = {
    "Auto-detect": None,
    "Indonesian": "id",
    "English": "en",
    "Japanese": "ja",
    "Korean": "ko",
    "Arabic": "ar",
    "Spanish": "es",
}


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


def _fmt_srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _load_model(model_size: str, log) -> object:
    from core.cuda_setup import ensure_cuda_libs

    from faster_whisper import WhisperModel

    if ensure_cuda_libs(log_cb=log):
        try:
            model = WhisperModel(model_size, device="cuda", compute_type="float16")
            log(f"Whisper backend: CUDA float16 (model={model_size})")
            return model
        except Exception as exc:
            log(f"CUDA load failed ({exc.__class__.__name__}) — using CPU.")

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    log(f"Whisper backend: CPU int8 (model={model_size})")
    return model


def transcribe_to_srt(
    media_path: str,
    srt_path: str,
    model_size: str = "small",
    language: str | None = None,
    progress_cb=None,
    cancel_check=None,
    log=None,
    status_cb=None,
) -> tuple[int, str | None]:
    """
    Transcribe *media_path* and write an SRT file to *srt_path*.

    Returns ``(cue_count, detected_language_or_None)``.
    Raises RuntimeError when faster-whisper is not installed.
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
    model = _load_model(model_size, log)

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

    lines: list[str] = []
    for index, (start, end, text) in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}")
        lines.append(text)
        lines.append("")

    Path(srt_path).write_text("\n".join(lines), encoding="utf-8")

    detected = getattr(info, "language", None)
    return len(cues), detected

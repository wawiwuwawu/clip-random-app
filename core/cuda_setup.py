"""
Runtime installer for the CUDA libraries required by faster-whisper on
Windows (cuBLAS + cuDNN).

The desktop installer deliberately does NOT bundle these (~1.1 GB).
On first GPU use, ``ensure_cuda_libs`` downloads two pinned NVIDIA wheels
from PyPI, extracts only the DLLs into
``%LOCALAPPDATA%/SmartVideoCompiler/cuda/bin`` and registers that folder
so CTranslate2 can load them. Any failure simply returns False — callers
fall back to CPU without surfacing an error.
"""

import json
import os
import zipfile
import urllib.request
import tempfile

from pathlib import Path

PINNED_PACKAGES = [
    ("nvidia-cublas-cu12", "12.6.4.1"),
    ("nvidia-cudnn-cu12", "9.10.2.21"),
]

_KEY_DLLS = ("cudnn64_9.dll", "cublas64_12.dll", "cublasLt64_12.dll")


class _DownloadCancelled(Exception):
    pass


def cuda_bin_dir() -> Path:
    directory = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")))
    directory = directory / "SmartVideoCompiler" / "cuda" / "bin"
    return directory


def _register(directory: Path) -> None:
    try:
        os.add_dll_directory(str(directory))
    except (AttributeError, OSError):
        pass
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"


def _dlls_present(directory: Path) -> bool:
    return all((directory / dll).is_file() for dll in _KEY_DLLS)


def ensure_cuda_libs(log_cb=None, progress_cb=None, cancel_check=None) -> bool:
    """
    Make cuBLAS/cuDNN DLLs available. Returns True when the directories are
    registered (GPU load may still fail for unrelated driver reasons).
    """
    log = log_cb or (lambda msg: None)
    report = progress_cb or (lambda fraction, label: None)

    directory = cuda_bin_dir()
    if _dlls_present(directory):
        _register(directory)
        return True

    log(
        "CUDA libraries not found — downloading NVIDIA runtime "
        "(~1.1 GB, one time only)..."
    )

    target = directory
    try:
        target.mkdir(parents=True, exist_ok=True)
        for index, (package, version) in enumerate(PINNED_PACKAGES):
            if cancel_check and cancel_check():
                log("CUDA download cancelled.")
                return False
            url, size = _resolve_wheel(package, version)
            label = package
            base_fraction = index / len(PINNED_PACKAGES)
            span = 1.0 / len(PINNED_PACKAGES)

            archive_path = Path(tempfile.gettempdir()) / f"svc_{Path(url).name}"
            _download(url, archive_path, size,
                      lambda frac, b=base_fraction, s=span, l=label:
                      report(b + frac * s, l),
                      cancel_check=cancel_check)
            _extract_dlls(archive_path, target)
            archive_path.unlink(missing_ok=True)
    except _DownloadCancelled:
        log("CUDA download cancelled.")
        return False
    except Exception as exc:
        log(f"CUDA auto-setup failed ({exc}) — using CPU instead.")
        return False

    if not _dlls_present(target):
        log("CUDA DLLs incomplete after setup — using CPU instead.")
        return False

    _register(target)
    log("CUDA libraries ready.")
    return True


# ----------------------------------------------------------------------

def _resolve_wheel(package: str, version: str) -> tuple[str, int | None]:
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)
    for item in data.get("urls", []):
        name = item.get("filename", "")
        if name.endswith(".whl") and "win_amd64" in name:
            return item["url"], item.get("size")
    raise RuntimeError(f"No win_amd64 wheel for {package}=={version}")


def _download(url: str, destination: Path, expected_size: int | None,
              progress, cancel_check=None) -> None:
    block = 1024 * 256
    downloaded = 0
    with urllib.request.urlopen(url, timeout=60) as response, \
            open(destination, "wb") as handle:
        total = expected_size or int(response.headers.get("Content-Length") or 0)
        while True:
            if cancel_check and cancel_check():
                raise _DownloadCancelled()
            chunk = response.read(block)
            if not chunk:
                break
            handle.write(chunk)
            downloaded += len(chunk)
            if total:
                progress(min(1.0, downloaded / total))
    progress(1.0)


def _extract_dlls(archive_path: Path, target: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            lowered = name.lower()
            if lowered.endswith(".dll") and "/bin/" in lowered:
                extracted = archive.extract(name, tempfile.gettempdir())
                extracted_path = Path(extracted)
                shutil_move(extracted_path, target / extracted_path.name)


def shutil_move(source: Path, destination: Path) -> None:
    import shutil

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))

# Smart Video Compiler

Aplikasi desktop Windows (PySide6 + FFmpeg) untuk:

- **Clip Compiler** — pilih folder/file video, app mengambil klip acak
  terbaik lalu menyatukannya menjadi satu video (preview + re-roll per
  klip, mode portrait 9:16, transisi fade opsional).
- **Silence Removal** — buang bagian hening dari satu file video/audio,
  lengkap dengan **penghilang noise** (FFT auto-kalibrasi / AI RNNoise)
  dan **auto-subtitle .srt**.
- **Transcribe** — media apa pun langsung jadi `.srt` + `.txt`.

Encoding memakai NVENC / QSV / AMF otomatis dengan fallback CPU.
Subtitle dijalankan oleh [faster-whisper](https://github.com/SYSTRAN/faster-whisper);
model `tiny` sudah ikut dalam installer, model lain terunduh otomatis.

## Requirements

- Windows 10/11 64-bit
- Python 3.11+ (untuk mode dev)
- FFmpeg & FFprobe di PATH (untuk dev; installer membawa miliknya sendiri)
- GPU NVIDIA opsional (NVENC) — tanpa GPU pun tetap jalan via CPU

## Menjalankan dari source (dev)

```bash
git clone https://github.com/wawiwuwawu/clip-random-app.git
cd clip-random-app
pip install -r requirements.txt
python main.py
```

FFmpeg cepat dipasang via WinGet: `winget install Gyan.FFmpeg`.

## Build installer

Cara termudah — satu script (butuh [Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```powershell
.\build_local.ps1
```

Hasil: `SmartVideoCompiler_Setup.exe` di root proyek.

Atau manual:

```bash
pyinstaller SmartVideoCompiler.spec --noconfirm
ISCC.exe SmartVideoCompiler.iss
```

### Releases otomatis

Push tag → GitHub Actions membangun installer dan menempelkannya ke
halaman [Releases](https://github.com/wawiwuwawu/clip-random-app/releases):

```bash
git tag v1.0.0
git push --tags
```

## Struktur Proyek

```
main.py                  entry point + wiring sinyal & antrean job
core/
  ffmpeg_engine.py       semua operasi FFmpeg/FFprobe (cut, concat, denoise…)
  worker.py              QThread workers (planning/render/silence/transcribe)
  clip_plan.py           data model sesi planning + re-roll
  subtitles.py           faster-whisper → SRT/TXT
  cuda_setup.py          auto-download cuBLAS/cuDNN untuk transkripsi GPU
  history.py             riwayat job (%LOCALAPPDATA%)
ui/
  main_window.py         window utama 3 mode + settings/help/history dialog
  clip_preview.py        review klip sebelum render
assets/models/           RNNoise denoise model + Whisper tiny (bundel)
```

## License

[MIT](LICENSE) — © 2026 wawiwuwawu (wawunime)

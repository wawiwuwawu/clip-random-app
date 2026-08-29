# Smart Video Compiler

Aplikasi desktop Windows ringan untuk mengolah video & audio secara otomatis.
Dibangun dengan PySide6 (Qt6) dan FFmpeg — gratis dan open source (MIT).

## Fitur Utama

- **Clip Compiler** — pilih folder/file video, aplikasi mengambil klip acak
  terbaik lalu menyatukannya menjadi satu video utuh. Tersedia preview per klip
  dengan opsi *exclude* / *re-roll*, mode portrait 9:16, serta transisi fade
  opsional.
- **Silence Removal** — buang bagian hening dari satu file video/audio.
  Dilengkapi **penghilang noise** (AI RNNoise atau FFT auto-kalibrasi) dan
  pembuatan **subtitle .srt** otomatis.
- **Transcribe** — ubah audio/video apa pun menjadi teks **.srt** + **.txt**
  menggunakan [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

Encoding video memakai akselerasi hardware (NVENC / QSV / AMF) secara otomatis
dengan fallback ke CPU bila diperlukan. Model Whisper `tiny` sudah dibundel
di dalam installer; model yang lebih besar akan diunduh sekali pada pemakaian
pertama.

## Persyaratan

- Windows 10 / 11 64-bit
- Tidak butuh Python atau FFmpeg terpisah — keduanya sudah termasuk dalam installer.
- GPU NVIDIA opsional (lebih cepat); tanpa GPU tetap berjalan via CPU.

## Cara Mendapatkan & Memasang

1. Buka halaman [Releases](https://github.com/wawiwuwawu/clip-random-app/releases)
   di GitHub.
2. Unduh **`SmartVideoCompiler_Setup.exe`** (atau `SmartVideoCompiler_Portable.zip`
   bila Anda lebih suka versi portable yang cukup diekstrak dan dijalankan).
3. Jalankan installer dan ikuti petunjuknya. Selesai.

### Memperbarui ke versi terbaru

Cukup jalankan installer versi terbaru. Installer akan memperbarui aplikasi di
tempatnya (in-place) — **tidak perlu uninstall manual**. Pengaturan Anda serta
model Whisper yang sudah diunduh tersimpan di folder data aplikasi
(`%LOCALAPPDATA%\SmartVideoCompiler`) dan akan tetap ada setelah pembaruan.

> Tips: jika setelah memperbarui Anda mendapati perilaku aneh (misal fitur
> tertentu gagal), uninstall aplikasi lewat *Settings → Apps*, pastikan folder
> `C:\Program Files\Smart Video Compiler\` sudah terhapus, lalu pasang ulang
> installer terbaru. Cara ini menjamin semua file internal benar-benar segar.

Jika subtitle gagal diunduh karena cache rusak, hapus folder
`%LOCALAPPDATA%\SmartVideoCompiler\hf` lalu coba lagi — aplikasi akan
mengunduh ulang secara otomatis.

## Cara Menggunakan (Singkat)

1. **Clip Compiler** — tambahkan sumber video, atur durasi klip & total, lalu
   *Plan*. Tinjau hasil di dialog preview, lalu *Render*.
2. **Silence Removal** — pilih satu file video/audio, atur ambang hening &
   opsi denoise, lalu mulai. Hasil `_cleaned_*` akan muncul di folder output.
3. **Transcribe** — pilih file media, pilih model & bahasa, lalu mulai. Teks
   `.srt`/`.txt` akan disimpan di sebelah file asal.

Semua proses berjalan dalam antrean; Anda dapat memantau progres dan log langsung
di jendela aplikasi.

## Build dari Source (untuk pengembang)

```bash
git clone https://github.com/wawiwuwawu/clip-random-app.git
cd clip-random-app
pip install -r requirements.txt
python main.py
```

Untuk membangun installer secara lokal (membutuhkan [Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```powershell
.\build_local.ps1
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

## Lisensi

[MIT](LICENSE) — © 2026 wawiwuwawu (wawunime)

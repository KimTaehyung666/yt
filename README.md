# Turunin — Downloader Semua Sosmed

Tempel link dari YouTube, TikTok, Instagram, Facebook, X/Twitter, dan
1000+ situs lain (mesin: **yt-dlp**) → periksa infonya → unduh sebagai
**MP4** (pilih resolusi) atau **MP3**. Tanpa API key.

## Cara kerja

- **Periksa link (server):** yt-dlp membaca metadata — judul, thumbnail,
  durasi, pengunggah, dan resolusi yang tersedia.
- **Unduh langsung (default, tanpa simpan di server):** yt-dlp mencari
  alamat file asli, server meneruskannya sebagai pipa ke browser —
  unduhan dikelola browser dan langsung masuk folder Download. MP3 dan
  kualitas yang video+audionya terpisah (H.264) digabung sambil jalan
  memakai ffmpeg sebagai pipa (URL → stdout), tetap tanpa file simpan.
- **Mode simpan (cadangan):** untuk kualitas yang tak bisa digabung
  sambil jalan, server mengunduh dulu (job async + progres live), lalu
  file otomatis terkirim ke browser untuk disimpan.
- **Riwayat sesi:** tersimpan di localStorage browser.

## Soal hosting (penting)

yt-dlp bisa jalan di Vercel, tapi platform besar memblokir **IP cloud**
(bot check) — jadi versi web publiknya terbatas: sebagian link lolos,
YouTube hampir selalu menolak. Mode yang full adalah **lokal (Termux)**:
server jalan di HP dengan IP rumahan. Lihat **PANDUAN-TERMUX.md**.

## Deploy ke Vercel

1. Push repo ini ke GitHub, lalu di Vercel: **Add New → Project → import
   repo ini** (sudah ada `vercel.json`). Tidak ada env var yang wajib.
2. Deploy. Chip di kanan atas akan menunjukkan MODE WEB — TERBATAS.

## Jalanin lokal / Termux

```bash
pip install -r requirements.txt
python app.py   # http://localhost:5004
```

Di Termux ada installer sekali jalan: `termux-install.sh`, lalu jalankan
dengan `termux-run.sh`. Detailnya di PANDUAN-TERMUX.md.

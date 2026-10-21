# Turunin di Termux — Mode Full

Turunin memakai yt-dlp, yang bisa jalan di mana saja — masalahnya bukan
mesinnya, tapi **IP**. Platform besar (terutama YouTube) memblokir request
dari IP cloud seperti Vercel ("Sign in to confirm you're not a bot").
Di Termux, servernya jalan di HP kamu dengan IP rumahan, jadi unduhan
berjalan seperti dari browser biasa.

## Install (sekali saja)

Di Termux:

```bash
pkg install -y git
git clone https://github.com/KimTaehyung666/yt.git ~/turunin
bash ~/turunin/termux-install.sh
```

Installer memasang Python, ffmpeg (buat gabung video+audio & konversi
MP3), dan yt-dlp. **Tidak perlu API key.**

## Menjalankan

```bash
bash ~/turunin/termux-run.sh
```

Buka **http://localhost:5004** di browser HP. Kalau chip di kanan atas
bertuliskan **MODE LOKAL — FULL**, kamu siap: tempel link dari platform
apa pun → Periksa → pilih MP4/MP3 + kualitas → Download sekarang.

Catatan:

- Hasil unduhan disimpan sementara di folder job server lalu dikirim ke
  browser; simpan file dari tombol "Simpan file" supaya masuk ke galeri /
  folder Download HP.
- Konten privat / khusus pengikut tetap tidak bisa — itu batasan platform,
  bukan tool-nya.
- Biarkan Termux terbuka selama server dipakai (wake-lock sudah dipasang
  di script run).

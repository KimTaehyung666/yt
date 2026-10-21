#!/data/data/com.termux/files/usr/bin/bash
# Installer Turunin untuk Termux — SATU KALI jalan.
# Setelah ini, jalankan Turunin kapan pun dengan: bash ~/turunin/termux-run.sh
# Mode Termux = mode full: server jalan di HP kamu (IP rumahan), jadi
# platform yang memblokir IP cloud (YouTube dkk) tetap bisa diunduh.
set -e

echo "== Turunin Termux Installer =="
pkg update -y
pkg install -y python ffmpeg git

if [ -d "$HOME/turunin/.git" ]; then
  echo "-- Update Turunin yang sudah ada..."
  cd "$HOME/turunin" && git pull
else
  echo "-- Download Turunin..."
  git clone https://github.com/KimTaehyung666/yt.git "$HOME/turunin"
  cd "$HOME/turunin"
fi

pip install --upgrade pip
pip install -r requirements.txt

chmod +x termux-run.sh termux-install.sh
echo ""
echo "Selesai! Jalankan Turunin dengan:"
echo "  bash ~/turunin/termux-run.sh"
echo "Lalu buka http://localhost:5004 di browser HP kamu."
echo "(Tidak perlu API key apa pun.)"

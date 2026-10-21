#!/data/data/com.termux/files/usr/bin/bash
# Jalankan Turunin di Termux (server lokal di HP kamu).
cd "$(dirname "$0")"
termux-wake-lock 2>/dev/null || true
echo "Turunin jalan di http://localhost:5004 — buka di browser HP kamu."
echo "Chip 'MODE LOKAL — FULL' = semua platform bisa dicoba."
PORT=5004 python app.py

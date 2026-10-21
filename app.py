import os, re, glob, shutil, tempfile, threading, uuid

from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

# Mesin unduh: yt-dlp (pustaka Python) — mendukung 1000+ situs
# (YouTube, TikTok, Instagram, Facebook, X/Twitter, dll).
# Di serverless (Vercel) yt-dlp tetap bisa jalan, TAPI platform besar
# sering memblokir IP cloud (bot check) — mode yang benar-benar bisa
# diandalkan adalah lokal (Termux/PC) dengan IP rumahan.
try:
    import yt_dlp
    ENGINE_OK = True
    ENGINE_ERROR = ""
except Exception as _e:  # alasan asli disimpan biar bisa didiagnosis
    yt_dlp = None
    ENGINE_OK = False
    ENGINE_ERROR = "%s: %s" % (type(_e).__name__, _e)

HAS_FFMPEG = bool(shutil.which("ffmpeg"))
IS_SERVERLESS = bool(os.getenv("VERCEL"))
LOCAL_MODE = bool(HAS_FFMPEG and not IS_SERVERLESS)

_JOBS = {}
_JOBS_DIR = os.path.join(tempfile.gettempdir(), "turunin_jobs")
os.makedirs(_JOBS_DIR, exist_ok=True)

BOT_MARKERS = ("Sign in to confirm", "not a bot", "HTTP Error 403",
               "HTTP Error 429", "Please sign in", "login required",
               "This content isn't available")


def _friendly_error(msg):
    m = str(msg)
    if any(k.lower() in m.lower() for k in BOT_MARKERS):
        return ("Platform memblokir akses dari IP server ini (bot check / "
                "perlu login). Ini batasan IP cloud — jalankan Turunin di "
                "Termux (IP rumahan) supaya bisa. Detail: " + m[:160])
    if "Unsupported URL" in m:
        return "Link ini tidak didukung yt-dlp. Coba link video/post langsung, bukan link profil atau halaman utama."
    if "Private video" in m or "private" in m.lower():
        return "Konten ini privat / khusus pengikut — tidak bisa diunduh tanpa login pemilik akun."
    return m[:300]


def _clean_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(s or ""))


def _fmt_dur(sec):
    if not sec:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/capabilities")
def capabilities():
    return jsonify({
        "engine": ENGINE_OK,
        "engine_error": ENGINE_ERROR,
        "ffmpeg": HAS_FFMPEG,
        "local_mode": LOCAL_MODE,
        "serverless": IS_SERVERLESS,
    })


def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if extra:
        opts.update(extra)
    return opts


@app.route("/api/info", methods=["POST"])
def info():
    if not ENGINE_OK:
        return jsonify({"error": "Mesin yt-dlp tidak tersedia di server ini. Sebab: " + ENGINE_ERROR}), 500
    data = request.get_json(force=True, silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify({"error": "Tempel link yang valid (diawali http/https)."}), 400
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            inf = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": _friendly_error(e)}), 502
    if inf.get("_type") == "playlist":
        entries = list(inf.get("entries") or [])
        if entries:
            inf = entries[0]
    heights = sorted({f.get("height") for f in (inf.get("formats") or [])
                      if f.get("height")}, reverse=True)
    heights = [h for h in heights if h and h <= 2160]
    return jsonify({
        "title": inf.get("title") or "(tanpa judul)",
        "platform": inf.get("extractor_key") or inf.get("extractor") or "?",
        "uploader": inf.get("uploader") or inf.get("channel") or "",
        "duration": inf.get("duration"),
        "duration_label": _fmt_dur(inf.get("duration")),
        "thumbnail": inf.get("thumbnail") or "",
        "heights": heights,
        "is_audio_only": bool(inf.get("is_live")) is False and not heights and bool(inf.get("formats")),
    })


def _format_spec(kind, quality):
    """Bangun format string yt-dlp + postprocessor sesuai pilihan user."""
    h = None
    if quality and str(quality).isdigit():
        h = int(quality)
    if kind == "mp3":
        if HAS_FFMPEG:
            return "ba/b", [{"key": "FFmpegExtractAudio",
                              "preferredcodec": "mp3",
                              "preferredquality": "192"}]
        return "ba[ext=m4a]/ba/b", []
    if HAS_FFMPEG:
        # Utamakan H.264 + AAC (paling kompatibel di semua pemutar/editor);
        # kalau tidak ada, terima codec apa pun yang tersedia.
        if h:
            return ("bv*[height<=%d][vcodec^=avc1]+ba[ext=m4a]/"
                    "bv*[height<=%d][vcodec^=avc1]+ba/"
                    "bv*[height<=%d]+ba/b[height<=%d]/b" % (h, h, h, h)), []
        return ("bv*[vcodec^=avc1]+ba[ext=m4a]/bv*+ba/b"), []
    # Tanpa ffmpeg tidak bisa gabung stream terpisah: ambil file tunggal.
    if h:
        return ("b[height<=%d]/b" % h), []
    return "b/best", []


def _run_job(job_id, url, kind, quality):
    job = _JOBS[job_id]
    workdir = os.path.join(_JOBS_DIR, job_id)
    os.makedirs(workdir, exist_ok=True)

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            job["phase"] = "Mengunduh…"
            job["percent"] = round(got * 100 / total, 1) if total else None
            job["speed"] = _clean_ansi(d.get("_speed_str", "")).strip()
            job["eta"] = _clean_ansi(d.get("_eta_str", "")).strip()
        elif d.get("status") == "finished":
            job["phase"] = "Mengunduh selesai, memproses file…"
            job["percent"] = 100

    fmt, post = _format_spec(kind, quality)
    opts = _ydl_opts({
        "format": fmt,
        "outtmpl": os.path.join(workdir, "%(title).80s [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
    })
    if post:
        opts["postprocessors"] = post
    if HAS_FFMPEG and kind == "mp4":
        opts["merge_output_format"] = "mp4"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        job["status"] = "error"
        job["error"] = _friendly_error(e)
        job["phase"] = "Gagal"
        return
    files = [f for f in glob.glob(os.path.join(workdir, "*"))
             if os.path.isfile(f) and not f.endswith((".part", ".ytdl"))]
    if not files:
        job["status"] = "error"
        job["error"] = "Download selesai tapi file hasil tidak ditemukan."
        job["phase"] = "Gagal"
        return
    files.sort(key=os.path.getmtime)
    f = files[-1]
    job["file"] = {"path": f, "name": os.path.basename(f),
                    "size": os.path.getsize(f)}
    job["phase"] = "Selesai"
    job["status"] = "done"


@app.route("/api/jobs", methods=["POST"])
def job_create():
    if not ENGINE_OK:
        return jsonify({"error": "Mesin yt-dlp tidak tersedia di server ini. Sebab: " + ENGINE_ERROR}), 500
    data = request.get_json(force=True, silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify({"error": "Link tidak valid."}), 400
    kind = "mp3" if data.get("kind") == "mp3" else "mp4"
    quality = str(data.get("quality") or "best")
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"status": "working", "phase": "Menyiapkan…",
                     "percent": None, "speed": "", "eta": "",
                     "error": "", "file": None}
    threading.Thread(target=_run_job, args=(job_id, url, kind, quality),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = _JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job tidak ditemukan (server mungkin restart)."}), 404
    out = {k: job[k] for k in ("status", "phase", "percent", "speed", "eta", "error")}
    if job.get("file"):
        out["file"] = {"name": job["file"]["name"], "size": job["file"]["size"]}
    return jsonify(out)


@app.route("/api/jobs/<job_id>/file")
def job_file(job_id):
    job = _JOBS.get(job_id)
    if not job or not job.get("file"):
        return jsonify({"error": "File tidak ditemukan."}), 404
    f = job["file"]
    return send_file(f["path"], as_attachment=True, download_name=f["name"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5004)),
            debug=os.environ.get("DEBUG") == "1")

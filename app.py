import json, os, re, glob, shutil, tempfile, threading, time, uuid

from flask import Flask, render_template, request, jsonify, send_file, Response

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
                "perlu login). Ini batasan IP cloud — jalankan Unduhin di "
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
        "rapidapi": _rapidapi_configured(),
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
        fb = _api_info_fallback(url)
        if fb:
            return jsonify(fb)
        ig = _ig_info_fallback(url)
        if ig:
            return jsonify(ig)
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
            job["downloaded"] = _clean_ansi(d.get("_downloaded_bytes_str", "")).strip()
            job["total"] = _clean_ansi(d.get("_total_bytes_str", "")).strip()
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
    out = {k: job.get(k) for k in ("status", "phase", "percent", "speed", "eta", "error", "downloaded", "total")}
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


# =====================================================================
# MODE LANGSUNG (streaming) — server TIDAK menyimpan file.
# yt-dlp mencari alamat file asli, lalu server meneruskannya sebagai
# pipa ke browser: unduhan dikelola browser dan langsung masuk folder
# Download. MP3 & gabungan video+audio terpisah memakai ffmpeg sebagai
# pipa juga (input URL -> stdout) — tetap tanpa file simpan.
# =====================================================================
import subprocess, urllib.request, urllib.error, urllib.parse


def _safe_filename(title, ext):
    name = re.sub(r"[^\w\s.\-]", "", str(title or "unduhin"), flags=re.UNICODE).strip()
    name = re.sub(r"\s+", " ", name)[:80].strip() or "unduhin"
    return name + "." + ext


def _disp(filename):
    quoted = urllib.parse.quote(filename)
    ascii_fb = filename.encode("ascii", "ignore").decode() or "unduhin"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_fb, quoted)


def _extract(url):
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        inf = ydl.extract_info(url, download=False)
    if inf.get("_type") == "playlist":
        entries = list(inf.get("entries") or [])
        if entries:
            inf = entries[0]
    return inf


def _pick_stream_plan(inf, kind, quality):
    """Return (plan | None, alasan). Plan: proxy / ffmpeg_mp3 / ffmpeg_merge."""
    fmts = [f for f in (inf.get("formats") or []) if f.get("url")]
    if not fmts:
        return None, "Platform tidak memberi alamat file langsung untuk link ini."
    h = int(quality) if str(quality or "").isdigit() else None

    # Catatan: extractor generik (file langsung) sering tidak mengisi
    # vcodec/acodec (None). Itu dianggap "ada" — hanya nilai "none"
    # eksplisit yang berarti stream-nya memang tidak ada.
    def has_v(f):
        return f.get("vcodec") != "none"

    def has_a(f):
        return f.get("acodec") != "none"

    def audio_only(f):
        return f.get("vcodec") == "none" and has_a(f)

    def video_only(f):
        return f.get("acodec") == "none" and has_v(f)

    if kind == "mp3":
        auds = [f for f in fmts if audio_only(f)] or [f for f in fmts if has_a(f)]
        if not auds:
            return None, "Tidak ada stream audio untuk link ini."
        best = max(auds, key=lambda f: f.get("abr") or 0)
        if HAS_FFMPEG:
            return {"type": "ffmpeg_mp3", "fmt": best}, ""
        return {"type": "proxy", "fmt": best}, ""

    videos = [f for f in fmts if has_v(f)]
    prog = [f for f in videos if has_a(f)]
    if h:
        capped = [f for f in prog if (f.get("height") or 0) <= h]
        if capped:
            prog = capped
    if prog:
        prog.sort(key=lambda f: ((f.get("height") or 0),
                                 1 if f.get("ext") == "mp4" else 0), reverse=True)
        return {"type": "proxy", "fmt": prog[0]}, ""
    if HAS_FFMPEG:
        vids = [f for f in videos if video_only(f)]
        if h:
            capped = [f for f in vids if (f.get("height") or 0) <= h]
            if capped:
                vids = capped
        vids.sort(key=lambda f: ((f.get("height") or 0),
                                 1 if str(f.get("vcodec", "")).startswith("avc1") else 0),
                  reverse=True)
        auds = [f for f in fmts if audio_only(f)]
        if vids and auds and str(vids[0].get("vcodec", "")).startswith("avc1"):
            auds.sort(key=lambda f: (1 if f.get("ext") == "m4a" else 0,
                                     f.get("abr") or 0), reverse=True)
            return {"type": "ffmpeg_merge", "vfmt": vids[0], "afmt": auds[0]}, ""
    return None, ("Di kualitas ini video & audionya terpisah dan tidak bisa "
                  "digabung sambil jalan. Mode simpan dipakai untuk kualitas ini.")


def _resolve(url, kind, quality):
    inf = _extract(url)
    plan, reason = _pick_stream_plan(inf, kind, quality)
    return plan, reason, inf.get("title") or "unduhin"


def _proxy_gen(fmt):
    req = urllib.request.Request(fmt["url"], headers=dict(fmt.get("http_headers") or {}))
    with urllib.request.urlopen(req, timeout=30) as resp:
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            yield chunk


def _proxy_url_gen(url, headers=None):
    req = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(req, timeout=30) as resp:
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            yield chunk


def _ffmpeg_gen(args):
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(262144)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def _ffmpeg_header_args(fmt):
    hdrs = fmt.get("http_headers") or {}
    if not hdrs:
        return []
    blob = "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items())
    return ["-headers", blob]


_CTYPE = {"mp4": "video/mp4", "webm": "video/webm", "m4a": "audio/mp4",
          "mp3": "audio/mpeg", "opus": "audio/opus", "ogg": "audio/ogg"}


# Tiket stream: hasil resolve (alamat file asli) disimpan sebentar di
# server supaya /api/stream bisa langsung mengalirkan file tanpa
# mengulang extract_info ke platform (dulu dua kali — terasa lambat).
_STREAM_TOKENS = {}


def _token_store(plan, title):
    now = time.time()
    for t in [t for t, v in _STREAM_TOKENS.items() if v["exp"] < now]:
        _STREAM_TOKENS.pop(t, None)
    tok = uuid.uuid4().hex[:16]
    _STREAM_TOKENS[tok] = {"plan": plan, "title": title, "exp": now + 600}
    return tok


@app.route("/api/stream/check", methods=["POST"])
def stream_check():
    if not ENGINE_OK:
        return jsonify({"error": "Mesin yt-dlp tidak tersedia di server ini. Sebab: " + ENGINE_ERROR}), 500
    data = request.get_json(force=True, silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify({"error": "Link tidak valid."}), 400
    kind = data.get("kind") if data.get("kind") in ("mp3", "image") else "mp4"
    quality = str(data.get("quality") or "best")
    if kind == "image":
        iplan = _ig_image_plan(url)
        if iplan:
            plan, title = iplan
            return jsonify({"mode": "stream", "via": "image",
                            "token": _token_store(plan, title)})
        return jsonify({"error": "Link ini bukan postingan foto Instagram yang gambarnya bisa diambil."}), 422
    plan, reason, title, err = None, "", "", None
    try:
        plan, reason, title = _resolve(url, kind, quality)
    except Exception as e:
        err = e
    if not plan:
        api = _api_plan_for(url, kind, quality)
        if api:
            plan, title = api
    if not plan:
        if err is not None:
            return jsonify({"error": _friendly_error(err)}), 502
        return jsonify({"mode": "job", "reason": reason})
    return jsonify({"mode": "stream", "via": plan["type"],
                    "token": _token_store(plan, title)})


@app.route("/api/stream")
def stream():
    url = str(request.args.get("url") or "").strip()
    kind = "mp3" if request.args.get("kind") == "mp3" else "mp4"
    quality = str(request.args.get("quality") or "best")

    def err_page(msg):
        return Response(
            "<!doctype html><meta charset=utf-8><body style='background:#0b0d12;color:#e8eaf2;"
            "font-family:sans-serif;padding:28px'><h3>Mode langsung tidak bisa untuk link ini</h3>"
            "<p style='color:#8a91a5'>" + re.sub(r"<[^>]+>", "", str(msg)) + "</p>"
            "<p><a href='/' style='color:#8994ff'>&larr; Balik ke Unduhin</a> — coba lagi, "
            "atau pakai kualitas lain.</p></body>", status=502, mimetype="text/html")

    if not ENGINE_OK:
        return err_page("Mesin yt-dlp tidak tersedia. Sebab: " + ENGINE_ERROR)
    tok = str(request.args.get("t") or "")
    entry = _STREAM_TOKENS.get(tok)
    if entry and entry["exp"] > time.time():
        plan, title = entry["plan"], entry["title"]
    else:
        if not re.match(r"^https?://", url):
            return err_page("Link tidak valid.")
        try:
            plan, reason, title = _resolve(url, kind, quality)
        except Exception as e:
            return err_page(_friendly_error(e))
        if not plan:
            return err_page(reason)

    if plan["type"] == "image":
        resp = Response(_proxy_url_gen(plan["url"], {"User-Agent": "Mozilla/5.0"}),
                        mimetype="image/jpeg")
        resp.headers["Content-Disposition"] = _disp(_safe_filename(title, "jpg"))
        return resp

    if plan["type"] == "api_link":
        # Link dari API eksternal. Di serverless browser diarahkan
        # langsung ke link-nya (paling ringan & tahan file besar);
        # di lokal diteruskan sebagai pipa agar nama file rapi.
        if plan.get("redirect"):
            redir = Response(status=302)
            redir.headers["Location"] = plan["url"]
            return redir
        resp = Response(_proxy_url_gen(plan["url"]),
                        mimetype=_CTYPE.get(plan.get("ext") or "mp4", "application/octet-stream"))
        resp.headers["Content-Disposition"] = _disp(_safe_filename(title, plan.get("ext") or "mp4"))
        return resp

    if plan["type"] == "proxy":
        fmt = plan["fmt"]
        ext = fmt.get("ext") or ("mp3" if kind == "mp3" else "mp4")
        resp = Response(_proxy_gen(fmt), mimetype=_CTYPE.get(ext, "application/octet-stream"))
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if size:
            resp.headers["Content-Length"] = str(int(size))
        resp.headers["Content-Disposition"] = _disp(_safe_filename(title, ext))
        return resp

    if plan["type"] == "ffmpeg_mp3":
        fmt = plan["fmt"]
        args = [shutil.which("ffmpeg"), "-v", "error"] + _ffmpeg_header_args(fmt) + [
            "-i", fmt["url"], "-vn", "-codec:a", "libmp3lame", "-b:a", "192k",
            "-f", "mp3", "pipe:1"]
        resp = Response(_ffmpeg_gen(args), mimetype="audio/mpeg")
        resp.headers["Content-Disposition"] = _disp(_safe_filename(title, "mp3"))
        return resp

    # ffmpeg_merge: gabung video H.264 + audio jadi MP4 terfragmentasi via pipa
    vf, af = plan["vfmt"], plan["afmt"]
    args = [shutil.which("ffmpeg"), "-v", "error"] + _ffmpeg_header_args(vf) + [
        "-i", vf["url"]] + _ffmpeg_header_args(af) + [
        "-i", af["url"], "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1"]
    resp = Response(_ffmpeg_gen(args), mimetype="video/mp4")
    resp.headers["Content-Disposition"] = _disp(_safe_filename(title, "mp4"))
    return resp


# =====================================================================
# API EKSTERNAL (RapidAPI) — cadangan khusus YouTube saat ekstrak lokal
# diblokir platform (kasus khas IP cloud/Vercel). Server penyedia API
# yang mengambil video; Unduhin menerima link file jadinya.
# Aktif hanya bila env RAPIDAPI_KEY diisi. Default penyedia:
# youtube-search-download3 (RapidAPI) — bisa diganti lewat env
# RAPIDAPI_HOST & RAPIDAPI_ENDPOINT (placeholder {id}).
# =====================================================================
_API_CACHE = {}  # video_id -> (links, exp)


def _rapidapi_configured():
    return bool(os.environ.get("RAPIDAPI_KEY"))


def _yt_video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})",
                  str(url or ""))
    return m.group(1) if m else None


def _oembed_info(url):
    try:
        api = "https://www.youtube.com/oembed?url=%s&format=json" % urllib.parse.quote(url, safe="")
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        return {"title": d.get("title"), "author": d.get("author_name"),
                "thumbnail": d.get("thumbnail_url")}
    except Exception:
        return None


def _walk_strings(node, path, out):
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_strings(v, path + "/" + str(k), out)
    elif isinstance(node, list):
        for v in node:
            _walk_strings(v, path, out)
    elif isinstance(node, str):
        out.append((path, node))


_ITAG_H = {"18": 360, "22": 720, "133": 240, "134": 360, "135": 480,
           "136": 720, "137": 1080, "160": 144, "140": 0}


_AUDIO_ITAGS = {"139", "140", "141", "249", "250", "251"}
_PROG_ITAGS = {"18", "22", "37", "43", "59", "78"}  # video+audio gabung


def _harvest_links(payload):
    """Panen link media dari JSON API mana pun secara toleran.
    Return {'mp4': {height: url}, 'mp4_prog': {height: url},
    'mp3': url|None, 'title': str|None} atau None."""
    items = []
    _walk_strings(payload, "", items)
    mp4, mp4_prog, mp3, title = {}, {}, None, None
    for path, s in items:
        low = path.lower()
        if low.endswith("/title") and len(s) > 3 and not s.startswith("http"):
            title = title or s
            continue
        if not s.startswith(("http://", "https://")):
            continue
        sl = s.lower()
        is_media = ("googlevideo" in sl or "videoplayback" in sl or "mime=" in sl
                    or re.search(r"\.(mp4|m4a|mp3|webm|opus)(\?|#|$)", sl))
        if not is_media:
            continue
        mitag = re.search(r"itag=(\d+)", sl)
        itag = mitag.group(1) if mitag else None
        is_audio = (itag in _AUDIO_ITAGS or "mp3" in low or "audio" in low
                    or "mime=audio" in sl or ".mp3" in sl or ".m4a" in sl)
        if is_audio:
            if mp3 is None or ".mp3" in sl:
                mp3 = s
            continue
        h = 0
        hay = path + " " + s
        mh = re.search(r"(?<!\d)(144|240|360|480|720|1080|1440|2160)(?!\d)", hay)
        if mh:
            h = int(mh.group(1))
        elif itag:
            h = _ITAG_H.get(itag, 0)
            if not h:
                mres = re.search(r"(\d{3,4})p", hay)
                h = int(mres.group(1)) if mres else 0
        if h not in mp4:
            mp4[h] = s
        if itag in _PROG_ITAGS and h not in mp4_prog:
            mp4_prog[h] = s
    if not mp4 and not mp3:
        return None
    return {"mp4": mp4, "mp4_prog": mp4_prog, "mp3": mp3, "title": title}


def _rapidapi_fetch_links(vid):
    now = time.time()
    hit = _API_CACHE.get(vid)
    if hit and hit[1] > now:
        return hit[0]
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        return None
    host = os.environ.get("RAPIDAPI_HOST", "ytstream-download-youtube-videos.p.rapidapi.com")
    endpoint = os.environ.get("RAPIDAPI_ENDPOINT", "/dl?id={id}")
    api_url = "https://" + host + endpoint.replace("{id}", urllib.parse.quote(vid))
    try:
        req = urllib.request.Request(api_url, headers={
            "x-rapidapi-key": key, "x-rapidapi-host": host,
            "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    links = _harvest_links(payload)
    if links:
        _API_CACHE[vid] = (links, now + 600)
    return links


def _api_plan_for(url, kind, quality):
    vid = _yt_video_id(url)
    if not vid or not _rapidapi_configured():
        return None
    links = _rapidapi_fetch_links(vid)
    if not links:
        return None
    title = links.get("title")
    if not title:
        oe = _oembed_info("https://www.youtube.com/watch?v=" + vid) or {}
        title = oe.get("title") or "unduhin"
    if kind == "mp3":
        u = links.get("mp3")
        if not u:
            return None
        return {"type": "api_link", "url": u, "ext": "mp3",
                "redirect": IS_SERVERLESS}, title
    # Utamakan format progresif (video+audio gabung, mis. itag 18/22).
    # Format adaptif dari API (1080p dst.) umumnya video SAJA tanpa
    # suara — mengunduhnya mentah menghasilkan video bisu.
    mp4 = links.get("mp4_prog") or links.get("mp4") or {}
    if not mp4:
        return None
    heights = sorted(mp4.keys(), reverse=True)
    h = int(quality) if str(quality or "").isdigit() else None
    pick = heights[0]
    if h:
        under = [x for x in heights if x and x <= h]
        if under:
            pick = under[0]
    u = mp4[pick]
    m = re.search(r"\.(mp4|webm)(\?|#|$)", u.lower())
    ext = m.group(1) if m else "mp4"
    return {"type": "api_link", "url": u, "ext": ext,
            "redirect": IS_SERVERLESS}, title


def _api_info_fallback(url):
    vid = _yt_video_id(url)
    if not vid or not _rapidapi_configured():
        return None
    oe = _oembed_info(url) or {}
    links = _rapidapi_fetch_links(vid) or {}
    title = links.get("title") or oe.get("title")
    if not title and not links:
        return None
    return {
        "title": title or "(tanpa judul)",
        "platform": "YouTube (via API)",
        "uploader": oe.get("author") or "",
        "duration": None,
        "duration_label": "-",
        "thumbnail": oe.get("thumbnail") or "",
        "heights": sorted((links.get("mp4") or {}).keys(), reverse=True),
        "is_audio_only": False,
        "via_api": True,
    }


# =====================================================================
# FOTO INSTAGRAM — yt-dlp menolak postingan foto ("No video formats
# found"), jadi gambar diambil dari meta pratinjau publik (og:image)
# yang Instagram sajikan untuk perayap tautan. Resolusi mengikuti
# pratinjau publik (640px); carousel hanya terbaca gambar sampulnya.
# =====================================================================
def _ig_photo(url):
    if "/p/" not in str(url or ""):
        return None
    m = re.search(r"instagram\.com/p/([A-Za-z0-9_-]+)", str(url or ""))
    if not m:
        return None
    page = "https://www.instagram.com/p/%s/" % m.group(1)
    for ua in ("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
               "Twitterbot/1.0"):
        try:
            req = urllib.request.Request(page, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=25) as resp:
                html = resp.read().decode("utf-8", "replace")
        except Exception:
            continue
        og = re.search(r'property="og:image" content="([^"]+)"', html)
        if not og:
            continue
        img = og.group(1).replace("&amp;", "&")
        import html as _html_lib
        t = re.search(r'property="og:title" content="([^"]+)"', html)
        raw = _html_lib.unescape(t.group(1)) if t else ""
        uploader = ""
        mu = re.match(r"(.+?) on Instagram:", raw)
        if mu:
            uploader = mu.group(1).strip()
        title = re.sub(r"^.*? on Instagram:\s*", "", raw).strip().strip('"')
        title = re.sub(r"\s+•\s+Instagram.*$", "", title)[:70].strip() or "Foto Instagram"
        return {"image_url": img, "title": title, "uploader": uploader}
    return None


def _ig_info_fallback(url):
    ph = _ig_photo(url)
    if not ph:
        return None
    return {
        "title": ph["title"],
        "platform": "Instagram",
        "uploader": ph.get("uploader") or "",
        "duration": None,
        "duration_label": "-",
        "thumbnail": ph["image_url"],
        "heights": [],
        "is_audio_only": False,
        "is_image": True,
    }


def _ig_image_plan(url):
    ph = _ig_photo(url)
    if not ph:
        return None
    return {"type": "image", "url": ph["image_url"]}, ph["title"]


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5004)),
            debug=os.environ.get("DEBUG") == "1")

import base64
import gc
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

# ----------------------------------------------------------------------------
# Konfiguracja
# ----------------------------------------------------------------------------
MAX_FRAMES = max(2, int(os.environ.get("MAX_FRAMES", "90")))       # limit klatek na jedno uruchomienie
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1024"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "projekt_uploads")
UPLOAD_TTL_S = 2 * 3600
UPLOAD_KEEP = 4

ENCODE_LOCK = threading.Lock()   # jedno kodowanie naraz: czasy kodowania nie zaleza od innych zadan
INFO_CACHE = {}
_HELP_TEXT = None
_ENCODERS = None


# ----------------------------------------------------------------------------
# FFmpeg: pomocnicze
# ----------------------------------------------------------------------------
def run_cmd(cmd, timeout=None):
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Przekroczono limit czasu polecenia ffmpeg/ffprobe.")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-600:])
    return proc


def ffmpeg_version():
    try:
        return run_cmd(["ffmpeg", "-hide_banner", "-version"]).stdout.splitlines()[0]
    except Exception as e:  # noqa: BLE001
        return f"brak ffmpeg ({e})"


def passthrough_args():
    """-vsync 0 zostalo usuniete w nowych FFmpeg; -fps_mode istnieje od 5.1."""
    global _HELP_TEXT
    if _HELP_TEXT is None:
        try:
            _HELP_TEXT = run_cmd(["ffmpeg", "-hide_banner", "-h", "full"]).stdout
        except Exception:  # noqa: BLE001
            _HELP_TEXT = ""
    if "-fps_mode" in _HELP_TEXT:
        return ["-fps_mode", "passthrough"]
    return ["-vsync", "0"]


def get_available_encoders():
    global _ENCODERS
    if _ENCODERS is None:
        try:
            out = run_cmd(["ffmpeg", "-hide_banner", "-encoders"]).stdout
        except Exception:  # noqa: BLE001
            out = ""
        av1 = "libaom-av1" if "libaom-av1" in out else ("libsvtav1" if "libsvtav1" in out else None)
        _ENCODERS = {
            "H.264": "libx264" if "libx264" in out else None,
            "H.265": "libx265" if "libx265" in out else None,
            "AV1": av1,
        }
    return _ENCODERS


# ----------------------------------------------------------------------------
# Wgrane pliki (jeden upload zamiast uploadu przy kazdym zapytaniu)
# ----------------------------------------------------------------------------
def safe_name(name):
    base = os.path.basename(name or "video")
    base = re.sub(r"[^\w.\-]+", "_", base)
    return base or "video"


def cleanup_uploads():
    try:
        dirs = [os.path.join(UPLOAD_DIR, d) for d in os.listdir(UPLOAD_DIR)]
    except FileNotFoundError:
        return
    dirs = [d for d in dirs if os.path.isdir(d)]
    dirs.sort(key=os.path.getmtime, reverse=True)
    now = time.time()
    for i, d in enumerate(dirs):
        if i >= UPLOAD_KEEP or now - os.path.getmtime(d) > UPLOAD_TTL_S:
            shutil.rmtree(d, ignore_errors=True)


def save_upload(f):
    fid = uuid.uuid4().hex
    d = os.path.join(UPLOAD_DIR, fid)
    os.makedirs(d, exist_ok=True)
    name = safe_name(f.filename)
    path = os.path.join(d, name)
    f.save(path)
    cleanup_uploads()
    return fid, path, name


def find_upload(fid):
    if not re.fullmatch(r"[0-9a-f]{32}", fid or ""):
        raise FileNotFoundError("Nieprawidlowy identyfikator pliku.")
    d = os.path.join(UPLOAD_DIR, fid)
    files = os.listdir(d) if os.path.isdir(d) else []
    if not files:
        raise FileNotFoundError("Plik wygasl na serwerze (restart lub limit czasu). Wybierz plik ponownie.")
    os.utime(d, None)  # przedluz zycie
    return os.path.join(d, files[0]), files[0]


def resolve_input():
    """Zwraca (sciezka, nazwa, file_id). file_id jest None dla plikow z repozytorium."""
    f = request.files.get("file")
    if f and f.filename:
        fid, path, name = save_upload(f)
        return path, name, fid
    fid = request.form.get("file_id", "")
    if fid:
        path, name = find_upload(fid)
        return path, name, fid
    repo = os.path.basename(request.form.get("repo_file") or "hall_monitor.y4m")
    path = os.path.join("sekwencje", repo)
    if os.path.isfile(path):
        return os.path.abspath(path), repo, None
    raise FileNotFoundError(f"Nie znaleziono pliku {repo} w folderze sekwencje/. Wgraj plik wideo z dysku.")


# ----------------------------------------------------------------------------
# Informacje o pliku
# ----------------------------------------------------------------------------
def parse_rate(s):
    try:
        s = str(s)
        if "/" in s:
            n, d = s.split("/", 1)
            d = float(d)
            return float(n) / d if d > 0 else 0.0
        return float(s)
    except Exception:  # noqa: BLE001
        return 0.0


def probe_info(path):
    key = (path, os.path.getmtime(path), os.path.getsize(path))
    if key in INFO_CACHE:
        return INFO_CACHE[key]

    out = run_cmd([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration:format=duration",
        "-of", "json", path,
    ], timeout=60).stdout
    j = json.loads(out)
    streams = j.get("streams") or []
    if not streams:
        raise RuntimeError("W pliku nie znaleziono strumienia wideo.")
    st, fmt = streams[0], j.get("format") or {}

    fps = parse_rate(st.get("r_frame_rate", ""))
    if not 1 <= fps <= 240:
        fps = parse_rate(st.get("avg_frame_rate", ""))
    if not 1 <= fps <= 240:
        fps = 25.0

    nb = str(st.get("nb_frames", ""))
    count = int(nb) if nb.isdigit() else 0
    if count <= 0:  # np. y4m: liczymy klatki (tylko gdy kontener nie podaje liczby)
        try:
            o2 = run_cmd([
                "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_frames", "-of", "json", path,
            ], timeout=120).stdout
            nb2 = str((json.loads(o2).get("streams") or [{}])[0].get("nb_read_frames", ""))
            count = int(nb2) if nb2.isdigit() else 0
        except Exception:  # noqa: BLE001
            count = 0

    dur_raw = st.get("duration") or fmt.get("duration")
    try:
        dur_val = float(dur_raw)
    except Exception:  # noqa: BLE001
        dur_val = 0.0
    dur = count / fps if count > 0 else (dur_val if dur_val > 0 else 10.0)

    info = {"w": int(st.get("width") or 0), "h": int(st.get("height") or 0),
            "fps": round(fps, 3), "dur": round(dur, 3), "count": count}
    INFO_CACHE[key] = info
    return info


def jpeg_size(raw):
    i, n = 2, len(raw)
    while i + 9 < n:
        if raw[i] != 0xFF:
            break
        marker = raw[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", raw[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7):
            i += 2
        else:
            i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
    return 0, 0


def jpg_list_to_data_urls(pattern):
    urls, w, h = [], 0, 0
    for idx, path in enumerate(sorted(glob.glob(pattern))):
        with open(path, "rb") as fp:
            raw = fp.read()
        if idx == 0:
            w, h = jpeg_size(raw)
        urls.append("data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"))
        os.remove(path)
    return urls, w, h


# ----------------------------------------------------------------------------
# Potok przetwarzania: kazda sciezka (wzorzec, czyste zrodlo, podglad) powstaje
# z TEGO SAMEGO planu: ten sam poczatek, ta sama liczba klatek N, te same wymiary.
# ----------------------------------------------------------------------------
def plan_frames(info, start, dlugosc, fps):
    """Zwraca N = liczbe klatek wyjsciowych (kazda wyjsciowa klatka zuzywa 2 klatki zrodla)."""
    out_fps = fps / 2.0
    n = min(int(round(dlugosc * out_fps)), MAX_FRAMES)
    if info.get("count", 0) > 0:
        start_frame = int(round(start * info["fps"]))
        n = min(n, max(0, info["count"] - start_frame) // 2)
    if n < 2:
        raise ValueError("Za krotki fragment: zmniejsz poczatek lub zwieksz dlugosc.")
    return n, out_fps


def scale_chain(szer):
    # Wymiary zawsze podzielne przez 4 (libx264 w trybie przeplotu tego wymaga).
    if szer > 0:
        w = max(4, szer - szer % 4)
        sc = f"scale={w}:trunc({w}*ih/iw/4)*4:flags=bicubic"
    else:
        sc = "scale=trunc(iw/4)*4:trunc(ih/4)*4:flags=bicubic"
    return [sc, "setsar=1", "format=yuv420p"]


def timeline(out_fps):
    return f"setpts=N/({out_fps:.6f}*TB)"


def filters_for(tryb, szer, out_fps, kolej, lowpass):
    """Lancuch -vf dla sciezki progressive (co druga klatka zrodla)."""
    vf = scale_chain(szer) + ["framestep=2", timeline(out_fps)]
    return ",".join(vf)


def weave_graph(szer, out_fps, kolej, lowpass):
    """Przeplot zlozony JAWNIE z dwooch strumieni: klatki parzyste (0,2,4..) i nieparzyste (1,3,5..).

    Klatka wyjsciowa k = polklatki z klatek zrodla 2k i 2k+1. Nie korzystamy z filtru 'interlace', bo jego
    tempo wyjsciowe zalezy od wersji FFmpeg (na niektorych buildach wzorzec jest spowolniony 2x).
    Bez filtru dolnoprzepustowego wynik jest bit-w-bit identyczny z 'interlace'; z filtrem rozni sie
    co najwyzej o 1 poziom jasnosci (filtr pionowy 1-2-1 po sasiednich liniach, jak w tinterlace).
    """
    lp = ""
    if lowpass:
        m = "0 1 0 0 2 0 0 1 0"
        lp = "," + ":".join(["convolution=0m='%s'" % m, "0rdiv=0.25",
                              "1m='%s'" % m, "1rdiv=0.25", "2m='%s'" % m, "2rdiv=0.25"])
    # tff: gorne (parzyste) linie z pierwszej klatki pary; bff: z drugiej
    ins = "[e][o]" if kolej == "tff" else "[o][e]"
    return (
        f"[0:v]{','.join(scale_chain(szer))}{lp},split=2[a][b];"
        "[a]select='eq(mod(n,2),0)',setpts=N/TB[e];"
        "[b]select='eq(mod(n,2),1)',setpts=N/TB[o];"
        f"{ins}blend=all_expr='if(eq(mod(Y,2),0),A,B)':shortest=1,{timeline(out_fps)}[v]"
    )


def reference_args(tryb, szer, out_fps, kolej, lowpass):
    if tryb == "interlaced":
        return ["-filter_complex", weave_graph(szer, out_fps, kolej, lowpass), "-map", "[v]"]
    return ["-vf", filters_for(tryb, szer, out_fps, kolej, lowpass)]


def ffmpeg_base(start):
    # -ss PRZED -i: szybkie, dokladne wyszukiwanie w zrodle (zamiast dekodowania od poczatku pliku).
    return ["ffmpeg", "-y", "-nostats", "-loglevel", "error", "-threads", "1", "-ss", f"{start:.3f}"]


def encoder_args(kodek, encs, tryb, kolej, latency, mbaff):
    a = []
    if kodek == "H.264":
        preset = "ultrafast" if latency == "realtime" else "fast"
        a += ["-c:v", "libx264", "-preset", preset]
        x = ["bframes=0", "ref=1", "rc-lookahead=0", "sync-lookahead=0", "threads=1"]
        if tryb == "interlaced" and mbaff:
            x.insert(0, "tff=1" if kolej == "tff" else "bff=1")
            a += ["-flags", "+ildct+ilme"]
        a += ["-x264-params", ":".join(x)]
    elif kodek == "H.265":
        preset = "ultrafast" if latency == "realtime" else "superfast"
        a += ["-c:v", "libx265", "-preset", preset]
        x = ["ctu=32", "min-cu-size=16", "bframes=0", "ref=1", "rc-lookahead=0", "lookahead-slices=0",
             "frame-threads=1", "pools=none", "no-sao=1", "no-weightp=1", "no-weightb=1"]
        if tryb == "interlaced":
            x.insert(0, f"interlace={kolej}")
        a += ["-x265-params", ":".join(x)]
    else:
        if encs["AV1"] == "libaom-av1":
            cpu = "8" if latency == "realtime" else "7"
            a += ["-c:v", "libaom-av1", "-usage", "realtime", "-cpu-used", cpu,
                  "-lag-in-frames", "0", "-auto-alt-ref", "0", "-arnr-maxframes", "0",
                  "-tile-columns", "0", "-tile-rows", "0", "-row-mt", "0"]
        else:
            a += ["-c:v", "libsvtav1", "-preset", "12",
                  "-svtav1-params", "lp=1:lookahead=0:pred-struct=1:tile-rows=0:tile-columns=0"]
    return a


def run_experiment(p, in_path, src_name, info):
    """p: slownik parametrow. Zwraca slownik gotowy do jsonify."""
    kodek, tryb, kbps = p["kodek"], p["tryb"], p["kbps"]
    encs = get_available_encoders()
    if not encs.get(kodek):
        raise ValueError(f"Koder {kodek} nie jest dostepny w tej instalacji FFmpeg.")

    n, out_fps = plan_frames(info, p["start"], p["dlugosc"], p["fps"])
    start_frame = int(round(p["start"] * info["fps"]))
    pt = passthrough_args()
    keep = p["keep"]

    with ENCODE_LOCK, tempfile.TemporaryDirectory() as tmp:
        ref_path = os.path.join(tmp, "ref.nut")
        out_path = os.path.join(tmp, "out.mkv")
        psnr_log = os.path.join(tmp, "psnr.log")
        ssim_log = os.path.join(tmp, "ssim.log")

        # 1) wzorzec (bezstratny, surowe wideo): dokladnie N klatek
        ref_args = reference_args(tryb, p["szer"], out_fps, p["kolej"], p["lowpass"])
        run_cmd(ffmpeg_base(p["start"]) + ["-i", in_path, "-an"] + ref_args + pt +
                ["-frames:v", str(n), "-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-f", "nut", ref_path])

        # 2) czyste zrodlo (progressive) dla widoku przeplotu: te same N klatek
        clean_urls = None
        if keep and tryb == "interlaced":
            vf_clean = filters_for("progressive", p["szer"], out_fps, p["kolej"], p["lowpass"])
            run_cmd(ffmpeg_base(p["start"]) + ["-i", in_path, "-an", "-vf", vf_clean] + pt +
                    ["-frames:v", str(n), "-q:v", "3", os.path.join(tmp, "clean_%04d.jpg")])
            clean_urls, _, _ = jpg_list_to_data_urls(os.path.join(tmp, "clean_*.jpg"))

        # 3) kodowanie
        enc_cmd = ["ffmpeg", "-y", "-nostats", "-loglevel", "error", "-threads", "1", "-i", ref_path, "-an"]
        enc_cmd += encoder_args(kodek, encs, tryb, p["kolej"], p["latency"], p["mbaff"])
        enc_cmd += pt + ["-frames:v", str(n), "-pix_fmt", "yuv420p", "-b:v", f"{kbps}k"]
        if p["bmode"] == "constant":
            enc_cmd += ["-minrate", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
        enc_cmd.append(out_path)
        t0 = time.perf_counter()
        try:
            run_cmd(enc_cmd)
        except RuntimeError as e:
            raise RuntimeError(f"Blad kodera {kodek}: {e}")
        encode_sec = time.perf_counter() - t0

        # 4) metryki: obie sciezki dostaja numer klatki jako czas, wiec para = ta sama klatka
        lavfi = (
            "[0:v]setpts=N/TB,split=2[e1][e2];"
            "[1:v]setpts=N/TB,split=2[r1][r2];"
            f"[e1][r1]psnr=stats_file={psnr_log};"
            f"[e2][r2]ssim=stats_file={ssim_log}"
        )
        run_cmd(["ffmpeg", "-y", "-nostats", "-loglevel", "error", "-threads", "1",
                 "-i", out_path, "-i", ref_path, "-lavfi", lavfi, "-f", "null", "-"])
        with open(psnr_log, "r", encoding="utf-8") as fp:
            psnr_vals = [100.0 if v == "inf" else float(v) for v in re.findall(r"psnr_avg:([0-9.]+|inf)", fp.read())]
        with open(ssim_log, "r", encoding="utf-8") as fp:
            ssim_vals = [float(v) for v in re.findall(r"All:([0-9.]+)", fp.read())]

        lengths = [n, len(psnr_vals), len(ssim_vals)]
        size_bytes = os.path.getsize(out_path)

        # 5) podglad: dokladnie te same klatki, z ktorych policzono metryki
        ref_urls = enc_urls = None
        w = h = 0
        if keep:
            run_cmd(["ffmpeg", "-y", "-nostats", "-loglevel", "error", "-threads", "1", "-i", ref_path] + pt +
                    ["-frames:v", str(n), "-q:v", "3", os.path.join(tmp, "ref_%04d.jpg")])
            run_cmd(["ffmpeg", "-y", "-nostats", "-loglevel", "error", "-threads", "1", "-i", out_path] + pt +
                    ["-frames:v", str(n), "-q:v", "3", os.path.join(tmp, "enc_%04d.jpg")])
            ref_urls, w, h = jpg_list_to_data_urls(os.path.join(tmp, "ref_*.jpg"))
            enc_urls, _, _ = jpg_list_to_data_urls(os.path.join(tmp, "enc_*.jpg"))
            lengths += [len(ref_urls), len(enc_urls)]
            if clean_urls is not None:
                lengths.append(len(clean_urls))

    # Jedna wspolna liczba klatek dla WSZYSTKIEGO (tabela, wykresy, podglad)
    m = min(lengths)
    if m < 2:
        raise RuntimeError("Nie udalo sie odczytac klatek lub metryk (wynik: " + str(lengths) + ").")
    warning = None
    if len(set(lengths)) > 1:
        warning = (f"Liczby klatek roznily sie (zadane {n}, metryki {len(psnr_vals)}/{len(ssim_vals)}"
                   + (f", podglad {len(ref_urls)}/{len(enc_urls)}" if keep else "")
                   + f"); wszystko przyciete do {m}.")

    psnr_vals, ssim_vals = psnr_vals[:m], ssim_vals[:m]
    actual_kbps = round(size_bytes * 8.0 / (m / out_fps) / 1000.0, 1)
    row = {
        "sekwencja": os.path.splitext(src_name)[0],
        "tryb": tryb,
        "kodek": kodek,
        "bitrate_zadany": kbps,
        "bitrate_rzecz": actual_kbps,
        "tempo": f"{p['latency']}/{'CBR' if p['bmode'] == 'constant' else 'VBR'}",
        "PSNR": round(sum(psnr_vals) / m, 2),
        "SSIM": round(sum(ssim_vals) / m, 4),
        "czas_kodowania_s": round(encode_sec, 2),
        "rozmiar_MB": round(size_bytes / 1e6, 3),
    }
    payload = {
        "wiersz": row, "tryb": tryb, "n": m, "n_zadane": n, "fo": out_fps, "max_frames": MAX_FRAMES,
        "start_frame": start_frame, "ostrzezenie": warning,
        "klatki": {"PSNR": psnr_vals, "SSIM": ssim_vals},
    }
    if keep:
        payload.update({
            "w": w, "h": h, "ref_urls": ref_urls[:m], "enc_urls": enc_urls[:m],
            "clean_urls": clean_urls[:m] if clean_urls is not None else None,
        })
    gc.collect()
    return payload


# ----------------------------------------------------------------------------
# Trasy
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    for candidate in ("index.html", "page.html"):
        if os.path.exists(candidate):
            return send_from_directory(".", candidate)
    if os.path.exists(os.path.join("static", "index.html")):
        return send_from_directory("static", "index.html")
    return "Brak pliku index.html lub page.html w katalogu glownym.", 404


@app.route("/sekwencje/<path:filename>")
def sequences(filename):
    if os.path.exists(os.path.join("sekwencje", filename)):
        return send_from_directory("sekwencje", filename)
    return "Nie znaleziono pliku.", 404


@app.route("/api/encoders", methods=["GET"])
def encoders_status():
    out = {k: bool(v) for k, v in get_available_encoders().items()}
    out["_ffmpeg"] = ffmpeg_version()
    return jsonify(out)


@app.route("/api/probe", methods=["POST"])
def probe_video():
    try:
        in_path, src_name, fid = resolve_input()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    try:
        info = probe_info(in_path)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Nie udalo sie odczytac parametrow wideo: {e}"}), 400
    max_dlug = round(max(1.0, min(10.0, MAX_FRAMES * 2.0 / info["fps"])), 1)
    return jsonify({"name": src_name, "file_id": fid, "max_frames": MAX_FRAMES, "max_dlug": max_dlug, **info})


def read_params():
    f = request.form
    kolej = f.get("kolej", "tff")
    tryb = f.get("tryb", "progressive")
    p = {
        "kodek": f.get("kodek", "H.264"),
        "tryb": tryb if tryb in ("progressive", "interlaced") else "progressive",
        "kbps": max(50, int(float(f.get("kbps", 1000)))),
        "latency": f.get("latency", "quality"),
        "bmode": f.get("bmode", "variable"),
        "start": max(0.0, float(f.get("start", 0))),
        "dlugosc": min(10.0, max(0.5, float(f.get("dlugosc", 3.0)))),
        "szer": max(0, int(float(f.get("szer", 854)))),
        "kolej": kolej if kolej in ("tff", "bff") else "tff",
        "lowpass": f.get("lowpass", "1") == "1",
        "mbaff": f.get("mbaff", "1") == "1",
        "fps": float(f.get("fps", 25)),
        "keep": f.get("keep", "0") == "1",
    }
    if not 1 <= p["fps"] <= 240:
        raise ValueError("Nieprawidlowa liczba klatek na sekunde.")
    return p


@app.route("/api/encode", methods=["POST"])
def encode_video():
    try:
        p = read_params()
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Nieprawidlowe parametry: {e}"}), 400
    try:
        in_path, src_name, fid = resolve_input()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    try:
        info = probe_info(in_path)
        result = run_experiment(p, in_path, src_name, info)
        result["file_id"] = fid
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ----------------------------------------------------------------------------
# Samotest synchronizacji: GET /api/selftest[?kodek=H.264]
# Generuje sekwencje, w ktorej pozycja jasnego paska = numer klatki zrodlowej,
# i sprawdza, z ktorej klatki zrodlowej pochodzi kazda klatka wzorca / po kodowaniu /
# czystego zrodla. Dzieki temu synchronizacje mozna zweryfikowac na wlasnej wersji FFmpeg.
# ----------------------------------------------------------------------------
def _bar_positions(data_url, w, h, rows_parity=None):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                          input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    img = proc.stdout
    if len(img) < w * h:
        return None
    rows = range(h) if rows_parity is None else range(rows_parity, h, 2)
    col = [0.0] * w
    for y in rows:
        base = y * w
        for x in range(w):
            col[x] += img[base + x]
    lo, hi = min(col), max(col)
    thr = (lo + hi) / 2
    xs = [x for x in range(w) if col[x] > thr]
    return sum(xs) / len(xs) if xs else None


@app.route("/api/selftest")
def selftest():
    kodek = request.args.get("kodek", "H.264")
    W, H, FPS = 160, 128, 30
    start, dlug = 0.5, 2.0
    problems, results = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "selftest.y4m")
        try:
            run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                     f"color=c=gray:s={W}x{H}:r={FPS},format=yuv420p,"
                     "geq=lum='if(between(X,N,N+5),235,100)':cb=128:cr=128",
                     "-t", "4", "-pix_fmt", "yuv420p", src])
            info = probe_info(src)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "ffmpeg": ffmpeg_version(), "blad": f"Nie udalo sie wygenerowac sekwencji: {e}"}), 500

        for tryb in ("progressive", "interlaced"):
            p = {"kodek": kodek, "tryb": tryb, "kbps": 1500, "latency": "realtime", "bmode": "variable",
                 "start": start, "dlugosc": dlug, "szer": 0, "kolej": "tff", "lowpass": True,
                 "mbaff": True, "fps": float(FPS), "keep": True}
            try:
                r = run_experiment(p, src, "selftest.y4m", info)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{tryb}: blad uruchomienia: {e}")
                continue
            n, s0 = r["n"], r["start_frame"]
            if r["n"] != r["n_zadane"]:
                problems.append(f"{tryb}: liczba klatek {r['n']} != zadana {r['n_zadane']}")
            bad = 0
            for k in range(n):
                exp_top = s0 + 2 * k + 2.5
                exp_bot = s0 + 2 * k + 1 + 2.5
                checks = []
                if tryb == "progressive":
                    checks = [("wzorzec", r["ref_urls"][k], None, exp_top), ("po kodowaniu", r["enc_urls"][k], None, exp_top)]
                else:
                    checks = [("wzorzec/gora", r["ref_urls"][k], 0, exp_top), ("wzorzec/dol", r["ref_urls"][k], 1, exp_bot),
                              ("po kodowaniu/gora", r["enc_urls"][k], 0, exp_top), ("po kodowaniu/dol", r["enc_urls"][k], 1, exp_bot),
                              ("czyste zrodlo", r["clean_urls"][k], None, exp_top)]
                for label, url, par, exp in checks:
                    got = _bar_positions(url, r["w"], r["h"], par)
                    if got is None or abs(got - exp) > 0.6:
                        bad += 1
                        if len(problems) < 12:
                            problems.append(f"{tryb} klatka {k} ({label}): oczekiwano x~{exp}, jest {got}")
            results[tryb] = {"klatek": n, "zadane": r["n_zadane"], "bledne_pomiary": bad, "ostrzezenie": r["ostrzezenie"]}
    ok = not problems and all(v["bledne_pomiary"] == 0 for v in results.values()) and len(results) == 2
    return jsonify({"ok": ok, "ffmpeg": ffmpeg_version(), "kodek": kodek, "wyniki": results, "problemy": problems})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)

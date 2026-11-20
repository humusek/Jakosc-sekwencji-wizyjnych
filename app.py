import base64
import glob
import os
import re
import struct
import subprocess
import tempfile
import time
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)


def run_cmd(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-700:])
    return proc


def get_available_encoders():
    try:
        proc = run_cmd(["ffmpeg", "-hide_banner", "-encoders"])
        out = proc.stdout
    except Exception:
        out = ""
    av1_enc = "libsvtav1" if "libsvtav1" in out else ("libaom-av1" if "libaom-av1" in out else None)
    return {
        "H.264": "libx264" if "libx264" in out else None,
        "H.265": "libx265" if "libx265" in out else None,
        "AV1": av1_enc,
    }


def resolve_input_file(tmpdir):
    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        src_name = os.path.basename(f.filename)
        in_path = os.path.join(tmpdir, src_name)
        f.save(in_path)
        return in_path, src_name
    repo_rel = request.form.get("repo_file", "sekwencje/hall_monitor.y4m")
    src_name = os.path.basename(repo_rel)
    for candidate in (repo_rel, src_name):
        if os.path.exists(candidate):
            return os.path.abspath(candidate), src_name
    raise FileNotFoundError(
        f"Nie znaleziono pliku {src_name} w repozytorium. Wgraj plik wideo z dysku."
    )


def png_list_to_data_urls(pattern):
    files = sorted(glob.glob(pattern))
    urls = []
    w, h = 0, 0
    for idx, path in enumerate(files):
        with open(path, "rb") as fp:
            raw = fp.read()
        if idx == 0 and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
        urls.append("data:image/png;base64," + base64.b64encode(raw).decode("ascii"))
    return urls, w, h


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
    if os.path.exists(filename):
        return send_from_directory(".", filename)
    return "Nie znaleziono pliku.", 404


@app.route("/api/encoders", methods=["GET"])
def encoders_status():
    encs = get_available_encoders()
    return jsonify({k: bool(v) for k, v in encs.items()})


@app.route("/api/probe", methods=["POST"])
def probe_video():
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            in_path, src_name = resolve_input_file(tmpdir)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404

        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration:format=duration",
            "-of", "default=noprint_wrappers=1", in_path
        ]
        try:
            proc = run_cmd(cmd)
        except RuntimeError as e:
            return jsonify({"error": f"Nie udalo sie odczytac parametrow wideo: {e}"}), 400

        info = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k not in info or info[k] in ("N/A", "0", "0/0", ""):
                    info[k] = v.strip()

        w = int(info.get("width", 0) or 0)
        h = int(info.get("height", 0) or 0)
        fps = 25.0
        rfr = info.get("r_frame_rate", "25/1")
        if "/" in rfr:
            num, den = rfr.split("/", 1)
            if float(den) > 0 and float(num) > 0:
                fps = float(num) / float(den)
        elif rfr not in ("", "N/A"):
            fps = float(rfr)

        dur_raw = info.get("duration", "N/A")
        nb_raw = info.get("nb_frames", "N/A")
        count = int(nb_raw) if nb_raw.isdigit() else 0
        if dur_raw not in ("", "N/A"):
            dur = float(dur_raw)
        elif count > 0:
            dur = count / fps
        else:
            dur = 10.0

        return jsonify({
            "name": src_name,
            "w": w,
            "h": h,
            "fps": round(fps, 3),
            "dur": round(dur, 3),
            "count": count
        })


@app.route("/api/encode", methods=["POST"])
def encode_video():
    kodek = request.form.get("kodek", "H.264")
    tryb = request.form.get("tryb", "progressive")
    kbps = int(request.form.get("kbps", 1000))
    latency = request.form.get("latency", "quality")
    bmode = request.form.get("bmode", "variable")
    start = max(0.0, float(request.form.get("start", 0)))
    dlugosc = max(0.5, float(request.form.get("dlugosc", 3)))
    szer = int(request.form.get("szer", 854))
    kolej = request.form.get("kolej", "tff")
    lowpass = request.form.get("lowpass", "1") == "1"
    mbaff = request.form.get("mbaff", "1") == "1"
    fps = float(request.form.get("fps", 25))
    keep = request.form.get("keep", "0") == "1"

    encs = get_available_encoders()
    if not encs.get(kodek):
        return jsonify({"error": f"Koder {kodek} nie jest dostepny w tej instalacji FFmpeg."}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            in_path, src_name = resolve_input_file(tmpdir)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 404

        ref_path = os.path.join(tmpdir, "ref.y4m")
        clean_path = os.path.join(tmpdir, "clean.y4m")
        out_path = os.path.join(tmpdir, "out.mkv")
        psnr_log = os.path.join(tmpdir, "psnr.log")
        ssim_log = os.path.join(tmpdir, "ssim.log")

        out_fps = fps / 2.0
        base_scale = [f"scale={szer}:-2:flags=bicubic"] if szer > 0 else ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]

        if tryb == "interlaced":
            lp = 1 if lowpass else 0
            ref_filters = base_scale + [f"interlace=scan={kolej}:lowpass={lp}"]
        else:
            ref_filters = base_scale + [f"select='not(mod(n\\,2))',setpts=N/({out_fps}*TB)"]

        ref_cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-t", str(dlugosc),
            "-i", in_path, "-vf", ",".join(ref_filters),
            "-pix_fmt", "yuv420p", ref_path
        ]
        try:
            run_cmd(ref_cmd)
        except RuntimeError as e:
            return jsonify({"error": f"Blad przygotowania wzorca: {e}"}), 500

        if keep and tryb == "interlaced":
            clean_filters = base_scale + [f"select='not(mod(n\\,2))',setpts=N/({out_fps}*TB)"]
            clean_cmd = [
                "ffmpeg", "-y", "-ss", str(start), "-t", str(dlugosc),
                "-i", in_path, "-vf", ",".join(clean_filters),
                "-pix_fmt", "yuv420p", clean_path
            ]
            try:
                run_cmd(clean_cmd)
            except RuntimeError as e:
                return jsonify({"error": f"Blad przygotowania czystego zrodla: {e}"}), 500

        enc_cmd = ["ffmpeg", "-y", "-i", ref_path, "-threads", "2"]

        if kodek == "H.264":
            preset = "ultrafast" if latency == "realtime" else "medium"
            enc_cmd += ["-c:v", "libx264", "-preset", preset]
            if tryb == "interlaced" and mbaff:
                parity = "tff=1" if kolej == "tff" else "bff=1"
                enc_cmd += ["-flags", "+ildct+ilme", "-x264-params", parity]
        elif kodek == "H.265":
            preset = "ultrafast" if latency == "realtime" else "medium"
            enc_cmd += ["-c:v", "libx265", "-preset", preset]
            if tryb == "interlaced":
                enc_cmd += ["-x265-params", f"interlace={kolej}"]
        elif kodek == "AV1":
            if encs["AV1"] == "libsvtav1":
                preset = "10" if latency == "realtime" else "6"
                enc_cmd += ["-c:v", "libsvtav1", "-preset", preset]
            else:
                cpu_used = "8" if latency == "realtime" else "4"
                enc_cmd += ["-c:v", "libaom-av1", "-cpu-used", cpu_used, "-row-mt", "1"]

        enc_cmd += ["-b:v", f"{kbps}k"]
        if bmode == "constant":
            enc_cmd += ["-minrate", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
        enc_cmd.append(out_path)

        t0 = time.perf_counter()
        try:
            run_cmd(enc_cmd)
        except RuntimeError as e:
            return jsonify({"error": f"Blad kodera {kodek}: {e}"}), 500
        encode_sec = time.perf_counter() - t0

        metric_cmd = [
            "ffmpeg", "-y", "-i", out_path, "-i", ref_path,
            "-lavfi", f"[0:v][1:v]psnr=stats_file={psnr_log};[0:v][1:v]ssim=stats_file={ssim_log}",
            "-f", "null", "-"
        ]
        try:
            run_cmd(metric_cmd)
        except RuntimeError as e:
            return jsonify({"error": f"Blad obliczania metryk: {e}"}), 500

        with open(psnr_log, "r", encoding="utf-8") as fp:
            psnr_vals = [100.0 if v == "inf" else float(v) for v in re.findall(r"psnr_avg:([0-9.]+|inf)", fp.read())]
        with open(ssim_log, "r", encoding="utf-

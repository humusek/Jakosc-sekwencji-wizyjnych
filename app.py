import base64
import gc
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
        raise RuntimeError(proc.stderr[-600:])
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


def jpeg_size(raw):
    i = 2
    n = len(raw)
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
            seg_len = struct.unpack(">H", raw[i + 2:i + 4])[0]
            i += 2 + seg_len
    return 0, 0


def jpg_list_to_data_urls(pattern):
    files = sorted(glob.glob(pattern))
    urls = []
    w, h = 0, 0
    for idx, path in enumerate(files):
        with open(path, "rb") as fp:
            raw = fp.read()
        if idx == 0:
            w, h = jpeg_size(raw)
        urls.append("data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"))
        os.remove(path)
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

        gc.collect()
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
    dlugosc = min(3.0, max(0.5, float(request.form.get("dlugosc", 2.5))))
    szer_raw = int(request.form.get("szer", 640))
    szer = 854 if (szer_raw == 0 or szer_raw > 854) else szer_raw
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
        base_scale = [f"scale={szer}:-2:flags=fast_bilinear"]

        if tryb == "interlaced":
            lp = 1 if lowpass else 0
            ref_filters = base_scale + [f"interlace=scan={kolej}:lowpass={lp}"]
        else:
            ref_filters = base_scale + [f"select='not(mod(n\\,2))',setpts=N/({out_fps}*TB)"]

        ref_cmd = [
            "ffmpeg", "-y", "-threads", "1", "-ss", str(start), "-t", str(dlugosc),
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
                "ffmpeg", "-y", "-threads", "1", "-ss", str(start), "-t", str(dlugosc),
                "-i", in_path, "-vf", ",".join(clean_filters),
                "-pix_fmt", "yuv420p", clean_path
            ]
            try:
                run_cmd(clean_cmd)
            except RuntimeError as e:
                return jsonify({"error": f"Blad przygotowania czystego zrodla: {e}"}), 500

        if os.path.dirname(in_path) == tmpdir and os.path.exists(in_path):
            os.remove(in_path)

        enc_cmd = ["ffmpeg", "-y", "-i", ref_path, "-threads", "1"]

        if kodek == "H.264":
            preset = "ultrafast" if latency == "realtime" else "fast"
            enc_cmd += ["-c:v", "libx264", "-preset", preset]
            x264_opts = ["rc-lookahead=5", "threads=1"]
            if tryb == "interlaced" and mbaff:
                parity = "tff=1" if kolej == "tff" else "bff=1"
                enc_cmd += ["-flags", "+ildct+ilme"]
                x264_opts.insert(0, parity)
            enc_cmd += ["-x264-params", ":".join(x264_opts)]
        elif kodek == "H.265":
            preset = "ultrafast" if latency == "realtime" else "fast"
            enc_cmd += ["-c:v", "libx265", "-preset", preset]
            x265_opts = ["rc-lookahead=5", "frame-threads=1", "pools=none"]
            if tryb == "interlaced":
                x265_opts.insert(0, f"interlace={kolej}")
            enc_cmd += ["-x265-params", ":".join(x265_opts)]
        elif kodek == "AV1":
            if encs["AV1"] == "libsvtav1":
                preset = "11" if latency == "realtime" else "8"
                enc_cmd += ["-c:v", "libsvtav1", "-preset", preset, "-svtav1-params", "lp=1:lookahead=4"]
            else:
                cpu_used = "8" if latency == "realtime" else "6"
                enc_cmd += ["-c:v", "libaom-av1", "-cpu-used", cpu_used, "-lag-in-frames", "4"]

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
            "ffmpeg", "-y", "-threads", "1", "-i", out_path, "-i", ref_path,
            "-lavfi", f"[0:v][1:v]psnr=stats_file={psnr_log};[0:v][1:v]ssim=stats_file={ssim_log}",
            "-f", "null", "-"
        ]
        try:
            run_cmd(metric_cmd)
        except RuntimeError as e:
            return jsonify({"error": f"Blad obliczania metryk: {e}"}), 500

        with open(psnr_log, "r", encoding="utf-8") as fp:
            psnr_vals = [100.0 if v == "inf" else float(v) for v in re.findall(r"psnr_avg:([0-9.]+|inf)", fp.read())]
        with open(ssim_log, "r", encoding="utf-8") as fs:
            ssim_vals = [float(v) for v in re.findall(r"All:([0-9.]+)", fs.read())]

        n_frames = len(psnr_vals)
        if n_frames == 0:
            return jsonify({"error": "Nie udalo sie odczytac metryk z klatek."}), 500

        size_bytes = os.path.getsize(out_path)
        actual_dur = max(n_frames / out_fps, 1e-6)
        actual_kbps = round((size_bytes * 8.0) / actual_dur / 1000.0, 1)

        row = {
            "sekwencja": os.path.splitext(src_name)[0],
            "tryb": tryb,
            "kodek": kodek,
            "bitrate_zadany": kbps,
            "bitrate_rzecz": actual_kbps,
            "tempo": f"{latency}/{'CBR' if bmode == 'constant' else 'VBR'}",
            "PSNR": round(sum(psnr_vals) / n_frames, 2),
            "SSIM": round(sum(ssim_vals) / n_frames, 4),
            "czas_kodowania_s": round(encode_sec, 2),
            "rozmiar_MB": round(size_bytes / 1e6, 3),
        }

        payload = {
            "wiersz": row,
            "tryb": tryb,
            "n": n_frames,
            "fo": out_fps,
            "klatki": {"PSNR": psnr_vals, "SSIM": ssim_vals},
        }

        if keep:
            run_cmd(["ffmpeg", "-y", "-threads", "1", "-i", ref_path, "-vframes", "12", "-q:v", "4", os.path.join(tmpdir, "ref_%04d.jpg")])
            if os.path.exists(ref_path):
                os.remove(ref_path)
            run_cmd(["ffmpeg", "-y", "-threads", "1", "-i", out_path, "-vframes", "12", "-q:v", "4", os.path.join(tmpdir, "enc_%04d.jpg")])
            if os.path.exists(out_path):
                os.remove(out_path)
            ref_urls, w, h = jpg_list_to_data_urls(os.path.join(tmpdir, "ref_*.jpg"))
            enc_urls, _, _ = jpg_list_to_data_urls(os.path.join(tmpdir, "enc_*.jpg"))
            clean_urls = None
            if tryb == "interlaced" and os.path.exists(clean_path):
                run_cmd(["ffmpeg", "-y", "-threads", "1", "-i", clean_path, "-vframes", "12", "-q:v", "4", os.path.join(tmpdir, "clean_%04d.jpg")])
                os.remove(clean_path)
                clean_urls, _, _ = jpg_list_to_data_urls(os.path.join(tmpdir, "clean_*.jpg"))
            m = min(len(ref_urls), len(enc_urls), n_frames)
            payload["n"] = m
            payload["w"] = w
            payload["h"] = h
            payload["ref_urls"] = ref_urls[:m]
            payload["enc_urls"] = enc_urls[:m]
            payload["clean_urls"] = clean_urls[:m] if clean_urls else None
            payload["klatki"]["PSNR"] = psnr_vals[:m]
            payload["klatki"]["SSIM"] = ssim_vals[:m]

        gc.collect()
        return jsonify(payload)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

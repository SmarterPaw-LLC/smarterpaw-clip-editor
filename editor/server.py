#!/usr/bin/env python3
"""Local UGC clip editor for the Meowi project.
Stdlib only. Serves the editor UI + source clips (with HTTP Range) and renders
the timeline to MP4 via ffmpeg. Bind: http://127.0.0.1:8765/
"""
import os, re, json, subprocess, tempfile, shutil, threading, urllib.parse, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR = os.path.join(PROJ, "editor")
THUMBS = os.path.join(EDITOR, "thumbs")
SRC_ROOT = os.path.join(PROJ, "sources", "youtube")
MUSIC_DIR = os.path.join(PROJ, "sources", "music")
EXPORTS = os.path.join(PROJ, "exports")
EDL_PATH = os.path.join(PROJ, "edl.json")
PROJECTS = os.path.join(PROJ, "projects")

_BIN = r"C:\Users\Jason\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin"
FFMPEG = os.path.join(_BIN, "ffmpeg.exe")
FFPROBE = os.path.join(_BIN, "ffprobe.exe")
if not os.path.exists(FFMPEG):
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    FFPROBE = shutil.which("ffprobe") or "ffprobe"

FONT = "C\\:/Windows/Fonts/arialbd.ttf"
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
       "-r", "30", "-video_track_timescale", "90000", "-an"]
ENDCARD_DUR = 2.6
ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")

_probe_cache = {}
_manifest_lock = threading.Lock()


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe(path):
    if path in _probe_cache:
        return _probe_cache[path]
    r = run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
             "-of", "json", path])
    dur, w, h = 0.0, 0, 0
    try:
        j = json.loads(r.stdout)
        dur = float(j.get("format", {}).get("duration", 0) or 0)
        for s in j.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = int(s.get("width", 0)), int(s.get("height", 0))
                break
    except Exception:
        pass
    _probe_cache[path] = (dur, w, h)
    return _probe_cache[path]


def scan_sources():
    """Return list of clips: {id,label,product,url,dur,w,h}."""
    clips = []
    for root, _dirs, files in os.walk(SRC_ROOT):
        for fn in files:
            if not fn.lower().endswith(".mp4"):
                continue
            full = os.path.join(root, fn)
            m = ID_RE.search(fn)
            cid = m.group(1) if m else os.path.splitext(fn)[0]
            product = os.path.basename(root)
            dur, w, h = probe(full)
            rel = os.path.relpath(full, PROJ).replace("\\", "/")
            # build a readable label from the title portion before [id]
            label = fn
            if m:
                label = fn[:m.start()].strip().strip("｜|").strip()
            label = re.sub(r"\s+", " ", label)[:60] or fn
            clips.append({"id": cid, "label": label, "product": product,
                          "url": "/" + urllib.parse.quote(rel), "file": full,
                          "dur": round(dur, 2), "w": w, "h": h})
    clips.sort(key=lambda c: (c["product"], c["label"]))
    return clips


def id_to_file():
    return {c["id"]: c["file"] for c in scan_sources()}


def ensure_thumbs(clips):
    os.makedirs(THUMBS, exist_ok=True)
    for c in clips:
        tp = os.path.join(THUMBS, c["id"] + ".jpg")
        if not os.path.exists(tp):
            ss = min(1.0, max(0.0, c["dur"] / 3))
            run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(ss), "-i", c["file"],
                 "-frames:v", "1", "-vf", "scale=200:-1", "-q:v", "4", tp])


def list_music():
    if not os.path.isdir(MUSIC_DIR):
        return []
    return sorted(f for f in os.listdir(MUSIC_DIR) if f.lower().endswith((".mp3", ".m4a", ".wav")))


def default_edl():
    seg = lambda i, d, c, **k: {"id": i, "in": d[0], "dur": d[1], "cap": c,
                                "zoom": k.get("zoom", 1.0), "anchor": k.get("anchor", "center")}
    return {
        "settings": {
            "canvas": "9x16", "music": "1076_smile.mp3",
            "endcard": ["MEOWIJUANA", "CATNIP JOINTS", "SHOP NOW"],
            "captionSize": 0.058, "captionY": 0.62, "endcardDur": ENDCARD_DUR,
        },
        "segments": [
            seg("8bjuag2ceXA", (1.0, 2.6), "when the joints come out"),
            seg("J22p8Etnkrc", (1.0, 2.0), "your cats new obsession"),
            seg("8bjuag2ceXA", (5.5, 1.6), "they cant get enough"),
            seg("J22p8Etnkrc", (17.5, 1.6), "every single cat"),
            seg("8bjuag2ceXA", (12.5, 1.4), "real catnip they play with"),
            seg("J22p8Etnkrc", (29.5, 2.3), "then total zen"),
            seg("8bjuag2ceXA", (39.0, 3.2), ""),
        ],
    }


def load_edl():
    if os.path.exists(EDL_PATH):
        try:
            with open(EDL_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default_edl()


def save_edl(edl):
    with open(EDL_PATH, "w", encoding="utf-8") as f:
        json.dump(edl, f, indent=2)


def slug(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "project"


def safe_project(fname):
    """Return absolute path inside PROJECTS for a *.json filename, or None."""
    if not fname or not fname.endswith(".json"):
        return None
    base = os.path.basename(fname)
    full = os.path.join(PROJECTS, base)
    if os.path.dirname(os.path.abspath(full)) != os.path.abspath(PROJECTS):
        return None
    return full


def list_projects():
    os.makedirs(PROJECTS, exist_ok=True)
    out = []
    for f in sorted(os.listdir(PROJECTS)):
        if f.endswith(".json"):
            name = os.path.splitext(f)[0]
            try:
                with open(os.path.join(PROJECTS, f), encoding="utf-8") as fh:
                    name = json.load(fh).get("name", name)
            except Exception:
                pass
            out.append({"file": f, "name": name})
    return out


def load_project(fname):
    full = safe_project(fname)
    if not full or not os.path.exists(full):
        return None
    with open(full, encoding="utf-8") as f:
        return json.load(f)


def save_project(name, edl):
    os.makedirs(PROJECTS, exist_ok=True)
    edl = dict(edl)
    edl["name"] = name
    fname = slug(name) + ".json"
    with open(os.path.join(PROJECTS, fname), "w", encoding="utf-8") as f:
        json.dump(edl, f, indent=2)
    return {"file": fname, "name": name}


def seed_projects():
    os.makedirs(PROJECTS, exist_ok=True)
    if any(f.endswith(".json") for f in os.listdir(PROJECTS)):
        return
    base = load_edl()
    base["name"] = "Rollies Hero"
    with open(os.path.join(PROJECTS, "rollies-hero.json"), "w", encoding="utf-8") as f:
        json.dump(base, f, indent=2)


CANVAS = {"9x16": (1080, 1920), "4x5": (1080, 1350), "16x9": (1920, 1080)}


def esc_path(p):
    return p.replace("\\", "/").replace(":", "\\:")


def crop_xy(anchor, W, H):
    if anchor == "bottom":
        return f"(iw-{W})/2", f"(ih-{H})"
    if anchor == "top":
        return f"(iw-{W})/2", "0"
    if anchor == "right":
        return f"(iw-{W})", f"(ih-{H})/2"
    if anchor == "left":
        return "0", f"(ih-{H})/2"
    return f"(iw-{W})/2", f"(ih-{H})/2"


def render(edl):
    s = edl.get("settings", {})
    canvas = s.get("canvas", "9x16")
    W, H = CANVAS.get(canvas, CANVAS["9x16"])
    segs = edl.get("segments", [])
    if not segs:
        return {"ok": False, "log": "No segments."}
    i2f = id_to_file()
    tmp = tempfile.mkdtemp(prefix="meowi_render_")
    try:
        fs = int(W * float(s.get("captionSize", 0.058)))
        capY = int(H * float(s.get("captionY", 0.62)))
        endcard_dur = float(s.get("endcardDur", ENDCARD_DUR))
        listf = os.path.join(tmp, "concat.txt")
        lines = []
        total = 0.0
        for idx, seg in enumerate(segs):
            src = i2f.get(seg["id"])
            if not src:
                return {"ok": False, "log": f"Clip not found for id {seg['id']}"}
            z = max(float(seg.get("zoom", 1.0)), 1.0)
            sw, sh = math.ceil(W * z), math.ceil(H * z)
            cx, cy = crop_xy(seg.get("anchor", "center"), W, H)
            vf = f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={W}:{H}:{cx}:{cy},setsar=1,fps=30,format=yuv420p"
            cap = (seg.get("cap") or "").strip()
            if cap:
                cf = os.path.join(tmp, f"cap_{idx}.txt")
                with open(cf, "w", encoding="utf-8") as f:
                    f.write(cap)
                vf += (f",drawtext=fontfile='{FONT}':textfile='{esc_path(cf)}':fontcolor=white:"
                       f"fontsize={fs}:borderw=7:bordercolor=black@0.95:box=1:boxcolor=black@0.30:"
                       f"boxborderw=18:x=(w-text_w)/2:y={capY}")
            so = os.path.join(tmp, f"seg_{idx:02d}.mp4")
            dur = float(seg["dur"])
            r = run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(seg["in"]), "-i", src,
                     "-t", str(dur), "-vf", vf] + ENC + [so])
            if r.returncode != 0:
                return {"ok": False, "log": f"seg {idx} failed:\n{r.stderr[-1500:]}"}
            lines.append("file '" + so.replace("\\", "/") + "'")
            total += dur
        # end card
        ec = os.path.join(tmp, "seg_99_endcard.mp4")
        el = s.get("endcard", ["MEOWIJUANA", "CATNIP JOINTS", "SHOP NOW"])
        f1, f2, f3 = int(W * 0.085), int(W * 0.048), int(W * 0.052)
        y1 = int(H * 0.40); y2 = y1 + int(f1 * 1.15); y3 = int(H * 0.60)
        ecvf = (
            f"drawtext=fontfile='{FONT}':text='{el[0]}':fontcolor=white:fontsize={f1}:x=(w-text_w)/2:y={y1},"
            f"drawtext=fontfile='{FONT}':text='{el[1]}':fontcolor=0xF5A623:fontsize={f2}:x=(w-text_w)/2:y={y2},"
            f"drawtext=fontfile='{FONT}':text='{el[2]}':fontcolor=black:fontsize={f3}:box=1:boxcolor=0xF5A623:"
            f"boxborderw=26:x=(w-text_w)/2:y={y3},format=yuv420p")
        r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", f"color=c=0x111111:s={W}x{H}:r=30:d={endcard_dur}", "-vf", ecvf] + ENC + [ec])
        if r.returncode != 0:
            return {"ok": False, "log": f"endcard failed:\n{r.stderr[-1500:]}"}
        lines.append("file '" + ec.replace("\\", "/") + "'")
        total += endcard_dur
        with open(listf, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        silent = os.path.join(tmp, "silent.mp4")
        r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listf, "-c", "copy", silent])
        if r.returncode != 0:
            return {"ok": False, "log": f"concat failed:\n{r.stderr[-1500:]}"}
        os.makedirs(EXPORTS, exist_ok=True)
        out = os.path.join(EXPORTS, f"Meowi_rollies_hero_{canvas}.mp4")
        music = os.path.join(MUSIC_DIR, s.get("music", "1076_smile.mp3"))
        fade = round(total - 1.0, 2)
        if os.path.exists(music):
            af = (f"[1:a]atrim=0:{total},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,"
                  f"afade=t=out:st={fade}:d=1,loudnorm=I=-16:TP=-1.5:LRA=11[a]")
            r = run([FFMPEG, "-y", "-loglevel", "error", "-i", silent, "-i", music,
                     "-filter_complex", af, "-map", "0:v", "-map", "[a]",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out])
        else:
            r = run([FFMPEG, "-y", "-loglevel", "error", "-i", silent,
                     "-c:v", "copy", "-movflags", "+faststart", out])
        if r.returncode != 0:
            return {"ok": False, "log": f"mux failed:\n{r.stderr[-1500:]}"}
        rel = "/" + urllib.parse.quote(os.path.relpath(out, PROJ).replace("\\", "/"))
        return {"ok": True, "output": rel, "name": os.path.basename(out),
                "total": round(total, 2), "canvas": canvas}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CTYPE = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
         ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _safe_path(self, urlpath):
        p = urllib.parse.unquote(urlpath.split("?", 1)[0]).lstrip("/")
        full = os.path.normpath(os.path.join(PROJ, p))
        if not full.startswith(PROJ):
            return None
        return full

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_file(os.path.join(EDITOR, "index.html"))
        if path == "/api/manifest":
            with _manifest_lock:
                clips = scan_sources()
                ensure_thumbs(clips)
            for c in clips:
                c["thumb"] = "/editor/thumbs/" + c["id"] + ".jpg"
                c.pop("file", None)
            return self._json({"clips": clips, "music": list_music(),
                               "canvases": list(CANVAS.keys())})
        if path == "/api/edl":
            return self._json(load_edl())
        if path == "/api/projects":
            return self._json({"projects": list_projects()})
        if path == "/api/project":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            fname = (qs.get("file") or [""])[0]
            p = load_project(fname)
            if p is None:
                return self._json({"error": "not found"}, 404)
            return self._json(p)
        full = self._safe_path(path)
        if full and os.path.isfile(full):
            return self._serve_file(full)
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        ln = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(ln) if ln else b"{}"
        try:
            data = json.loads(body or b"{}")
        except Exception:
            return self._json({"ok": False, "log": "bad json"}, 400)
        if path == "/api/edl":
            save_edl(data)
            return self._json({"ok": True})
        if path == "/api/project":
            name = (data.get("name") or "").strip()
            edl = data.get("edl") or {}
            if not name:
                return self._json({"ok": False, "log": "name required"}, 400)
            res = save_project(name, edl)
            return self._json({"ok": True, **res})
        if path == "/api/project/delete":
            full = safe_project(data.get("file", ""))
            if full and os.path.exists(full):
                os.remove(full)
                return self._json({"ok": True})
            return self._json({"ok": False, "log": "not found"}, 404)
        if path == "/api/render":
            save_edl(data)
            try:
                return self._json(render(data))
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
        self.send_error(404)

    def _serve_file(self, full):
        ext = os.path.splitext(full)[1].lower()
        ctype = CTYPE.get(ext, "application/octet-stream")
        try:
            size = os.path.getsize(full)
        except OSError:
            return self.send_error(404)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-", 1)
                start = int(a) if a else 0
                end = int(b) if b else size - 1
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            start = max(0, min(start, end))
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(full, "rb") as f:
            shutil.copyfileobj(f, self.wfile)


def main():
    os.makedirs(EXPORTS, exist_ok=True)
    if not os.path.exists(EDL_PATH):
        save_edl(default_edl())
    seed_projects()
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Meowi editor running at http://127.0.0.1:8765/")
    srv.serve_forever()


if __name__ == "__main__":
    main()

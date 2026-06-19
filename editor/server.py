#!/usr/bin/env python3
"""Local UGC clip editor for the Meowi project.
Stdlib only. Serves the editor UI + source clips (with HTTP Range) and renders
the timeline to MP4 via ffmpeg. Bind: http://127.0.0.1:8765/
"""
import os, re, json, subprocess, tempfile, shutil, threading, urllib.parse, math, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR = os.path.join(PROJ, "editor")
THUMBS = os.path.join(EDITOR, "thumbs")
SRC_ROOT = os.path.join(PROJ, "sources", "youtube")
MUSIC_DIR = os.path.join(PROJ, "sources", "music")
EXPORTS = os.path.join(PROJ, "exports")
EDL_PATH = os.path.join(PROJ, "edl.json")
PROJECTS = os.path.join(PROJ, "projects")
ASSETS = os.path.join(PROJ, "assets")
AI_BUILDS = os.path.join(PROJ, "ai-builds")

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


def vcodec(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path])
    return (r.stdout or "").strip().lower()


def ingest_upload(raw, orig_name, category):
    """Save an uploaded video into sources/youtube/<category>/ as a browser-playable
    .mp4 (remux if already H.264, else re-encode). Returns (rel_path, category)."""
    cat = re.sub(r"[^A-Za-z0-9_-]", "", (category or "uploads").lower()) or "uploads"
    dest_dir = os.path.join(SRC_ROOT, cat)
    os.makedirs(dest_dir, exist_ok=True)
    src_ext = os.path.splitext(orig_name)[1].lower() or ".bin"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(os.path.basename(orig_name))[0]) or "clip"
    # unique destination .mp4
    dest = os.path.join(dest_dir, stem + ".mp4")
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{stem}_{n}.mp4")
        n += 1
    fd, tmpf = tempfile.mkstemp(suffix=src_ext)
    os.close(fd)
    try:
        with open(tmpf, "wb") as f:
            f.write(raw)
        codec = vcodec(tmpf)
        ok = False
        if src_ext == ".mp4" and codec == "h264":
            r = run([FFMPEG, "-y", "-loglevel", "error", "-i", tmpf,
                     "-c", "copy", "-movflags", "+faststart", dest])
            ok = r.returncode == 0 and os.path.exists(dest)
        if not ok:
            r = run([FFMPEG, "-y", "-loglevel", "error", "-i", tmpf,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", dest])
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "ffmpeg failed")[-400:])
        _probe_cache.pop(dest, None)
        cid = os.path.splitext(os.path.basename(dest))[0]
        return os.path.relpath(dest, PROJ).replace("\\", "/"), cat, cid
    finally:
        try:
            os.remove(tmpf)
        except OSError:
            pass


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
            "logo": "logo.png",
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


def create_ai_build(name, clip_ids, canvas, length, brief):
    """Record an 'AI build' handoff: Jason picked clips + wrote a brief in the editor.
    We DON'T copy media — just write a manifest referencing the source clips, and append
    a line to ai-builds/BUILD_LOG.md (the file Claude reads when Jason says 'run here')."""
    by_id = {c["id"]: c for c in scan_sources()}
    chosen = []
    for cid in clip_ids:
        c = by_id.get(cid)
        if not c:
            continue
        chosen.append({"id": c["id"], "label": c["label"], "product": c["product"],
                       "source": os.path.relpath(c["file"], PROJ).replace("\\", "/"),
                       "url": c["url"], "dur": c["dur"], "w": c["w"], "h": c["h"]})
    if not chosen:
        return {"ok": False, "log": "None of the selected clips were found."}
    now = datetime.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    folder_name = f"{slug(name)}-{stamp}"
    folder = os.path.join(AI_BUILDS, folder_name)
    os.makedirs(folder, exist_ok=True)
    manifest = {
        "name": name, "status": "ready", "created": now.isoformat(timespec="seconds"),
        "canvas": canvas, "targetLength": length, "brief": brief, "clips": chosen,
        "_note": ("AI build handoff. status=ready means Jason finished picking clips in the editor. "
                  "Assemble an EDL from these clips per the brief + target length, save it as a project, "
                  "then point edl.json at it for preview/render."),
    }
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    rel_manifest = f"ai-builds/{folder_name}/manifest.json"
    log_path = os.path.join(AI_BUILDS, "BUILD_LOG.md")
    new_log = not os.path.exists(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        if new_log:
            f.write("# AI build queue\n\n"
                    "Each entry is a selection Jason made in the editor (New → ✨ AI build). "
                    "When he says “run here”, read the newest **READY** manifest below and assemble the cut.\n\n")
        f.write(f"- **{name}** — {now.strftime('%Y-%m-%d %H:%M')} · {len(chosen)} clip(s) · "
                f"target {length}s {canvas} · status: **READY**\n"
                f"  - manifest: `{rel_manifest}`\n"
                f"  - brief: {brief or '(none given)'}\n"
                f"  - clips: {', '.join(c['label'] + ' [' + c['id'] + ']' for c in chosen)}\n")
    return {"ok": True, "folder": f"ai-builds/{folder_name}", "manifest": rel_manifest,
            "name": name, "clips": len(chosen)}


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


FONT_FILES = {
    "arial": "C:/Windows/Fonts/arialbd.ttf",
    "cooper": os.path.join(EDITOR, "fonts", "CooperBlack-Std.otf"),
    "meowijuana": os.path.join(EDITOR, "fonts", "meowijuana.ttf"),
}


def _font_path(key):
    p = FONT_FILES.get(key, FONT_FILES["arial"])
    return p if os.path.exists(p) else FONT_FILES["arial"]


def wrap_caption(text, font_path, fontsize, maxw):
    """Word-wrap text to fit maxw px; returns (wrapped_text, n_lines). Balances a 2-line wrap."""
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(font_path, fontsize)
        measure = lambda s: font.getlength(s)
    except Exception:
        measure = lambda s: len(s) * fontsize * 0.55
    words = text.split()
    if not words:
        return text, 1
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if measure(t) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    # balance a 2-line wrap so lines are more even
    if len(lines) == 2 and measure(text) <= maxw * 1.9:
        best, bestdiff = None, 1e9
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if measure(a) <= maxw and measure(b) <= maxw:
                diff = abs(measure(a) - measure(b))
                if diff < bestdiff:
                    bestdiff, best = diff, (a, b)
        if best:
            lines = list(best)
    return "\n".join(lines), len(lines)


def _rgba(c, alpha=255):
    c = (c or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)
    except Exception:
        return (0, 0, 0, alpha)


def normalize_endcard(s):
    """Return a rich end-card dict, migrating the old 3-string array form."""
    ec = s.get("endcard")
    if isinstance(ec, list) or ec is None:
        arr = (ec if isinstance(ec, list) else ["MEOWIJUANA", "CATNIP JOINTS", "SHOP NOW"])
        arr = (list(arr) + ["", "", ""])[:3]
        ec = {
            "enabled": True, "dur": float(s.get("endcardDur", ENDCARD_DUR)),
            "bg": {"mode": "gradient", "color": "#111111", "start": "#f5e020", "end": "#e87820", "angle": 170},
            "logo": {"show": True, "scale": 0.8, "x": 0.5, "y": 0.30},
            "lines": [
                {"text": "", "font": "meowijuana", "color": "#f07830", "size": 0.075, "y": 0.30, "button": False, "bg": "#f07830"},
                {"text": arr[1], "font": "cooper", "color": "#257741", "size": 0.05, "y": 0.56, "button": False, "bg": "#f07830"},
                {"text": arr[2], "font": "cooper", "color": "#ffffff", "size": 0.05, "y": 0.68, "button": True, "bg": "#f07830"},
            ],
        }
    ec.setdefault("enabled", True)
    ec["dur"] = float(ec.get("dur", ENDCARD_DUR))
    bg = ec.setdefault("bg", {})
    bg.setdefault("mode", "gradient"); bg.setdefault("color", "#111111")
    bg.setdefault("start", "#f5e020"); bg.setdefault("end", "#e87820"); bg.setdefault("angle", 170)
    lg = ec.setdefault("logo", {})
    lg.setdefault("show", True); lg.setdefault("scale", 0.8); lg.setdefault("x", 0.5); lg.setdefault("y", 0.30)
    for ln in ec.setdefault("lines", []):
        ln.setdefault("font", "cooper"); ln.setdefault("color", "#ffffff"); ln.setdefault("size", 0.05)
        ln.setdefault("y", 0.6); ln.setdefault("button", False); ln.setdefault("bg", "#f07830")
    return ec


def build_endcard_image(ec, W, H, out_path):
    """Render the end-card graphics to a PNG (RGBA). Transparent bg leaves only logo+text."""
    from PIL import Image, ImageDraw, ImageFont
    bg = ec["bg"]; mode = bg.get("mode", "gradient")
    if mode == "transparent":
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    elif mode == "color":
        img = Image.new("RGBA", (W, H), _rgba(bg.get("color"), 255))
    else:  # vertical gradient: start (top) -> end (bottom)
        top, bot = _rgba(bg.get("start")), _rgba(bg.get("end"))
        col = [tuple(int(top[i] + (bot[i] - top[i]) * (y / (H - 1 or 1))) for i in range(4)) for y in range(H)]
        strip = Image.new("RGBA", (1, H)); strip.putdata(col)
        img = strip.resize((W, H))
    draw = ImageDraw.Draw(img)
    lg = ec.get("logo", {})
    if lg.get("show", True):
        logo_path = os.path.join(PROJ, "logo.png")
        if os.path.exists(logo_path):
            try:
                lo = Image.open(logo_path).convert("RGBA")
                lw = max(1, int(W * float(lg.get("scale", 0.8))))
                lh = max(1, int(lo.height * lw / lo.width))
                lo = lo.resize((lw, lh))
                cx, cy = int(W * float(lg.get("x", 0.5))), int(H * float(lg.get("y", 0.30)))
                img.alpha_composite(lo, (cx - lw // 2, cy - lh // 2))
            except Exception:
                pass
    for ln in ec.get("lines", []):
        txt = (ln.get("text") or "").strip()
        if not txt:
            continue
        size = max(8, int(W * float(ln.get("size", 0.05))))
        try:
            font = ImageFont.truetype(_font_path(ln.get("font", "cooper")), size)
        except Exception:
            font = ImageFont.truetype(FONT_FILES["arial"], size)
        cx, cy = int(W * 0.5), int(H * float(ln.get("y", 0.6)))
        if ln.get("button"):
            bb = draw.textbbox((cx, cy), txt, font=font, anchor="mm")
            padx, pady = int(size * 0.55), int(size * 0.38)
            draw.rounded_rectangle([bb[0] - padx, bb[1] - pady, bb[2] + padx, bb[3] + pady],
                                   radius=int(size * 0.3), fill=_rgba(ln.get("bg"), 255))
        draw.text((cx, cy), txt, font=font, fill=_rgba(ln.get("color"), 255), anchor="mm")
    img.save(out_path)


def list_assets():
    os.makedirs(ASSETS, exist_ok=True)
    out = []
    for f in sorted(os.listdir(ASSETS)):
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            out.append("assets/" + f)
    return out


def _with_shadow(img, o, W):
    """Return img (RGBA) with a drop shadow composited behind it, per o['shadow']."""
    sh = o.get("shadow") or {}
    if not sh.get("on"):
        return img
    from PIL import Image, ImageFilter
    dx = int(W * float(sh.get("dx", 0.004))); dy = int(W * float(sh.get("dy", 0.006)))
    blur = max(0, int(W * float(sh.get("blur", 0.01))))
    op = int(max(0.0, min(1.0, float(sh.get("opacity", 0.5)))) * 255)
    col = _rgba(sh.get("color", "#000000"), 255)
    pad = blur * 3 + max(abs(dx), abs(dy)) + 2
    base = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    alpha = img.split()[3].point(lambda a: int(a * op / 255))
    shadow = Image.new("RGBA", img.size, (col[0], col[1], col[2], 0)); shadow.putalpha(alpha)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0)); layer.paste(shadow, (pad + dx, pad + dy), shadow)
    if blur > 0:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    base = Image.alpha_composite(base, layer)
    base.alpha_composite(img, (pad, pad))
    return base


def _gradient_rgba(w, h, start, end, angle=180.0, gtype="linear", alpha=255):
    """A w×h RGBA gradient matching CSS linear-gradient(<angle>deg)/radial-gradient(circle).
    angle: 0=up, 90=right, 180=down (clockwise). Uses a 256-step LUT for speed."""
    from PIL import Image
    s = _rgba(start, alpha); e = _rgba(end, alpha)
    lut = [tuple(int(s[i] + (e[i] - s[i]) * (k / 255.0)) for i in range(4)) for k in range(256)]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    data = [None] * (w * h)
    if gtype == "radial":
        maxr = (math.hypot(cx, cy) or 1.0)
        for y in range(h):
            row = y * w
            for x in range(w):
                t = math.hypot(x - cx, y - cy) / maxr
                data[row + x] = lut[255 if t >= 1 else int(t * 255)]
    else:
        th = math.radians(angle)
        dx, dy = math.sin(th), -math.cos(th)
        L = (abs(w * math.sin(th)) + abs(h * math.cos(th))) or 1.0
        for y in range(h):
            row = y * w; yc = (y - cy) * dy
            for x in range(w):
                t = ((x - cx) * dx + yc + L / 2.0) / L
                data[row + x] = lut[0 if t <= 0 else 255 if t >= 1 else int(t * 255)]
    img = Image.new("RGBA", (w, h)); img.putdata(data)
    return img


def build_shape_png(o, W, H, out):
    """Rounded rectangle (solid or gradient fill, optional border) -> transparent PNG."""
    from PIL import Image, ImageDraw
    sw = max(1, int(W * float(o.get("w", 0.5))))
    sh = max(1, int(H * float(o.get("h", 0.2))))
    rad = max(0, min(int(W * float(o.get("radius", 0.04))), min(sw, sh) // 2))
    alpha = int(max(0.0, min(1.0, float(o.get("opacity", 0.5)))) * 255)
    bw = max(0, int(W * float(o.get("strokeW", 0))))
    if (o.get("fillType") == "gradient"):
        grad = _gradient_rgba(sw, sh, o.get("gradStart", o.get("fill", "#000000")),
                              o.get("gradEnd", "#ffffff"), float(o.get("gradAngle", 180)),
                              o.get("gradType", "linear"), alpha)
        mask = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, sw - 1, sh - 1], radius=rad, fill=255)
        img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        img.paste(grad, (0, 0), mask)
        if bw > 0:
            ImageDraw.Draw(img).rounded_rectangle([bw // 2, bw // 2, sw - 1 - bw // 2, sh - 1 - bw // 2],
                                                  radius=rad, outline=_rgba(o.get("stroke", "#ffffff"), 255), width=bw)
    else:
        img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        fill = _rgba(o.get("fill", "#000000"), alpha)
        if bw > 0:
            d.rounded_rectangle([bw // 2, bw // 2, sw - 1 - bw // 2, sh - 1 - bw // 2],
                                radius=rad, fill=fill, outline=_rgba(o.get("stroke", "#ffffff"), 255), width=bw)
        else:
            d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=rad, fill=fill)
    _with_shadow(img, o, W).save(out)


def build_sticker_png(o, W, H, out):
    """Bubble-sticker text (ported from the social image tool): bold font, thick white
    outline, gradient fill, drop shadow. Single line -> transparent PNG sized to the text."""
    from PIL import Image, ImageDraw, ImageFont
    text = (o.get("text") or "").strip() or " "
    size = max(10, int(W * float(o.get("size", 0.07))))
    try:
        font = ImageFont.truetype(_font_path(o.get("font", "cooper")), size)
    except Exception:
        font = ImageFont.truetype(FONT_FILES["arial"], size)
    outline = max(4, int(round(size * float(o.get("bleed", 0.22)))))   # white sticker border thickness
    g_from = o.get("gradStart", "#8ab81d"); g_to = o.get("gradEnd", "#3c8e14")
    tmp = Image.new("RGBA", (10, 10)); md = ImageDraw.Draw(tmp)
    bb = md.textbbox((0, 0), text, font=font, stroke_width=outline)
    pad = outline + max(6, int(size * 0.12))
    cw, ch = (bb[2] - bb[0]) + pad * 2, (bb[3] - bb[1]) + pad * 2
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    px, py = pad - bb[0], pad - bb[1]
    d.text((px, py), text, font=font, fill=(255, 255, 255, 255),
           stroke_width=outline, stroke_fill=(255, 255, 255, 255))   # white sticker outline + base
    grad = _gradient_rgba(cw, ch, g_from, g_to, float(o.get("gradAngle", 180)), o.get("gradType", "linear"), 255)
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).text((px, py), text, font=font, fill=255)   # glyph interior only (no stroke)
    img.paste(grad, (0, 0), mask)
    so = dict(o.get("shadow") or {})
    if not so.get("on"):                                  # default sticker pop shadow if none set
        so = {"on": True, "color": "#103300", "blur": 0.008, "dx": 0.003, "dy": 0.006, "opacity": 0.45}
    _with_shadow(img, {"shadow": so}, W).save(out)


def _anim_exprs(o, s, dur, W, tv="t"):
    """Build ffmpeg expressions (time var `tv`) for overlay animations:
    (dx_px, dy_px position offsets, alpha multiplier 0..1, has_opacity). Mirrors animState() in the UI.
    Falls back to legacy fadeIn/fadeOut when no `anims` list is present."""
    e = s + dur
    anims = o.get("anims")
    if anims is None:
        anims = []
        if float(o.get("fadeIn", 0) or 0) > 0: anims.append({"type": "fadeIn", "d": float(o.get("fadeIn"))})
        if float(o.get("fadeOut", 0) or 0) > 0: anims.append({"type": "fadeOut", "d": float(o.get("fadeOut"))})
    lt = "(%s-%g)" % (tv, s)
    dxs, dys, amul, rots = [], [], [], []
    for a in anims:
        ty = a.get("type")
        if ty == "fadeIn":
            d = max(0.01, float(a.get("d", 0.5))); amul.append("min(1,max(0,(%s-%g)/%g))" % (tv, s, d))
        elif ty == "fadeOut":
            d = max(0.01, float(a.get("d", 0.5))); amul.append("min(1,max(0,(%g-%s)/%g))" % (e, tv, d))
        elif ty == "pulse":
            amp = float(a.get("amp", 0.5)); sp = float(a.get("speed", 1.5)); amul.append("(1-%g*(0.5-0.5*cos(2*PI*%g*%s)))" % (amp, sp, lt))
        elif ty == "jitter":
            amp = float(a.get("amp", 0.012)) * W; sp = float(a.get("speed", 11))
            dxs.append("%g*(sin(2*PI*%g*%s)+0.7*sin(2*PI*%g*%s+1.1))/1.7" % (amp, sp, lt, sp * 1.7, lt))
            dys.append("%g*(cos(2*PI*%g*%s)+0.7*sin(2*PI*%g*%s+0.5))/1.7" % (amp, sp * 1.3, lt, sp * 2.1, lt))
        elif ty == "float":
            amp = float(a.get("amp", 0.02)) * W; sp = float(a.get("speed", 0.6)); dys.append("%g*sin(2*PI*%g*%s)" % (amp, sp, lt))
        elif ty == "slideIn":
            d = max(0.01, float(a.get("d", 0.5))); dist = float(a.get("dist", 0.2)) * W; dr = a.get("dir", "left")
            term = "if(lt(%s,%g),pow(1-%s/%g,2)*%g,0)" % (lt, d, lt, d, dist)
            (dxs if dr in ("left", "right") else dys).append(("-" if dr in ("left", "up") else "") + term)
        elif ty == "bounceIn":
            d = max(0.01, float(a.get("d", 0.6))); amp = float(a.get("amp", 0.12)) * W
            dys.append("if(lt(%s,%g),-%g*exp(-3*%s/%g)*cos(2*PI*1.6*%s/%g)*(1-%s/%g),0)" % (lt, d, amp, lt, d, lt, d, lt, d))
        elif ty == "dropIn":
            d = max(0.01, float(a.get("d", 0.6))); dist = float(a.get("dist", 0.5)) * W
            k = "((%s)/%g)" % (lt, d); eb = "(1+2.70158*pow(%s-1,3)+1.70158*pow(%s-1,2))" % (k, k)
            dys.append("if(lt(%s,%g),-%g*(1-%s),0)" % (lt, d, dist, eb))
        elif ty == "popIn":
            d = max(0.01, float(a.get("d", 0.45)))   # scale isn't animatable on an ffmpeg overlay → render as a quick fade-in
            amul.append("min(1,max(0,(%s)/%g))" % (lt, d * 0.5))
        elif ty == "blink":
            sp = float(a.get("speed", 2)); amul.append("gte(sin(2*PI*%g*%s),0)" % (sp, lt))
        elif ty == "wiggle":
            amp = float(a.get("amp", 8)) * math.pi / 180.0; sp = float(a.get("speed", 2)); rots.append("%g*sin(2*PI*%g*%s)" % (amp, sp, lt))
        elif ty == "spin":
            sp = float(a.get("speed", 0.5)); sign = -1 if a.get("dir") == "ccw" else 1; rots.append("%g*2*PI*%g*%s" % (sign, sp, lt))
    dx = "+".join("(%s)" % x for x in dxs) if dxs else "0"
    dy = "+".join("(%s)" % x for x in dys) if dys else "0"
    am = "max(0,min(1,%s))" % ("*".join("(%s)" % x for x in amul)) if amul else "1"
    rot = "+".join("(%s)" % x for x in rots) if rots else None
    return dx, dy, am, bool(amul), rot


def _sparkle_field(a):
    """Deterministic sparkle scatter — must match sparkleField() in the UI."""
    n = max(1, int(a.get("count", 6)))
    out = []
    for i in range(n):
        ang = i * 2.39996
        r = 0.35 + 0.6 * (((i + 1) * 0.618034) % 1.0)
        out.append({"cx": math.cos(ang) * r, "cy": math.sin(ang) * r, "ph": i * 1.7,
                    "sc": 0.7 + 0.6 * (((i + 1) * 0.318) % 1.0)})
    return out


def _emoji_png(emoji, out, px=256):
    """Rasterize an emoji to a transparent PNG via Segoe UI Emoji (color)."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", px)
    img = Image.new("RGBA", (px * 2, px * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        d.text((px, px), emoji, font=font, embedded_color=True, anchor="mm")
    except Exception:
        d.text((px, px), emoji, font=font, anchor="mm")
    bb = img.getbbox()
    if bb:
        img = img.crop(bb)
    img.save(out)


def apply_overlays(silent, overlays, W, H, tmp):
    """Composite free-floating text/image/shape overlays over the full timeline (global time),
    preserving list order as z-order (later = on top)."""
    overlays = [o for o in (overlays or []) if isinstance(o, dict) and o.get("type") in ("text", "image", "shape")]
    if not overlays:
        return silent, None
    overlays = sorted(overlays, key=lambda o: int(o.get("ch", 0) or 0))   # higher channel composited later = on top (stable within a channel)
    inputs = ["-i", silent]
    fc = []
    last = "0:v"
    ii = 1  # next ffmpeg input index for image/shape files
    for k, o in enumerate(overlays):
        t = o.get("type")
        s = float(o.get("start", 0)); e = s + float(o.get("dur", 3))
        ox, oy = float(o.get("x", 0.5)), float(o.get("y", 0.5))
        en = f"enable='between(t,{s},{e})'"
        if t == "text" and o.get("style") != "sticker":
            if not (o.get("text") or "").strip():
                continue
            tf = os.path.join(tmp, f"ov_{k}.txt")
            with open(tf, "w", encoding="utf-8") as fh:
                fh.write(o["text"])
            ff = esc_path(_font_path(o.get("font", "cooper")))
            size = max(8, int(W * float(o.get("size", 0.06))))
            col = (o.get("color", "#ffffff") or "#ffffff").replace("#", "0x")
            adx, ady, aam, _, _ = _anim_exprs(o, s, float(o.get("dur", 3)), W, "t")
            alpha = f":alpha='{aam}'"
            sh = o.get("shadow") or {}
            shopt = ""
            if sh.get("on"):
                sdx = int(W * float(sh.get("dx", 0.004))); sdy = int(W * float(sh.get("dy", 0.006)))
                sop = float(sh.get("opacity", 0.5)); scol = (sh.get("color", "#000000") or "#000000").replace("#", "0x")
                shopt = f":shadowx={sdx}:shadowy={sdy}:shadowcolor={scol}@{sop}"
            common = (f"fontfile='{ff}':textfile='{esc_path(tf)}':fontcolor={col}:fontsize={size}:"
                      f"x='w*{ox}-text_w/2+({adx})':y='h*{oy}-text_h/2+({ady})':{en}{alpha}{shopt}")
            if o.get("button"):
                bgc = (o.get("bg", "#f07830") or "#f07830").replace("#", "0x")
                dt = f"drawtext={common}:box=1:boxcolor={bgc}:boxborderw={int(size*0.4)}"
            else:
                dt = f"drawtext={common}:borderw=6:bordercolor=black@0.6"
            fc.append(f"[{last}]{dt}[v{k}]"); last = f"v{k}"
        else:
            if t == "text":                       # sticker-style text → rendered as a PNG
                if not (o.get("text") or "").strip():
                    continue
                p = os.path.join(tmp, f"sticker_{k}.png")
                build_sticker_png(o, W, H, p)
                scale_w = None
            elif t == "shape":
                p = os.path.join(tmp, f"shape_{k}.png")
                build_shape_png(o, W, H, p)
                scale_w = None
            else:
                p = os.path.join(PROJ, *o["src"].split("/"))
                if not os.path.exists(p):
                    continue
                if (o.get("shadow") or {}).get("on"):
                    from PIL import Image
                    im = Image.open(p).convert("RGBA")
                    tw = max(1, int(W * float(o.get("scale", 0.3)))); th = max(1, int(im.height * tw / im.width))
                    im = _with_shadow(im.resize((tw, th)), o, W)
                    p = os.path.join(tmp, f"img_{k}.png"); im.save(p); scale_w = None
                else:
                    scale_w = max(1, int(W * float(o.get("scale", 0.3))))
            dur_o = float(o.get("dur", 3))
            odx, ody, _, _, orot = _anim_exprs(o, s, dur_o, W, "t")     # position + rotation (overlay time = t)
            _, _, amT, has_op, _ = _anim_exprs(o, s, dur_o, W, "T")     # opacity (geq pixel time = T)
            inputs += ["-loop", "1", "-t", str(e), "-i", p]
            filt = []
            if scale_w:
                filt.append(f"scale={scale_w}:-1")
            filt.append("format=rgba")
            shim = next((a for a in (o.get("anims") or []) if a.get("type") == "shimmer"), None)
            if has_op or shim:                                          # per-frame alpha (opacity) and/or shimmer sheen via geq (T = timeline t)
                if shim:
                    amt = float(shim.get("amount", 0.7)); bwf = float(shim.get("width", 0.22)); spd = float(shim.get("speed", 1.0))
                    ca, sa = math.cos(math.radians(30)), math.sin(math.radians(30))
                    proj = "(X*%g+Y*%g)" % (ca, sa); L = "(W*%g+H*%g)" % (abs(ca), abs(sa)); band = "(%g*%s)" % (bwf, L)
                    sw = "(mod((T-%g)*%g,1)*(%s+2*%s)-%s)" % (s, spd, L, band, band)
                    B = "%g*exp(-pow((%s-%s)/(%s/2+1),2))" % (amt, proj, sw, band)
                    rex, gex, bex = ("min(255,%s(X,Y)+(%s)*(255-%s(X,Y)))" % (c, B, c) for c in ("r", "g", "b"))
                else:
                    rex, gex, bex = "r(X,Y)", "g(X,Y)", "b(X,Y)"
                aex = "alpha(X,Y)*(%s)" % amT if has_op else "alpha(X,Y)"
                filt.append(f"geq=r='{rex}':g='{gex}':b='{bex}':a='{aex}'")
            pop = next((a for a in (o.get("anims") or []) if a.get("type") == "popIn"), None)
            if pop:                                                      # real scale-pop via time-varying scale (eval=frame, t = global)
                d = max(0.01, float(pop.get("d", 0.45)))
                kk = "((t-%g)/%g)" % (s, d); eb = "(1+2.70158*pow(%s-1,3)+1.70158*pow(%s-1,2))" % (kk, kk)
                pops = "if(between(t,%g,%g),max(0.05,%s),1)" % (s, s + d, eb)
                filt.append(f"scale=w='iw*({pops})':h='ih*({pops})':eval=frame")
            srot = float(o.get("rot", 0) or 0)                          # static rotation (degrees) + any anim rotation
            rot_terms = ([orot] if orot else []) + ([f"{math.radians(srot):.6f}"] if abs(srot) > 1e-6 else [])
            if rot_terms:
                rexpr = "+".join(f"({r})" for r in rot_terms)
                # transparent margin first so the rotate doesn't smear edge pixels of overlays
                # whose content touches the PNG boundary (caused ghost streaks on wobble/spin)
                filt.append("pad=ceil(iw*1.08):ceil(ih*1.08):(ow-iw)/2:(oh-ih)/2:color=black@0")
                filt.append(f"rotate='{rexpr}':c=none:ow='hypot(iw,ih)':oh='hypot(iw,ih)'")
            fc.append(f"[{ii}:v]" + ",".join(filt) + f"[oi{k}]")
            fc.append(f"[{last}][oi{k}]overlay=x='W*{ox}-w/2+({odx})':y='H*{oy}-h/2+({ody})':{en}[v{k}]")
            last = f"v{k}"; ii += 1
        # sparkle: composite twinkling copies of an emoji or image around the overlay center
        spk = next((a for a in (o.get("anims") or []) if a.get("type") == "sparkle" and ((a.get("emoji") or "").strip() or a.get("src"))), None)
        if spk:
            if (spk.get("emoji") or "").strip():
                psp = os.path.join(tmp, f"emoji_{k}.png")
                try:
                    _emoji_png(spk["emoji"], psp)
                except Exception:
                    psp = None
            else:
                psp = os.path.join(PROJ, *spk["src"].split("/"))
                if not os.path.exists(psp):
                    psp = None
            if psp:
                spread = float(spk.get("spread", 0.12)) * W; ssize = float(spk.get("size", 0.06)) * W; sspeed = float(spk.get("speed", 1.2))
                for i, f in enumerate(_sparkle_field(spk)):
                    sw = max(2, int(ssize * f["sc"])); offx = f["cx"] * spread; offy = f["cy"] * spread
                    twk = "pow(max(0,sin(2*PI*%g*(T-%g)+%g)),3)" % (sspeed, s, f["ph"])
                    inputs += ["-loop", "1", "-t", str(e), "-i", psp]
                    fc.append(f"[{ii}:v]scale={sw}:-1,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({twk})'[sp{k}_{i}]")
                    fc.append(f"[{last}][sp{k}_{i}]overlay=x='W*{ox}-w/2+({offx})':y='H*{oy}-h/2+({offy})':{en}[vs{k}_{i}]")
                    last = f"vs{k}_{i}"; ii += 1
    if not fc:
        return silent, None
    out = os.path.join(tmp, "overlaid.mp4")
    cmd = ([FFMPEG, "-y", "-loglevel", "error"] + inputs + ["-filter_complex", ";".join(fc), "-map", f"[{last}]"]
           + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
              "-r", "30", "-video_track_timescale", "90000", "-an", out])
    r = run(cmd)
    if r.returncode != 0:
        return None, r.stderr[-1500:]
    return out, None


def flatten_segments(edl):
    """Collapse multi-channel clips into one sequential list (higher channel covers lower).
    channel 0 = base story track (sequential, with lead `gap`); channels >=1 are positioned by
    absolute `t0`. Returns (flat, vid_total). Each flat entry is either a black filler
    {"black": True, "dur": d} or a clip sub-segment
    {id,in,dur,speed,cap,zoom,anchor[,panX,panY,fadeIn,fadeOut]} where `dur` is SOURCE seconds."""
    def _spd(s):
        return max(0.1, min(10.0, float(s.get("speed", 1) or 1)))
    def _tl(s):   # timeline footprint = source dur / speed (matches the preview's segLen)
        return max(0.05, float(s.get("dur", 1) or 1) / _spd(s))
    segs = edl.get("segments", [])
    base, acc = [], 0.0
    for i, s in enumerate(segs):
        if int(s.get("ch", 0) or 0) != 0:
            continue
        g = max(0.0, float(s.get("gap", 0) or 0))
        d = _tl(s)
        base.append({"i": i, "s": s, "ch": 0, "start": acc + g, "end": acc + g + d})
        acc += g + d
    clips = list(base)
    for i, s in enumerate(segs):
        ch = int(s.get("ch", 0) or 0)
        if ch == 0:
            continue
        t0 = max(0.0, float(s.get("t0", 0) or 0))
        d = _tl(s)
        clips.append({"i": i, "s": s, "ch": ch, "start": t0, "end": t0 + d})
    vid_total = acc
    for c in clips:
        vid_total = max(vid_total, c["end"])
    bset = {0.0, round(vid_total, 4)}
    for c in clips:
        bset.add(round(c["start"], 4))
        bset.add(round(c["end"], 4))
    bs = sorted(x for x in bset if 0.0 <= x <= vid_total + 1e-6)
    flat = []
    for a, b in zip(bs, bs[1:]):
        if b - a < 0.02:
            continue
        m = (a + b) / 2.0
        top = None
        for c in clips:
            if c["start"] <= m < c["end"]:
                if top is None or c["ch"] > top["ch"] or (c["ch"] == top["ch"] and c["i"] > top["i"]):
                    top = c
        if top is None:
            flat.append({"black": True, "dur": round(b - a, 4)})
            continue
        s, off = top["s"], a - top["start"]
        sp = _spd(s)
        # `off` and (b-a) are TIMELINE seconds; convert to SOURCE seconds for the encoder
        # (which treats seg["dur"] as source seconds and re-stretches via setpts at `speed`).
        seg = {"id": s.get("id"), "in": round(float(s.get("in", 0) or 0) + off * sp, 4),
               "dur": round((b - a) * sp, 4), "speed": sp, "cap": s.get("cap", ""),
               "zoom": s.get("zoom", 1.0), "anchor": s.get("anchor", "center")}
        if s.get("panX") is not None:
            seg["panX"] = s.get("panX")
        if s.get("panY") is not None:
            seg["panY"] = s.get("panY")
        fi = float(s.get("fadeIn", 0) or 0)
        fo = float(s.get("fadeOut", 0) or 0)
        if fi > 0 and abs(a - top["start"]) < 0.02:
            seg["fadeIn"] = fi
        if fo > 0 and abs(b - top["end"]) < 0.02:
            seg["fadeOut"] = fo
        flat.append(seg)
    return flat, vid_total


def render(edl, out_dir=None, out_name=None):
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
        EC = normalize_endcard(s)
        ec_on = bool(EC.get("enabled", True))
        ec_dur = float(EC.get("dur", ENDCARD_DUR))
        ec_transparent = ec_on and EC["bg"].get("mode") == "transparent"
        ec_png = os.path.join(tmp, "endcard_gfx.png")
        if ec_on:
            try:
                build_endcard_image(EC, W, H, ec_png)
            except Exception as e:
                return {"ok": False, "log": f"end card image failed: {e!r}"}
        listf = os.path.join(tmp, "concat.txt")
        lines = []
        total = 0.0
        flat, _vt = flatten_segments(edl)   # multi-channel → sequential (top channel covers lower)
        if not flat:
            return {"ok": False, "log": "Nothing to render."}
        last_real = max((k for k, e in enumerate(flat) if not e.get("black")), default=-1)
        for idx, seg in enumerate(flat):
            if seg.get("black"):            # uncovered span (gap, or black under upper clips) → black filler
                d = max(0.05, float(seg.get("dur", 0.1)))
                gp = os.path.join(tmp, f"blk_{idx:02d}.mp4")
                r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                         "-i", f"color=c=black:s={W}x{H}:r=30:d={d}", "-vf", "format=yuv420p"] + ENC + [gp])
                if r.returncode != 0:
                    return {"ok": False, "log": f"black {idx} failed:\n{r.stderr[-1500:]}"}
                lines.append("file '" + gp.replace("\\", "/") + "'")
                total += d
                continue
            src = i2f.get(seg["id"])
            if not src:
                return {"ok": False, "log": f"Clip not found for id {seg['id']}"}
            z = max(float(seg.get("zoom", 1.0)), 1.0)
            sw, sh = math.ceil(W * z), math.ceil(H * z)
            px, py = seg.get("panX"), seg.get("panY")
            if px is not None or py is not None:
                px = min(1.0, max(0.0, float(px) if px is not None else 0.5))
                py = min(1.0, max(0.0, float(py) if py is not None else 0.5))
                cx, cy = f"(iw-{W})*{px:.5f}", f"(ih-{H})*{py:.5f}"
            else:
                cx, cy = crop_xy(seg.get("anchor", "center"), W, H)
            base = f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={W}:{H}:{cx}:{cy},setsar=1,fps=30,format=yuv420p"
            dur = float(seg["dur"])                     # dur = SOURCE seconds consumed
            spd = min(10.0, max(0.1, float(seg.get("speed", 1) or 1)))
            outlen = dur / spd                          # timeline (output) seconds
            if abs(spd - 1.0) > 1e-3:
                base = f"setpts={1.0/spd:.6f}*PTS," + base   # slow (<1) / speed up (>1)
            sfi = float(seg.get("fadeIn", 0) or 0); sfo = float(seg.get("fadeOut", 0) or 0)
            if sfi > 0:
                base += f",fade=t=in:st=0:d={sfi}"
            if sfo > 0:
                base += f",fade=t=out:st={max(0,outlen-sfo)}:d={sfo}"
            is_last = (idx == last_real)
            cap = (seg.get("cap") or "").strip()
            cap_dt = ""
            if cap:
                wrapped, nlines = wrap_caption(cap, FONT_FILES["arial"], fs, int(W * 0.86))
                cf = os.path.join(tmp, f"cap_{idx}.txt")
                with open(cf, "w", encoding="utf-8") as f:
                    f.write(wrapped)
                cap_y = max(0, capY - int((nlines - 1) * fs * 0.62))
                cap_dt = (f"drawtext=fontfile='{FONT}':textfile='{esc_path(cf)}':fontcolor=white:"
                          f"fontsize={fs}:text_align=C:line_spacing=6:borderw=7:bordercolor=black@0.95:"
                          f"box=1:boxcolor=black@0.30:boxborderw=18:x=(w-text_w)/2:y={cap_y}")
            so = os.path.join(tmp, f"seg_{idx:02d}.mp4")
            if is_last and ec_transparent:
                # extend the last clip by the end-card duration and fade the CTA overlay in over it.
                # The clip portion lasts `outlen` timeline seconds (after any speed change), then holds.
                ext = outlen + ec_dur
                src_read = dur + ec_dur * spd          # source seconds covering the held tail
                vbase = base + (("," + cap_dt + f":enable='lt(t,{outlen})'") if cap_dt else "")
                fc = (f"[0:v]{vbase}[v];"
                      f"[1:v]format=rgba,fade=t=in:st={outlen}:d=0.4:alpha=1[g];"
                      f"[v][g]overlay=0:0:enable='gte(t,{outlen})'[out]")
                r = run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(seg["in"]), "-t", str(src_read), "-i", src,
                         "-loop", "1", "-i", ec_png,
                         "-filter_complex", fc, "-map", "[out]", "-t", str(ext)] + ENC + [so])
                if r.returncode != 0:
                    return {"ok": False, "log": f"seg {idx} (transparent end card) failed:\n{r.stderr[-1500:]}"}
                total += ext
            else:
                vf = base + (("," + cap_dt) if cap_dt else "")
                r = run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(seg["in"]), "-t", str(dur), "-i", src,
                         "-vf", vf, "-t", str(outlen)] + ENC + [so])
                if r.returncode != 0:
                    return {"ok": False, "log": f"seg {idx} failed:\n{r.stderr[-1500:]}"}
                total += outlen
            lines.append("file '" + so.replace("\\", "/") + "'")
        # end card: solid/gradient gets its own clip; transparent is already baked into the last segment
        if ec_on and not ec_transparent:
            ec_clip = os.path.join(tmp, "seg_99_endcard.mp4")
            r = run([FFMPEG, "-y", "-loglevel", "error", "-loop", "1", "-t", str(ec_dur),
                     "-i", ec_png, "-vf", "format=yuv420p"] + ENC + [ec_clip])
            if r.returncode != 0:
                return {"ok": False, "log": f"endcard failed:\n{r.stderr[-1500:]}"}
            lines.append("file '" + ec_clip.replace("\\", "/") + "'")
            total += ec_dur
        with open(listf, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        silent = os.path.join(tmp, "silent.mp4")
        r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listf, "-c", "copy", silent])
        if r.returncode != 0:
            return {"ok": False, "log": f"concat failed:\n{r.stderr[-1500:]}"}
        # free-floating overlays (text/images) over the whole timeline
        vid_src, ov_err = apply_overlays(silent, edl.get("overlays", []), W, H, tmp)
        if vid_src is None:
            return {"ok": False, "log": f"overlays failed:\n{ov_err}"}
        base_name = (out_name or f"Meowi_rollies_hero_{canvas}").strip()
        if not base_name.lower().endswith(".mp4"):
            base_name += ".mp4"
        target_dir = out_dir.strip() if out_dir else EXPORTS
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as ex:
            return {"ok": False, "log": f"Can't create folder:\n{target_dir}\n{ex}"}
        out = os.path.join(target_dir, base_name)
        music = os.path.join(MUSIC_DIR, s.get("music", "1076_smile.mp3"))
        mvol = float(s.get("musicVol", 0.5) if s.get("musicVol") is not None else 0.5)
        fade = round(total - 1.0, 2)
        if os.path.exists(music) and mvol > 0.001:
            af = (f"[1:a]atrim=0:{total},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,"
                  f"afade=t=out:st={fade}:d=1,loudnorm=I=-16:TP=-1.5:LRA=11,volume={mvol:.3f}[a]")
            r = run([FFMPEG, "-y", "-loglevel", "error", "-i", vid_src, "-i", music,
                     "-filter_complex", af, "-map", "0:v", "-map", "[a]",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out])
        else:
            r = run([FFMPEG, "-y", "-loglevel", "error", "-i", vid_src,
                     "-c:v", "copy", "-movflags", "+faststart", out])
        if r.returncode != 0:
            tail = r.stderr[-1500:]
            hint = ""
            if "Permission denied" in tail or "being used" in tail or "Invalid argument" in tail:
                hint = "\n(The file may be open in a video player — close it, or use a new filename.)"
            return {"ok": False, "log": f"Couldn't write the file:\n{tail}{hint}"}
        url = None
        try:
            rel = os.path.relpath(out, PROJ)
            if not rel.startswith(".."):
                url = "/" + urllib.parse.quote(rel.replace("\\", "/"))
        except Exception:
            url = None
        return {"ok": True, "output": url, "path": os.path.abspath(out), "name": os.path.basename(out),
                "total": round(total, 2), "canvas": canvas}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CTYPE = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
         ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".json": "application/json",
         ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2"}


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
        if path == "/api/srcsig":
            # cheap fingerprint of the source library so the UI can auto-refresh when clips change
            n = 0; mx = 0.0
            for root, _d, files in os.walk(SRC_ROOT):
                for fn in files:
                    if fn.lower().endswith(".mp4"):
                        n += 1
                        try: mx = max(mx, os.path.getmtime(os.path.join(root, fn)))
                        except OSError: pass
            return self._json({"n": n, "m": round(mx, 2)})
        if path == "/api/manifest":
            with _manifest_lock:
                clips = scan_sources()
                ensure_thumbs(clips)
            for c in clips:
                c["thumb"] = "/editor/thumbs/" + c["id"] + ".jpg"
                c.pop("file", None)
            logo = None
            for cand in ("logo.png", "logo.jpg", "logo.jpeg", "logo.webp"):
                if os.path.exists(os.path.join(PROJ, cand)):
                    logo = "/" + cand
                    break
            return self._json({"clips": clips, "music": list_music(),
                               "canvases": list(CANVAS.keys()), "logo": logo, "assets": list_assets(),
                               "exportsDir": EXPORTS})
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
        if path == "/api/clip/recat":
            cid = (data.get("id") or "").strip()
            cat = re.sub(r"[^A-Za-z0-9_-]", "", (data.get("category") or "").lower())
            if not cid or not cat:
                return self._json({"ok": False, "log": "id and category required"}, 400)
            src = id_to_file().get(cid)
            if not src or not os.path.exists(src):
                return self._json({"ok": False, "log": "clip not found"}, 404)
            dest_dir = os.path.join(SRC_ROOT, cat)
            if os.path.abspath(os.path.dirname(src)) == os.path.abspath(dest_dir):
                return self._json({"ok": True, "category": cat})   # already there
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            if os.path.exists(dest):
                return self._json({"ok": False, "log": f"a clip with that name already exists in '{cat}'"}, 409)
            try:
                shutil.move(src, dest)
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
            _probe_cache.pop(src, None)
            return self._json({"ok": True, "category": cat})
        if path == "/api/clip/delete":
            cid = (data.get("id") or "").strip()
            src = id_to_file().get(cid)
            if not src or not os.path.exists(src):
                return self._json({"ok": False, "log": "clip not found"}, 404)
            try:
                os.remove(src)
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
            th = os.path.join(THUMBS, cid + ".jpg")
            if os.path.exists(th):
                try: os.remove(th)
                except OSError: pass
            _probe_cache.pop(src, None)
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
            out_dir = data.pop("_outDir", None) or None
            out_name = data.pop("_outName", None) or None
            save_edl(data)
            try:
                return self._json(render(data, out_dir, out_name))
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
        if path == "/api/ai-build":
            clip_ids = data.get("clips") or []
            if not clip_ids:
                return self._json({"ok": False, "log": "Select at least one clip."}, 400)
            name = (data.get("name") or "").strip() or "AI build"
            canvas = data.get("canvas") or "9x16"
            try:
                length = float(data.get("length") or 15)
            except Exception:
                length = 15.0
            brief = (data.get("brief") or "").strip()
            try:
                return self._json(create_ai_build(name, clip_ids, canvas, length, brief))
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
        if path == "/api/upload":
            import base64
            name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(data.get("name", "upload"))) or "upload.png"
            b64 = data.get("data", "")
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return self._json({"ok": False, "log": "bad image data"}, 400)
            os.makedirs(ASSETS, exist_ok=True)
            with open(os.path.join(ASSETS, name), "wb") as f:
                f.write(raw)
            return self._json({"ok": True, "src": "assets/" + name})
        if path == "/api/upload-clip":
            import base64
            b64 = data.get("data", "")
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return self._json({"ok": False, "log": "bad clip data"}, 400)
            if not raw:
                return self._json({"ok": False, "log": "empty file"}, 400)
            try:
                rel, cat, cid = ingest_upload(raw, data.get("name", "clip.mp4"), data.get("category", "uploads"))
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
            return self._json({"ok": True, "category": cat, "file": rel, "id": cid})
        self.send_error(404)

    def _serve_file(self, full):
        ext = os.path.splitext(full)[1].lower()
        ctype = CTYPE.get(ext, "application/octet-stream")
        nocache = ext in (".html", ".js", ".json", ".css")
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
        if nocache:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
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

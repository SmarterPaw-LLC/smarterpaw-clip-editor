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


def build_shape_png(o, W, H, out):
    """Rounded rectangle (fill+opacity, optional border) -> transparent PNG."""
    from PIL import Image, ImageDraw
    sw = max(1, int(W * float(o.get("w", 0.5))))
    sh = max(1, int(H * float(o.get("h", 0.2))))
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = max(0, min(int(W * float(o.get("radius", 0.04))), min(sw, sh) // 2))
    alpha = int(max(0.0, min(1.0, float(o.get("opacity", 0.5)))) * 255)
    fill = _rgba(o.get("fill", "#000000"), alpha)
    bw = max(0, int(W * float(o.get("strokeW", 0))))
    if bw > 0:
        d.rounded_rectangle([bw // 2, bw // 2, sw - 1 - bw // 2, sh - 1 - bw // 2],
                            radius=rad, fill=fill, outline=_rgba(o.get("stroke", "#ffffff"), 255), width=bw)
    else:
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=rad, fill=fill)
    _with_shadow(img, o, W).save(out)


def apply_overlays(silent, overlays, W, H, tmp):
    """Composite free-floating text/image/shape overlays over the full timeline (global time),
    preserving list order as z-order (later = on top)."""
    overlays = [o for o in (overlays or []) if isinstance(o, dict) and o.get("type") in ("text", "image", "shape")]
    if not overlays:
        return silent, None
    inputs = ["-i", silent]
    fc = []
    last = "0:v"
    ii = 1  # next ffmpeg input index for image/shape files
    for k, o in enumerate(overlays):
        t = o.get("type")
        s = float(o.get("start", 0)); e = s + float(o.get("dur", 3))
        ox, oy = float(o.get("x", 0.5)), float(o.get("y", 0.5))
        en = f"enable='between(t,{s},{e})'"
        if t == "text":
            if not (o.get("text") or "").strip():
                continue
            tf = os.path.join(tmp, f"ov_{k}.txt")
            with open(tf, "w", encoding="utf-8") as fh:
                fh.write(o["text"])
            ff = esc_path(_font_path(o.get("font", "cooper")))
            size = max(8, int(W * float(o.get("size", 0.06))))
            col = (o.get("color", "#ffffff") or "#ffffff").replace("#", "0x")
            fi = float(o.get("fadeIn", 0) or 0); fo = float(o.get("fadeOut", 0) or 0)
            alpha = ""
            if fi > 0 or fo > 0:
                ai = f"min(1,(t-{s})/{fi})" if fi > 0 else "1"
                ao = f"min(1,({e}-t)/{fo})" if fo > 0 else "1"
                alpha = f":alpha='max(0,min({ai},{ao}))'"
            sh = o.get("shadow") or {}
            shopt = ""
            if sh.get("on"):
                sdx = int(W * float(sh.get("dx", 0.004))); sdy = int(W * float(sh.get("dy", 0.006)))
                sop = float(sh.get("opacity", 0.5)); scol = (sh.get("color", "#000000") or "#000000").replace("#", "0x")
                shopt = f":shadowx={sdx}:shadowy={sdy}:shadowcolor={scol}@{sop}"
            common = (f"fontfile='{ff}':textfile='{esc_path(tf)}':fontcolor={col}:fontsize={size}:"
                      f"x=w*{ox}-text_w/2:y=h*{oy}-text_h/2:{en}{alpha}{shopt}")
            if o.get("button"):
                bgc = (o.get("bg", "#f07830") or "#f07830").replace("#", "0x")
                dt = f"drawtext={common}:box=1:boxcolor={bgc}:boxborderw={int(size*0.4)}"
            else:
                dt = f"drawtext={common}:borderw=6:bordercolor=black@0.6"
            fc.append(f"[{last}]{dt}[v{k}]"); last = f"v{k}"
        else:
            if t == "shape":
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
            fi = float(o.get("fadeIn", 0) or 0); fo = float(o.get("fadeOut", 0) or 0)
            inputs += ["-loop", "1", "-t", str(e), "-i", p]
            filt = []
            if scale_w:
                filt.append(f"scale={scale_w}:-1")
            filt.append("format=rgba")
            if fi > 0:
                filt.append(f"fade=t=in:st={s}:d={fi}:alpha=1")
            if fo > 0:
                filt.append(f"fade=t=out:st={max(0,e-fo)}:d={fo}:alpha=1")
            fc.append(f"[{ii}:v]" + ",".join(filt) + f"[oi{k}]")
            fc.append(f"[{last}][oi{k}]overlay=x=W*{ox}-w/2:y=H*{oy}-h/2:{en}[v{k}]")
            last = f"v{k}"; ii += 1
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
        for idx, seg in enumerate(segs):
            gap = max(0.0, float(seg.get("gap", 0) or 0))   # lead black gap (free-mode timeline gaps)
            if gap > 0.01:
                gp = os.path.join(tmp, f"gap_{idx:02d}.mp4")
                r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                         "-i", f"color=c=black:s={W}x{H}:r=30:d={gap}", "-vf", "format=yuv420p"] + ENC + [gp])
                if r.returncode != 0:
                    return {"ok": False, "log": f"gap {idx} failed:\n{r.stderr[-1500:]}"}
                lines.append("file '" + gp.replace("\\", "/") + "'")
                total += gap
            src = i2f.get(seg["id"])
            if not src:
                return {"ok": False, "log": f"Clip not found for id {seg['id']}"}
            z = max(float(seg.get("zoom", 1.0)), 1.0)
            sw, sh = math.ceil(W * z), math.ceil(H * z)
            cx, cy = crop_xy(seg.get("anchor", "center"), W, H)
            base = f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={W}:{H}:{cx}:{cy},setsar=1,fps=30,format=yuv420p"
            dur = float(seg["dur"])
            sfi = float(seg.get("fadeIn", 0) or 0); sfo = float(seg.get("fadeOut", 0) or 0)
            if sfi > 0:
                base += f",fade=t=in:st=0:d={sfi}"
            if sfo > 0:
                base += f",fade=t=out:st={max(0,dur-sfo)}:d={sfo}"
            is_last = (idx == len(segs) - 1)
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
                # extend the last clip by the end-card duration and fade the CTA overlay in over it
                ext = dur + ec_dur
                vbase = base + (("," + cap_dt + f":enable='lt(t,{dur})'") if cap_dt else "")
                fc = (f"[0:v]{vbase}[v];"
                      f"[1:v]format=rgba,fade=t=in:st={dur}:d=0.4:alpha=1[g];"
                      f"[v][g]overlay=0:0:enable='gte(t,{dur})'[out]")
                r = run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(seg["in"]), "-i", src,
                         "-t", str(ext), "-loop", "1", "-i", ec_png,
                         "-filter_complex", fc, "-map", "[out]"] + ENC + [so])
                if r.returncode != 0:
                    return {"ok": False, "log": f"seg {idx} (transparent end card) failed:\n{r.stderr[-1500:]}"}
                total += ext
            else:
                vf = base + (("," + cap_dt) if cap_dt else "")
                r = run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(seg["in"]), "-i", src,
                         "-t", str(dur), "-vf", vf] + ENC + [so])
                if r.returncode != 0:
                    return {"ok": False, "log": f"seg {idx} failed:\n{r.stderr[-1500:]}"}
                total += dur
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
        fade = round(total - 1.0, 2)
        if os.path.exists(music):
            af = (f"[1:a]atrim=0:{total},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,"
                  f"afade=t=out:st={fade}:d=1,loudnorm=I=-16:TP=-1.5:LRA=11[a]")
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

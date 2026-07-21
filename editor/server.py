#!/usr/bin/env python3
"""Local UGC clip editor for the Meowi project.
Stdlib only. Serves the editor UI + source clips (with HTTP Range) and renders
the timeline to MP4 via ffmpeg. Bind: http://127.0.0.1:8765/
"""
import os, re, json, subprocess, tempfile, shutil, threading, urllib.parse, urllib.request, math, datetime, time, zipfile, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Where the in-app updater checks for new versions (the GitHub Pages site).
UPDATE_URL = "https://smarterpaw-llc.github.io/smarterpaw-clip-editor"
RESTART_EXIT_CODE = 42   # launcher loop re-runs server.py when it exits with this code

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR = os.path.join(PROJ, "editor")
# Frozen at process start: the mtime of server.py as this process saw it. UI compares this
# to the current on-disk mtime and warns when they differ (== code was edited after boot,
# so the running process is running STALE code and needs a restart).
try:
    _SERVER_PY_BOOT_MTIME = int(os.path.getmtime(os.path.join(EDITOR, "server.py")))
except Exception:
    _SERVER_PY_BOOT_MTIME = 0
THUMBS = os.path.join(EDITOR, "thumbs")
SRC_ROOT = os.path.join(PROJ, "sources", "clips")   # the clip repository (was 'sources/youtube'; renamed v1.62.5 for clarity)
MUSIC_DIR = os.path.join(PROJ, "sources", "music")
SFX_DIR   = os.path.join(PROJ, "sources", "sfx")
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
       "-r", "60", "-video_track_timescale", "90000", "-an"]
ENDCARD_DUR = 2.6
ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")

_probe_cache = {}                                  # in-memory: {path: (dur,w,h)}
_probe_disk = {}                                   # disk-mirrored: {path: {mtime,size,dur,w,h}}
_probe_dirty = False
_probe_cache_lock = threading.Lock()
PROBE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_cache.json")
def _probe_cache_load():
    try:
        if os.path.exists(PROBE_CACHE_FILE):
            with open(PROBE_CACHE_FILE, encoding="utf-8") as f:
                d = json.load(f) or {}
            if isinstance(d, dict):
                _probe_disk.update(d)
    except Exception:
        pass
def _probe_cache_save():
    global _probe_dirty
    if not _probe_dirty: return
    try:
        tmp = PROBE_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_probe_disk, f)
        os.replace(tmp, PROBE_CACHE_FILE)
        _probe_dirty = False
    except Exception:
        pass
_probe_cache_load()

# --- Clip tags (free-form, comma-separated, persisted in editor/clip_tags.json) ---
CLIP_TAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clip_tags.json")
_clip_tags_cache = None
def load_clip_tags():
    global _clip_tags_cache
    if _clip_tags_cache is not None: return _clip_tags_cache
    try:
        if os.path.exists(CLIP_TAGS_FILE):
            d = json.load(open(CLIP_TAGS_FILE, encoding="utf-8")) or {}
            if isinstance(d, dict):
                _clip_tags_cache = {k: [str(t) for t in (v or []) if t] for k, v in d.items()}
                return _clip_tags_cache
    except Exception: pass
    _clip_tags_cache = {}
    return _clip_tags_cache
def save_clip_tags(d):
    global _clip_tags_cache
    _clip_tags_cache = {k: sorted(set(str(t).strip() for t in (v or []) if str(t).strip())) for k, v in (d or {}).items()}
    _clip_tags_cache = {k: v for k, v in _clip_tags_cache.items() if v}   # drop empty
    tmp = CLIP_TAGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_clip_tags_cache, f, indent=2, sort_keys=True)
    os.replace(tmp, CLIP_TAGS_FILE)

_manifest_lock = threading.Lock()
_render_jobs = {}            # job_id -> {stage, pct, done, result}
_render_lock = threading.Lock()
_render_seq = [0]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def probe(path):
    global _probe_dirty
    if path in _probe_cache:
        return _probe_cache[path]
    # Disk cache hit if (mtime,size) match — survives server restarts. Cold startup of a project
    # with many clips used to be one ffprobe per file (slow); now it's all O(1) reads after first run.
    try:
        st = os.stat(path)
        ent = _probe_disk.get(path)
        if ent and ent.get("mtime") == int(st.st_mtime) and ent.get("size") == st.st_size:
            t = (float(ent["dur"]), int(ent["w"]), int(ent["h"]))
            _probe_cache[path] = t
            return t
    except OSError:
        st = None
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
    if st is not None:
        with _probe_cache_lock:
            _probe_disk[path] = {"mtime": int(st.st_mtime), "size": st.st_size,
                                 "dur": dur, "w": w, "h": h}
            _probe_dirty = True
    return _probe_cache[path]


BRAND_KEYS = ("meowijuana", "doggijuana", "kkz", "unassigned")     # canonical brand ids
BRAND_LABEL = {"meowijuana": "Meowijuana", "doggijuana": "Doggijuana", "kkz": "Kitty Ka-Zoom", "unassigned": "Unassigned"}
# Auto-defaults for known product folders. Anything not listed → "meowijuana" (most of the catalog).
# Per-product overrides live in sources/clips/_brands.json and beat these defaults.
BRAND_AUTODEFAULTS = {"juananip": "doggijuana"}
BRANDS_FILE = os.path.join(SRC_ROOT, "_brands.json")


def load_brand_map():
    try:
        if os.path.exists(BRANDS_FILE):
            return json.load(open(BRANDS_FILE, encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def save_brand_map(m):
    os.makedirs(SRC_ROOT, exist_ok=True)
    with open(BRANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, sort_keys=True)


def product_brand(product, brand_map=None):
    bm = brand_map if brand_map is not None else load_brand_map()
    v = bm.get(product)
    if v in BRAND_KEYS:
        return v
    return BRAND_AUTODEFAULTS.get(product, "meowijuana")


def _brand_from_path(full_path, product, bm):
    """If the clip lives under <SRC_ROOT>/<brand>/<product>/file, use that brand from disk.
    Otherwise fall back to the brand_map / autodefaults (legacy flat layout)."""
    rel = os.path.relpath(full_path, SRC_ROOT).replace("\\", "/")
    parts = rel.split("/")
    if len(parts) >= 3:                                # <brand>/<product>/<file>...
        b = parts[0].lower()
        if b in BRAND_KEYS:
            return b
    return product_brand(product, bm)


def _find_product_dir(product):
    """Return the absolute path of an existing product folder, whether it's at the legacy
    flat location (SRC_ROOT/<product>) OR nested under any brand (SRC_ROOT/<brand>/<product>)."""
    dirs = _find_all_product_dirs(product)
    return dirs[0] if dirs else None


def _find_all_product_dirs(product):
    """Return ALL absolute paths where a product folder exists — flat and nested under every brand.
    Used by setbrand to detect + merge duplicated folders (e.g. when the same product name
    ended up under two brand paths and the manifest scan is showing the clip twice)."""
    out = []
    flat = os.path.join(SRC_ROOT, product)
    if os.path.isdir(flat):
        out.append(flat)
    if os.path.isdir(SRC_ROOT):
        for b in os.listdir(SRC_ROOT):
            bp = os.path.join(SRC_ROOT, b)
            if not os.path.isdir(bp) or b.lower() not in BRAND_KEYS:
                continue
            nested = os.path.join(bp, product)
            if os.path.isdir(nested):
                out.append(nested)
    return out


def _cleanup_empty_dirs(path):
    """Remove a directory and any now-empty parent directories up to (but not including) SRC_ROOT.
    Safe no-op if path doesn't exist or isn't empty. Retries briefly on Windows where a rmdir
    right after a file remove can transiently fail with "directory not empty"."""
    import time as _t
    try:
        if not (path and os.path.isdir(path) and not os.listdir(path)):
            return
        for attempt in range(3):
            try:
                os.rmdir(path); break
            except OSError:
                if attempt == 2: return
                _t.sleep(0.05)
        parent = os.path.dirname(path)
        if parent and os.path.abspath(parent) != os.path.abspath(SRC_ROOT):
            _cleanup_empty_dirs(parent)
    except OSError:
        pass


def scan_sources():
    """Return list of clips: {id,label,product,brand,url,dur,w,h,tags}."""
    bm = load_brand_map()
    tags_map = load_clip_tags()
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
                          "brand": _brand_from_path(full, product, bm),
                          "url": "/" + urllib.parse.quote(rel), "file": full,
                          "dur": round(dur, 2), "w": w, "h": h,
                          "tags": tags_map.get(cid, [])})
    # sort by brand → product → label so the bin groups naturally
    brand_ord = {b: i for i, b in enumerate(BRAND_KEYS)}
    clips.sort(key=lambda c: (brand_ord.get(c["brand"], 99), c["product"], c["label"]))
    _probe_cache_save()                            # persist any new probes from this scan
    return clips


def id_to_file():
    return {c["id"]: c["file"] for c in scan_sources()}


def vcodec(path):
    r = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path])
    return (r.stdout or "").strip().lower()


def ingest_upload(raw, orig_name, category):
    """Save an uploaded video into sources/clips/<brand>/<category>/ as a browser-playable
    .mp4 (remux if already H.264, else re-encode). Returns (rel_path, category)."""
    cat = re.sub(r"[^A-Za-z0-9_-]", "", (category or "uploads").lower()) or "uploads"
    # Reuse an existing product folder if there is one (flat OR nested) so we don't fragment.
    existing = _find_product_dir(cat)
    if existing:
        dest_dir = existing
    else:
        brand = product_brand(cat)                     # brand_map / autodefaults → "meowijuana" if unknown
        dest_dir = os.path.join(SRC_ROOT, brand, cat)
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


_AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac")
def list_music():
    if not os.path.isdir(MUSIC_DIR):
        return []
    return sorted(f for f in os.listdir(MUSIC_DIR) if f.lower().endswith(_AUDIO_EXTS))
def list_sfx():
    if not os.path.isdir(SFX_DIR):
        os.makedirs(SFX_DIR, exist_ok=True)
        return []
    return sorted(f for f in os.listdir(SFX_DIR) if f.lower().endswith(_AUDIO_EXTS))


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
                       "url": c["url"], "dur": c["dur"], "w": c["w"], "h": c["h"],
                       "tags": c.get("tags") or []})
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


CANVAS = {"9x16": (1080, 1920), "4x5": (1080, 1350), "1x1": (1080, 1080), "16x9": (1920, 1080)}


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
    # KKZ brand slots — free-license lookalikes wired to real .ttf files on disk (so Pillow can render them).
    # Swap paths here when the real Ofelia/Caraque/Avenir .otf files are licensed + activated.
    "ofelia":  os.path.join(EDITOR, "fonts", "BarlowCondensed-ExtraBold.ttf"),
    "caraque": os.path.join(EDITOR, "fonts", "AlfaSlabOne-Regular.ttf"),
    "avenir":  "C:/Windows/Fonts/gothic.ttf",   # Century Gothic ships with Windows — closest free stand-in for Avenir Book
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


def _anim_exprs(o, s, dur, W, tv="t", H=None):
    """Build ffmpeg expressions (time var `tv`) for overlay animations:
    (dx_px, dy_px position offsets, alpha multiplier 0..1, has_opacity). Mirrors animState() in the UI.
    Falls back to legacy fadeIn/fadeOut when no `anims` list is present.
    H (canvas height in px) is optional — needed for moveTo's Y target scaling; defaults to 16:9 of W."""
    if H is None: H = int(W * 16 / 9)   # sensible default matching the 9x16 canvas; caller should pass real H
    e = s + dur
    anims = o.get("anims")
    if anims is None:
        anims = []
        if float(o.get("fadeIn", 0) or 0) > 0: anims.append({"type": "fadeIn", "d": float(o.get("fadeIn"))})
        if float(o.get("fadeOut", 0) or 0) > 0: anims.append({"type": "fadeOut", "d": float(o.get("fadeOut"))})
    aphase = float(o.get("aphase", 0) or 0)   # per-piece sprinkle animation phase offset
    dxs, dys, amul, rots = [], [], [], []
    for a in anims:
        # Per-animation window within the overlay; defaults to the whole overlay.
        aS = max(0.0, float(a.get("tStart", 0) or 0))
        aEv = a.get("tEnd")
        aE = min(dur, float(aEv)) if (aEv is not None and float(aEv) > 0) else dur
        if aE <= aS: continue
        dw = aE - aS                       # animation's local duration
        eA = s + aE                        # absolute timeline end of the window
        win_open, win_close = s + aS, eA
        lt = "((%s-%g)+%g)" % (tv, win_open, aphase) if aphase else "(%s-%g)" % (tv, win_open)
        gfps = float(a.get("gifFps", 0) or 0)             # GIF-look: quantize this anim's time
        if gfps > 0:
            lt = "(floor(%s*%g)/%g)" % (lt, gfps, gfps)
        gate = "between(%s,%g,%g)" % (tv, win_open, win_close)
        windowed = (aS > 0 or aE < dur)
        def add_dx(expr): dxs.append(("if(%s,%s,0)" % (gate, expr)) if windowed else expr)
        def add_dy(expr): dys.append(("if(%s,%s,0)" % (gate, expr)) if windowed else expr)
        def add_rot(expr): rots.append(("if(%s,%s,0)" % (gate, expr)) if windowed else expr)
        def add_amul(expr): amul.append(("if(%s,%s,1)" % (gate, expr)) if windowed else expr)
        ty = a.get("type")
        if ty == "fadeIn":
            d = max(0.01, float(a.get("d", 0.5))); add_amul("min(1,max(0,(%s)/%g))" % (lt, d))
        elif ty == "fadeOut":
            d = max(0.01, float(a.get("d", 0.5))); add_amul("min(1,max(0,(%g-%s)/%g))" % (eA, tv, d))
        elif ty == "pulse":
            amp = float(a.get("amp", 0.5)); sp = float(a.get("speed", 1.5)); add_amul("(1-%g*(0.5-0.5*cos(2*PI*%g*%s)))" % (amp, sp, lt))
        elif ty == "jitter":
            amp = float(a.get("amp", 0.012)) * W; sp = float(a.get("speed", 11))
            add_dx("%g*(sin(2*PI*%g*%s)+0.7*sin(2*PI*%g*%s+1.1))/1.7" % (amp, sp, lt, sp * 1.7, lt))
            add_dy("%g*(cos(2*PI*%g*%s)+0.7*sin(2*PI*%g*%s+0.5))/1.7" % (amp, sp * 1.3, lt, sp * 2.1, lt))
        elif ty == "float":
            amp = float(a.get("amp", 0.02)) * W; sp = float(a.get("speed", 0.6)); add_dy("%g*sin(2*PI*%g*%s)" % (amp, sp, lt))
        elif ty == "bounce":
            amp = float(a.get("amp", 0.05)) * W; sp = float(a.get("speed", 1)); add_dy("-%g*abs(sin(PI*%g*%s))" % (amp, sp, lt))
        elif ty == "slideIn":
            d = max(0.01, float(a.get("d", 0.5))); dist = float(a.get("dist", 0.2)) * W; dr = a.get("dir", "left")
            term = "if(lt(%s,%g),pow(1-%s/%g,2)*%g,0)" % (lt, d, lt, d, dist)
            term = ("-" if dr in ("left", "up") else "") + term
            (add_dx if dr in ("left", "right") else add_dy)(term)
        elif ty == "moveTo":   # ease from (o.x, o.y) to (a.x, a.y) over d seconds, then hold
            d = max(0.01, float(a.get("d", 1)))
            tx = float(a.get("x", o.get("x", 0.5)) or o.get("x", 0.5))
            ty_ = float(a.get("y", o.get("y", 0.5)) or o.get("y", 0.5))
            ox = float(o.get("x", 0.5) or 0.5); oy = float(o.get("y", 0.5) or 0.5)
            # ease-out cubic on k = clamp(lt/d, 0, 1); after t=d it holds at target (k=1 → ease=1)
            k = "min(1,max(0,%s/%g))" % (lt, d)
            ease = "(1-pow(1-(%s),3))" % k
            add_dx("%g*(%s)" % ((tx - ox) * W,  ease))
            add_dy("%g*(%s)" % ((ty_ - oy) * H, ease))
        elif ty == "bounceIn":
            d = max(0.01, float(a.get("d", 0.6))); amp = float(a.get("amp", 0.12)) * W
            add_dy("if(lt(%s,%g),-%g*exp(-3*%s/%g)*cos(2*PI*1.6*%s/%g)*(1-%s/%g),0)" % (lt, d, amp, lt, d, lt, d, lt, d))
        elif ty == "dropIn":
            d = max(0.01, float(a.get("d", 0.6))); dist = float(a.get("dist", 0.5)) * W
            k = "((%s)/%g)" % (lt, d); eb = "(1+2.70158*pow(%s-1,3)+1.70158*pow(%s-1,2))" % (k, k)
            add_dy("if(lt(%s,%g),-%g*(1-%s),0)" % (lt, d, dist, eb))
        elif ty == "popIn":
            d = max(0.01, float(a.get("d", 0.45)))   # scale isn't animatable on an ffmpeg overlay → render as a quick fade-in
            add_amul("min(1,max(0,(%s)/%g))" % (lt, d * 0.5))
        elif ty == "bubbleUp":
            d = max(0.01, float(a.get("d", 0.7)))
            dist = float(a.get("dist", 0.15)) * W
            sway = float(a.get("sway", 0.02)) * W
            add_dy("if(lt(%s,%g),%g*pow(1-%s/%g,3),0)" % (lt, d, dist, lt, d))           # rise (+dist → 0, cubic ease-out)
            add_dx("if(lt(%s,%g),%g*sin(2*PI*%s/%g),0)" % (lt, d, sway, lt, d))          # gentle horizontal sway
            add_amul("min(1,max(0,(%s)/%g))" % (lt, d * 0.3))                            # fade in over first 30%
        elif ty == "reshuffle":   # seeded per-step randomization; same hash math as _rsHash() in the UI
            freq = max(0.2, float(a.get("freq", 2))); amt = float(a.get("amt", 0.15)); rseed = float(a.get("seed", 1))
            def _H(salt):
                inner = "(floor(%s*%g)*12.9898+%g*78.233+%g*45.13)" % (lt, freq, rseed, salt)
                sx = "sin(%s)*43758.5453" % inner
                return "((%s)-floor(%s))*2-1" % (sx, sx)
            if a.get("posX"): add_dx("%g*(%s)" % (amt * W, _H(1.1)))
            if a.get("posY"): add_dy("%g*(%s)" % (amt * W, _H(2.3)))
            if a.get("rot"): add_rot("%g*(%s)" % (amt * math.pi, _H(3.7)))
            if a.get("opacity"): add_amul("max(0,1-((%s)*0.5+0.5)*%g)" % (_H(6.1), amt * 1.4))
        elif ty == "blink":
            sp = float(a.get("speed", 2)); add_amul("gte(sin(2*PI*%g*%s),0)" % (sp, lt))
        elif ty == "wiggle":
            amp = float(a.get("amp", 8)) * math.pi / 180.0; sp = float(a.get("speed", 2)); add_rot("%g*sin(2*PI*%g*%s)" % (amp, sp, lt))
        elif ty == "gifwobble":   # low-framerate stepped wobble (GIF sticker look); time quantized via floor
            fps = max(2.0, float(a.get("fps", 8))); amp = float(a.get("amp", 0.6)); sp = float(a.get("speed", 2))
            tq = "(floor(%s*%g)/%g)" % (lt, fps, fps)
            add_rot("%g*sin(2*PI*%g*%s)" % (math.radians(amp * 10), sp, tq))
            j = amp * 0.01 * W
            add_dx("%g*sin(2*PI*%g*%s+1.7)" % (j, sp * 1.3, tq))
            add_dy("%g*cos(2*PI*%g*%s+0.6)" % (j, sp * 0.9, tq))
        elif ty == "spin":
            sp = float(a.get("speed", 0.5)); sign = -1 if a.get("dir") == "ccw" else 1; add_rot("%g*2*PI*%g*%s" % (sign, sp, lt))
        # NOTE: distort (psychedelic warp) is intentionally NOT handled here — it's a pixel-level
        # geq warp + hue rotation applied as separate filter passes in apply_overlays, not a
        # transform on the layer as a whole.
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


_FONT_CMAP_CACHE = {}
_FONT_TOOLS_OK = None   # None = untested, True = importable, False = missing
def _font_has_char(font_path, codepoint):
    """True iff the font's cmap actually maps this codepoint to a real glyph (not .notdef).
    Handles .ttc collections (opens face #0) and is conservative: if a font file can't be
    parsed, we treat it as having NOTHING (skip it) so we don't pick it and render tofu."""
    global _FONT_TOOLS_OK
    if _FONT_TOOLS_OK is None:
        try:
            import fontTools.ttLib   # noqa: F401
            _FONT_TOOLS_OK = True
        except Exception:
            _FONT_TOOLS_OK = False
    if not _FONT_TOOLS_OK:
        return True                              # no way to check → preserve old assume-yes behavior
    cm = _FONT_CMAP_CACHE.get(font_path)
    if cm is None:
        cm = set()
        try:
            from fontTools.ttLib import TTFont, TTLibFileIsCollectionError
            try:
                tt = TTFont(font_path, lazy=True)
            except TTLibFileIsCollectionError:   # .ttc → open the first face explicitly
                tt = TTFont(font_path, lazy=True, fontNumber=0)
            for table in tt["cmap"].tables:
                for cp, glyph_name in table.cmap.items():
                    if glyph_name and glyph_name != ".notdef":
                        cm.add(cp)
            tt.close()
        except Exception:
            cm = set()                           # unreadable → skip this font (don't false-positive)
        _FONT_CMAP_CACHE[font_path] = cm
    return codepoint in cm


def _emoji_png(emoji, out, px=256):
    """Rasterize an emoji / decorative sparkle string to a transparent PNG with FONT FALLBACK.
    Each char picks the first font whose CMAP ACTUALLY has it (Pillow has no per-glyph fallback,
    and .notdef tofu glyphs would otherwise sneak through a naive 'rendered pixels' check).
    So "˗ˏˋ ✸ ˎˊ˗" renders the modifier marks via Lucida/Cambria and the star via emoji font."""
    from PIL import Image, ImageDraw, ImageFont
    candidates = [
        ("C:/Windows/Fonts/seguiemj.ttf",         True),    # color emoji (✨ 🎉 ⭐)
        ("C:/Windows/Fonts/seguisym.ttf",         False),   # Segoe UI Symbol (✸ ✶ ⋆ ★)
        ("C:/Windows/Fonts/seguihis.ttf",         False),   # Segoe UI Historic (many ancient scripts)
        ("C:/Windows/Fonts/l_10646.ttf",          False),   # Lucida Sans Unicode (modifier letters ˗ˏˋ)
        ("C:/Windows/Fonts/lucon.ttf",            False),   # Lucida Console (broad coverage fallback)
        ("C:/Windows/Fonts/cambria.ttc",          False),   # Cambria (broad symbol coverage)
        ("C:/Windows/Fonts/SansSerifCollection.ttf", False), # Win11 sans-serif COLLECTION — catch-all for rare planes (Anatolian Hieroglyphs etc.)
        ("C:/Windows/Fonts/segoeui.ttf",          False),
        ("C:/Windows/Fonts/arial.ttf",            False),
    ]
    fonts = []
    for path, is_color in candidates:
        try:
            fonts.append((path, is_color, ImageFont.truetype(path, px)))
        except Exception:
            pass
    if not fonts:
        Image.new("RGBA", (1, 1)).save(out); return

    def pick(ch):
        cp = ord(ch)
        for path, is_color, f in fonts:
            if _font_has_char(path, cp):
                return f, is_color
        return fonts[-1][2], fonts[-1][1]

    if not emoji:
        Image.new("RGBA", (1, 1)).save(out); return

    pad = int(px * 0.05)
    pieces = []
    for ch in emoji:
        if ch.isspace():
            pieces.append((None, int(px * 0.3))); continue
        f, is_color = pick(ch)
        cell = Image.new("RGBA", (px * 2, px * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(cell)
        try:
            d.text((px, px), ch, font=f, embedded_color=is_color, anchor="mm", fill=(255, 255, 255, 255))
        except Exception:
            d.text((px, px), ch, font=f, anchor="mm", fill=(255, 255, 255, 255))
        bb = cell.getbbox()
        if not bb: pieces.append((None, int(px * 0.15))); continue
        pieces.append((cell.crop(bb), cell.crop(bb).width))

    real = [p for p in pieces if p[0] is not None]
    if not real:
        Image.new("RGBA", (1, 1)).save(out); return

    total_w = sum(w for _, w in pieces) + max(0, len(pieces) - 1) * pad
    max_h = max(p[0].height for p in real)
    out_img = Image.new("RGBA", (max(1, total_w), max(1, max_h)), (0, 0, 0, 0))
    x = 0
    for img, w in pieces:
        if img is not None:
            out_img.paste(img, (x, (max_h - img.height) // 2), img)
        x += w + pad
    out_img.save(out)


def prerender_piececlip(o, W, H, tmp, k):
    """Render a per-piece sprinkle's N pieces onto a SHORT transparent clip (length = dur).
    Each piece animates with its own phase offset. Returns the clip path (qtrle MOV w/ alpha),
    or None to fall back to inline compositing. Compositing N pieces here runs only for the
    sprinkle's window, not the whole timeline — the big win."""
    pieces = o.get("pieces") or []
    dur = float(o.get("dur", 0) or 0)
    src = o.get("src")
    if not pieces or dur <= 0 or not src:
        return None
    p = os.path.join(PROJ, *src.split("/"))
    if not os.path.exists(p):
        return None
    anims = o.get("anims") or []
    n = len(pieces)
    # transparent base (alpha 0) + one shared piece input split n ways (decode once, not n times)
    inputs = ["-f", "lavfi", "-i", "nullsrc=s=%dx%d:r=60:d=%g,format=rgba,colorchannelmixer=aa=0" % (W, H, dur)]
    inputs += ["-framerate", "60", "-loop", "1", "-t", str(dur), "-i", p]
    fc = ["[0:v]format=rgba[base0]"]
    last = "base0"
    if n > 1:
        fc.append("[1:v]split=%d%s" % (n, "".join("[ps%d]" % i for i in range(n))))
        srcs = ["ps%d" % i for i in range(n)]
    else:
        srcs = ["1:v"]
    for i, pc in enumerate(pieces):
        po = {"start": 0, "dur": dur, "aphase": pc.get("aphase", 0), "anims": anims, "rot": pc.get("rot", 0)}
        ox = float(pc.get("x", 0.5)); oy = float(pc.get("y", 0.5))
        sw = max(1, int(W * float(pc.get("scale", 0.08))))
        odx, ody, _, _, orot = _anim_exprs(po, 0, dur, W, "t")    # position + anim rotation (overlay/clip time = t)
        _, _, amT, has_op, _ = _anim_exprs(po, 0, dur, W, "T")    # opacity via geq (pixel time = T)
        filt = ["scale=%d:-1" % sw, "format=rgba"]
        # cycle-hue support for per-piece sprinkles (each piece animates with its own phase via aphase)
        aphase = float(pc.get("aphase", 0) or 0)
        for a in anims:
            if a.get("type") != "cycleHue": continue
            aS = max(0.0, float(a.get("tStart", 0) or 0)); aEv = a.get("tEnd")
            aE = min(dur, float(aEv)) if (aEv is not None and float(aEv) > 0) else dur
            if aE <= aS: continue
            sp = float(a.get("speed", 0.5)); off = float(a.get("offset", 0))
            expr = "if(between(t,%g,%g),%g+%g*((t-%g)+%g),0)" % (aS, aE, off, sp * 360.0, aS, aphase)
            filt.append("hue=h='%s'" % expr)
        if has_op:
            filt.append("geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*(%s)'" % amT)
        srot = float(pc.get("rot", 0) or 0)
        rot_terms = ([orot] if orot else []) + (["%.6f" % math.radians(srot)] if abs(srot) > 1e-6 else [])
        if rot_terms:
            rexpr = "+".join("(%s)" % r for r in rot_terms)
            filt.append("pad=ceil(iw*1.08):ceil(ih*1.08):(ow-iw)/2:(oh-ih)/2:color=black@0")
            filt.append("rotate='%s':c=none:ow='hypot(iw,ih)':oh='hypot(iw,ih)'" % rexpr)
        # popIn anim-scale AFTER pad+rotate so pad's static output box doesn't cap subsequent
        # larger-scale frames (same "chopped top" bug as the main overlay chain — fixed alongside).
        pop = next((a for a in anims if a.get("type") == "popIn"), None)
        if pop:
            d = max(0.01, float(pop.get("d", 0.45)))
            kk = "(t/%g)" % d; eb = "(1+2.70158*pow(%s-1,3)+1.70158*pow(%s-1,2))" % (kk, kk)
            pops = "if(between(t,0,%g),max(0.05,%s),1)" % (d, eb)
            filt.append("scale=w='iw*(%s)':h='ih*(%s)':eval=frame:flags=bicubic" % (pops, pops))   # per-piece sprinkle popIn — bicubic for clean per-frame resizing
        fc.append("[%s]%s[oi%d]" % (srcs[i], ",".join(filt), i))
        fc.append("[%s][oi%d]overlay=x='W*%g-w/2+(%s)':y='H*%g-h/2+(%s)'[v%d]" % (last, i, ox, odx, oy, ody, i))
        last = "v%d" % i
    out = os.path.join(tmp, "piececlip_%d.mov" % k)
    fc_path = os.path.join(tmp, "pc_fg_%d.txt" % k)
    with open(fc_path, "w", encoding="utf-8") as fh:
        fh.write(";\n".join(fc))
    r = run([FFMPEG, "-y", "-loglevel", "error"] + inputs
            + ["-filter_complex_script", fc_path, "-map", "[%s]" % last, "-c:v", "qtrle", "-pix_fmt", "argb", out])
    if r.returncode != 0 or not os.path.exists(out):
        return None
    return out


CF_POLAROID = {"padL": 0.06, "padR": 0.06, "padT": 0.06, "padB": 0.22}


def gen_distort_noise_map(w, h, amp_px, freq, octaves, seed, out_path):
    """Fractal noise displacement PNG matching SVG feTurbulence character (independent R & G channels
    for X and Y displacement, multi-octave value noise summed). Written as RGBA where R = X-offset,
    G = Y-offset, both centered at 128 with ±amp_px range. Used by ffmpeg `displace` filter."""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(seed)
    field = np.zeros((h, w, 2), dtype=np.float64)
    total_amp = 0.0
    cell = max(4, int(min(w, h) / max(0.5, freq)))
    layer_amp = 1.0
    for _ in range(max(1, int(octaves))):
        gw = max(2, w // cell + 2)
        gh = max(2, h // cell + 2)
        # Independent random grids for X and Y displacement so the warp has 2D character.
        grid_r = (rng.random((gh, gw)) * 255).astype(np.uint8)
        grid_g = (rng.random((gh, gw)) * 255).astype(np.uint8)
        # Bilinear upscale via PIL (cheap and smooth)
        up_r = np.array(Image.fromarray(grid_r).resize((w, h), Image.BILINEAR)).astype(np.float64) / 255.0
        up_g = np.array(Image.fromarray(grid_g).resize((w, h), Image.BILINEAR)).astype(np.float64) / 255.0
        field[:, :, 0] += up_r * layer_amp
        field[:, :, 1] += up_g * layer_amp
        total_amp += layer_amp
        layer_amp *= 0.5
        cell = max(2, cell // 2)
    field = field / total_amp                        # normalize to [0, 1]
    disp = 128.0 + amp_px * (2.0 * field - 1.0)      # center at 128, range ±amp_px
    disp = np.clip(disp, 0, 255).astype(np.uint8)
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[:, :, 0] = disp[:, :, 0]                    # R = X displacement
    rgba[:, :, 1] = disp[:, :, 1]                    # G = Y displacement
    rgba[:, :, 2] = 128
    rgba[:, :, 3] = 255
    Image.fromarray(rgba, "RGBA").save(out_path)

def _cf_mask_png(frame, out_w, out_h, path):
    """Draw an alpha mask for a clipframe shape (white where content, transparent elsewhere).
    Frame is one of 'polaroid'|'circle'|'roundrect'|'rect'|'star'. Path is the mask PNG output."""
    from PIL import Image, ImageDraw
    img = Image.new("L", (out_w, out_h), 0)
    d = ImageDraw.Draw(img)
    if frame == "circle":
        d.ellipse([0, 0, out_w - 1, out_h - 1], fill=255)
    elif frame == "roundrect":
        r = int(min(out_w, out_h) * 0.12)
        d.rounded_rectangle([0, 0, out_w - 1, out_h - 1], radius=r, fill=255)
    elif frame == "star":
        cx, cy = out_w / 2.0, out_h / 2.0
        R = min(out_w, out_h) / 2.0
        r = R * 0.42
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rad = R if i % 2 == 0 else r
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        d.polygon(pts, fill=255)
    elif frame == "trapezoid":
        # Short top, wide bottom.
        d.polygon([(0.18 * out_w, 0), (0.82 * out_w, 0),
                   (out_w, out_h), (0, out_h)], fill=255)
    elif frame == "parallelogram":
        # Slanted rectangle — top offset right, bottom offset left.
        d.polygon([(0.22 * out_w, 0), (out_w, 0),
                   (0.78 * out_w, out_h), (0, out_h)], fill=255)
    elif frame == "bolt":
        # Chunky Z-bolt (KKZ style) — same coords as the CSS clip-path (fractions of w/h),
        # clockwise from the top-right corner of the upper stroke.
        pts = [(0.70, 0.00), (0.60, 0.50), (0.82, 0.45), (0.55, 1.00),
               (0.30, 1.00), (0.40, 0.45), (0.18, 0.50), (0.35, 0.00)]
        d.polygon([(fx * out_w, fy * out_h) for (fx, fy) in pts], fill=255)
    else:
        d.rectangle([0, 0, out_w - 1, out_h - 1], fill=255)   # rect / polaroid inner
    img.save(path)


def prerender_clipframe(o, W, H, tmp, k):
    """Render a picture-in-picture clipframe to a short transparent MOV (qtrle w/ alpha).
    Trims the source clip to (srcIn, srcIn+dur*speed), speeds it up 1/spd to fit dur, masks it
    to the chosen shape, and (for polaroid) pads with a solid border. Returns MOV path or None."""
    src = o.get("_src")
    dur = float(o.get("dur", 0) or 0)
    if not src or dur <= 0 or not os.path.exists(src):
        return None
    frame = (o.get("frame") or "polaroid").lower()
    src_in = max(0.0, float(o.get("srcIn", 0) or 0))
    spd = max(0.1, float(o.get("srcSpeed", 1) or 1))
    # Probe source aspect (fall back to square if unknown)
    src_ar = 1.0
    try:
        r = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", src])
        w_s, h_s = (r.stdout or "").strip().split(",")
        if int(w_s) > 0 and int(h_s) > 0:
            src_ar = int(w_s) / int(h_s)
    except Exception:
        pass
    # Outer dims: `w` (fraction of canvas W) + `h` (fraction of canvas H) — independent sliders.
    # Legacy fallback: single `scale` field with a per-frame fixed aspect (kept only so older
    # projects saved before v1.68.3 still render at their original size).
    CF_AR = {"polaroid": 0.82, "circle": 1.0, "star": 1.0, "rect": 1.0, "roundrect": 1.0,
             "trapezoid": 1.2, "parallelogram": 1.5, "bolt": 0.9}
    outer_ar = CF_AR.get(frame, 1.0)
    if o.get("w") is not None and o.get("h") is not None:
        outer_w = max(4, int(W * float(o["w"])))
        outer_h = max(4, int(H * float(o["h"])))
    else:
        scale = float(o.get("scale", 0.4))
        outer_w = max(4, int(W * scale))
        outer_h = max(4, int(outer_w / outer_ar))
    # Source clip is CROPPED to fill the inner rect ("cover" behavior) so a portrait 9:16 UGC
    # clip doesn't stretch the polaroid into a tall rectangle with an oversized bottom strip.
    if frame == "polaroid":
        inner_x = int(outer_w * CF_POLAROID["padL"])
        inner_y = int(outer_h * CF_POLAROID["padT"])
        inner_w = max(2, int(outer_w * (1 - CF_POLAROID["padL"] - CF_POLAROID["padR"])))
        inner_h = max(2, int(outer_h * (1 - CF_POLAROID["padT"] - CF_POLAROID["padB"])))
    else:
        inner_x = inner_y = 0
        inner_w, inner_h = outer_w, outer_h
    # Make even (yuv420p / VP9 friendly)
    outer_w -= outer_w % 2; outer_h -= outer_h % 2
    inner_w -= inner_w % 2; inner_h -= inner_h % 2
    # Mask for the whole outer canvas (transparent outside the shape)
    mask_path = os.path.join(tmp, "cf_mask_%d.png" % k)
    _cf_mask_png(frame, outer_w, outer_h, mask_path)
    # Frame color (polaroid only)
    fc_color = (o.get("frameColor") or "#ffffff").lstrip("#")
    fc_rgb = "0x" + (fc_color if len(fc_color) == 6 else "ffffff")
    # Trim length from source (before speed-up)
    take = dur * spd
    src_take = min(take, max(0.1, take))
    # Inputs
    # [0] source clip (trimmed)
    # [1] mask PNG (single-frame, looped)
    inputs = ["-ss", "%g" % src_in, "-t", "%g" % src_take, "-i", src,
              "-loop", "1", "-t", "%g" % dur, "-i", mask_path]
    # Build filter graph
    parts = []
    bg = fc_rgb if frame == "polaroid" else "black"     # ffmpeg color: "0xRRGGBB" or a named color — 8-hex ("0x00000000") is invalid
    bg_a = 1.0 if frame == "polaroid" else 0.0
    # Speed up + scale to inner (COVER: fill, then crop overflow — matches CSS object-fit:cover)
    # + pad to outer with border color (transparent for non-polaroid).
    parts.append("[0:v]setpts=(PTS-STARTPTS)/%g,"
                 "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                 "format=rgba,pad=%d:%d:%d:%d:color=%s@%g,setpts=PTS-STARTPTS[fg]"
                 % (spd, inner_w, inner_h, inner_w, inner_h,
                    outer_w, outer_h, inner_x, inner_y, bg, bg_a))
    parts.append("[1:v]format=gray[m]")
    parts.append("[fg][m]alphamerge[outv]")
    fc_txt = ";".join(parts)
    out = os.path.join(tmp, "cf_%d.mov" % k)
    r = run([FFMPEG, "-y", "-loglevel", "error"] + inputs
            + ["-filter_complex", fc_txt, "-map", "[outv]",
               "-c:v", "qtrle", "-pix_fmt", "argb", "-t", "%g" % dur, out])
    if r.returncode != 0 or not os.path.exists(out):
        return None
    return out


def apply_overlays(silent, overlays, W, H, tmp):
    """Composite free-floating text/image/shape overlays over the full timeline (global time),
    preserving list order as z-order (later = on top)."""
    overlays = [o for o in (overlays or []) if isinstance(o, dict) and o.get("type") in ("text", "image", "shape", "piececlip", "clipframe") and not o.get("hidden")]
    if not overlays:
        return silent, None
    overlays = sorted(overlays, key=lambda o: int(o.get("ch", 0) or 0))   # higher channel composited later = on top (stable within a channel)
    # Per-piece sprinkles arrive as a single 'piececlip'. Pre-render their pieces onto a SHORT
    # transparent clip (only the [start,end] window), so the heavy N-piece composite runs once
    # for the window instead of across the whole timeline. Fall back to inline pieces on failure.
    expanded = []
    i2f = None   # lazily built id → source file map for clipframe lookups
    for o in overlays:
        if o.get("type") == "piececlip":
            clip = prerender_piececlip(o, W, H, tmp, len(expanded))
            if clip:
                oo = dict(o); oo["_clip"] = clip; expanded.append(oo)
            else:
                for pc in (o.get("pieces") or []):
                    expanded.append({"type": "image", "src": o.get("src"), "x": pc.get("x", 0.5), "y": pc.get("y", 0.5),
                                     "scale": pc.get("scale", 0.08), "start": o.get("start", 0), "dur": o.get("dur", 3),
                                     "ch": o.get("ch", 0), "rot": pc.get("rot", 0), "aphase": pc.get("aphase", 0), "anims": o.get("anims")})
        elif o.get("type") == "clipframe":
            if i2f is None:
                i2f = id_to_file()
            src_path = i2f.get(o.get("srcId"))
            if not src_path or not os.path.exists(src_path):
                continue                                           # missing source — skip silently (project.json can outlive a deleted clip)
            oo = dict(o); oo["_src"] = src_path
            clip = prerender_clipframe(oo, W, H, tmp, len(expanded))
            if clip:
                oo["_clip"] = clip
                expanded.append(oo)
        else:
            expanded.append(o)
    overlays = expanded
    inputs = ["-i", silent]
    fc = []
    # Normalize the base to clean CFR/timestamps first. The concatenated video can carry
    # duplicate/irregular timestamps from tpad freeze-fill, which makes the overlay+geq
    # compositing pass pathologically slow (minutes). fps=60 + reset PTS fixes that.
    fc.append("[0:v]fps=60,setpts=PTS-STARTPTS[base0]")
    last = "base0"
    ii = 1  # next ffmpeg input index for image/shape files
    for k, o in enumerate(overlays):
        t = o.get("type")
        s = float(o.get("start", 0)); e = s + float(o.get("dur", 3))
        ox, oy = float(o.get("x", 0.5)), float(o.get("y", 0.5))
        en = f"enable='between(t,{s},{e})'"
        if t == "piececlip":                       # pre-rendered per-piece sprinkle clip, shown only in its window
            clip = o.get("_clip")
            if clip:
                inputs += ["-i", clip]
                fc.append(f"[{ii}:v]setpts=PTS-STARTPTS+{s}/TB,format=rgba[pcc{k}]")
                fc.append(f"[{last}][pcc{k}]overlay=0:0:{en}:eof_action=pass[v{k}]")
                last = f"v{k}"; ii += 1
            continue
        if t == "clipframe":                       # picture-in-picture: pre-rendered short MOV, place at overlay center
            clip = o.get("_clip")
            if not clip:
                continue
            inputs += ["-i", clip]
            filt = [f"setpts=PTS-STARTPTS+{s}/TB", "format=rgba"]
            # Static rotation (degrees on the overlay). Pad transparent margin first so rotate
            # doesn't smear edge pixels of the polaroid rectangle. Same pattern as image overlays.
            srot = float(o.get("rot", 0) or 0)
            if abs(srot) > 1e-6:
                filt.append("pad=ceil(iw*1.08):ceil(ih*1.08):(ow-iw)/2:(oh-ih)/2:color=black@0")
                filt.append(f"rotate='{math.radians(srot):.6f}':c=none:ow='hypot(iw,ih)':oh='hypot(iw,ih)'")
            fc.append(f"[{ii}:v]" + ",".join(filt) + f"[cfp{k}]")
            fc.append(f"[{last}][cfp{k}]overlay=x='W*{ox}-w/2':y='H*{oy}-h/2':{en}:eof_action=pass[v{k}]")
            last = f"v{k}"; ii += 1
            continue
        if t == "text" and o.get("style") != "sticker":
            if not (o.get("text") or "").strip():
                continue
            tf = os.path.join(tmp, f"ov_{k}.txt")
            with open(tf, "w", encoding="utf-8") as fh:
                fh.write(o["text"])
            ff = esc_path(_font_path(o.get("font", "cooper")))
            size = max(8, int(W * float(o.get("size", 0.06))))
            col = (o.get("color", "#ffffff") or "#ffffff").replace("#", "0x")
            adx, ady, aam, _, _ = _anim_exprs(o, s, float(o.get("dur", 3)), W, "t", H=H)
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
            anim_img = False
            if t == "image":
                p = os.path.join(PROJ, *o["src"].split("/"))
                if not os.path.exists(p):
                    continue
                anim_img = os.path.splitext(p)[1].lower() in (".gif", ".webp", ".apng")   # animated overlay → loop it
                if anim_img:
                    scale_w = max(1, int(W * float(o.get("scale", 0.3))))   # shadow PIL would freeze frame 1; skip for animated
                elif (o.get("shadow") or {}).get("on"):
                    from PIL import Image
                    im = Image.open(p).convert("RGBA")
                    tw = max(1, int(W * float(o.get("scale", 0.3)))); th = max(1, int(im.height * tw / im.width))
                    im = _with_shadow(im.resize((tw, th)), o, W)
                    p = os.path.join(tmp, f"img_{k}.png"); im.save(p); scale_w = None
                else:
                    scale_w = max(1, int(W * float(o.get("scale", 0.3))))
            dur_o = float(o.get("dur", 3))
            odx, ody, _, _, orot = _anim_exprs(o, s, dur_o, W, "t", H=H)     # position + rotation (overlay time = t)
            _, _, amT, has_op, _ = _anim_exprs(o, s, dur_o, W, "T", H=H)     # opacity (geq pixel time = T)
            if anim_img:
                inputs += ["-stream_loop", "-1", "-t", str(e), "-i", p]   # play+loop the animation over the timeline
            else:
                inputs += ["-framerate", "60", "-loop", "1", "-t", str(e), "-i", p]
            filt = []
            if scale_w:
                filt.append(f"scale={scale_w}:-1")
            filt.append("format=rgba")
            # Cycle-hue animation — apply ffmpeg hue=h=<expr> for each cycleHue anim, respecting its tStart/tEnd window
            for a in (o.get("anims") or []):
                if a.get("type") != "cycleHue": continue
                aS = s + max(0.0, float(a.get("tStart", 0) or 0))
                aEv = a.get("tEnd"); aE = s + min(dur_o, float(aEv)) if (aEv is not None and float(aEv) > 0) else (s + dur_o)
                if aE <= aS: continue
                sp = float(a.get("speed", 0.5)); off = float(a.get("offset", 0))
                expr = "if(between(t,%g,%g),%g+%g*(t-%g),0)" % (aS, aE, off, sp * 360.0, aS)
                filt.append("hue=h='%s'" % expr)
            # Distort (psychedelic warp) — pre-bake a fractal noise displacement PNG (same character
            # as the client's SVG feTurbulence) and apply via ffmpeg `displace` filter. Emitted as
            # a graph OUTSIDE the filt chain (see below where the fc entry is built). Only the hue
            # rotation from distort still folds into the in-chain filter list.
            distort = next((a for a in (o.get("anims") or []) if a.get("type") == "distort"), None)
            distort_noise_png = None
            distort_noise_png_b = None
            distort_blend_speed = 0.0
            distort_window = None
            if distort:
                aS_d = s + max(0.0, float(distort.get("tStart", 0) or 0))
                aEv_d = distort.get("tEnd"); aE_d = s + min(dur_o, float(aEv_d)) if (aEv_d is not None and float(aEv_d) > 0) else (s + dur_o)
                if aE_d > aS_d:
                    amp_frac = float(distort.get("amp", 0.025))
                    freq = float(distort.get("freq", 2.5))
                    dspd = float(distort.get("speed", 1.2))
                    octaves = max(1, min(4, int(round(float(distort.get("octaves", 2))))))
                    smooth = max(0.0, min(1.0, float(distort.get("smooth", 0.3))))
                    # Fixed seed per overlay id — same noise pattern across renders of the same project.
                    seed = abs(hash(o.get("id", "d") + str(freq) + str(octaves))) & 0xffff
                    # Noise maps at moderate resolution (256×256). Two seeds, cross-blended over time
                    # via ffmpeg blend filter — matches the client SVG's feComposite k2/k3 oscillation
                    # so the warp FLOWS instead of being frozen for the whole clip. amp_px is in
                    # canvas pixel units (halved for SVG feDisplacementMap parity).
                    map_size = 256
                    amp_px = amp_frac * W * 0.5
                    try:
                        distort_noise_png = os.path.join(tmp, f"dnoise_{k}_a.png")
                        gen_distort_noise_map(map_size, map_size, amp_px, freq, octaves, seed, distort_noise_png)
                        distort_noise_png_b = os.path.join(tmp, f"dnoise_{k}_b.png")
                        gen_distort_noise_map(map_size, map_size, amp_px, freq, octaves, seed ^ 0xA5A5, distort_noise_png_b)
                    except Exception:
                        distort_noise_png = None
                        distort_noise_png_b = None
                    # Blend cycle period matches the client's cross-fade: same 'speed' param.
                    distort_blend_speed = dspd * 0.5
                    distort_window = (aS_d, aE_d)
                    # gblur sigma on the SCALED noise map (canvas coords). Matches client's smoothPx.
                    # Coefficient tuned so smooth=1 produces a clearly liquid warp, not a subtle one.
                    distort_smooth_sigma = smooth * W * 0.15
                    hspd = float(distort.get("hue", 0.35))
                    if hspd > 0:
                        hexpr = "if(between(t,%g,%g),%g*(t-%g),0)" % (aS_d, aE_d, hspd * 360.0, aS_d)
                        filt.append("hue=h='%s'" % hexpr)
            # (Blur anim is emitted OUTSIDE the filt list — see below where the fc entry is built.)
            static_op = float(o.get("opacity", 1) or 1)   # image overlay's static opacity (text-sticker / shape / sprinkle / arrow bake it into the PNG; only image needs this here)
            if static_op < 0.999:
                filt.append("colorchannelmixer=aa=%g" % max(0, min(1, static_op)))
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
            # Combine ALL scale-related anims (popIn / scaleUp / scaleDown / scaleBeat) into one
            # multiplied expression and emit a single scale=eval=frame filter. Each anim respects
            # its own tStart/tEnd window; outside the window the factor is 1 (identity).
            # Track peak_sc = biggest scale factor this overlay can hit — used to pad the scaled
            # output to a CONSTANT size so the overlay filter's position doesn't jitter as the
            # scaled dimensions change by ±1px per frame (the real cause of "scale-pulse jitter").
            sc_factors = []
            peak_sc = 1.0
            for a in (o.get("anims") or []):
                ty = a.get("type")
                if ty not in ("popIn", "scaleUp", "scaleDown", "scaleBeat", "bubbleUp"): continue
                aS = s + max(0.0, float(a.get("tStart", 0) or 0))
                aEv = a.get("tEnd"); aE = s + min(dur_o, float(aEv)) if (aEv is not None and float(aEv) > 0) else (s + dur_o)
                if aE <= aS: continue
                lt = "(t-%g)" % aS; dw = aE - aS
                if ty == "scaleBeat":     peak_sc *= (1.0 + abs(float(a.get("amp", 0.15))))
                elif ty == "popIn":       peak_sc *= 1.15   # ease-out-back overshoot
                elif ty == "bubbleUp":    peak_sc *= 1.15   # same easing family
                if ty == "popIn":
                    d = max(0.01, float(a.get("d", 0.45)))
                    kk = "(%s/%g)" % (lt, d); eb = "(1+2.70158*pow(%s-1,3)+1.70158*pow(%s-1,2))" % (kk, kk)
                    sc_factors.append("if(between(t,%g,%g),max(0.05,%s),1)" % (aS, aS + d, eb))
                elif ty == "scaleUp":
                    d = max(0.01, float(a.get("d", 0.5))); fr = float(a.get("from", 0.3))
                    kk = "(%s/%g)" % (lt, d); ease = "(1-pow(1-%s,3))" % kk
                    sc_factors.append("if(between(t,%g,%g),%g+(1-%g)*%s,1)" % (aS, aS + d, fr, fr, ease))
                elif ty == "scaleDown":
                    d = max(0.01, float(a.get("d", 0.5))); to = float(a.get("to", 0))
                    tail = aE - d
                    kk = "((%g-t)/%g)" % (aE, d); ease = "(1-pow(1-%s,3))" % kk
                    sc_factors.append("if(between(t,%g,%g),%g+(1-%g)*%s,1)" % (tail, aE, to, to, ease))
                elif ty == "scaleBeat":
                    amp = float(a.get("amp", 0.15)); sp = float(a.get("speed", 1.5))
                    sc_factors.append("if(between(t,%g,%g),max(0.05,1+%g*sin(2*PI*%g*%s)),1)" % (aS, aE, amp, sp, lt))
                elif ty == "bubbleUp":
                    # scale 0 → 1 with ease-out-back (same overshoot curve as popIn — the bubble "pops" in as it rises)
                    d = max(0.01, float(a.get("d", 0.7)))
                    kk = "(%s/%g)" % (lt, d); eb = "(1+2.70158*pow(%s-1,3)+1.70158*pow(%s-1,2))" % (kk, kk)
                    sc_factors.append("if(between(t,%g,%g),max(0.05,%s),1)" % (aS, aS + d, eb))
            srot = float(o.get("rot", 0) or 0)                          # static rotation (degrees) + any anim rotation
            rot_terms = ([orot] if orot else []) + ([f"{math.radians(srot):.6f}"] if abs(srot) > 1e-6 else [])
            if rot_terms:
                rexpr = "+".join(f"({r})" for r in rot_terms)
                # transparent margin first so the rotate doesn't smear edge pixels of overlays
                # whose content touches the PNG boundary (caused ghost streaks on wobble/spin)
                filt.append("pad=ceil(iw*1.08):ceil(ih*1.08):(ow-iw)/2:(oh-ih)/2:color=black@0")
                filt.append(f"rotate='{rexpr}':c=none:ow='hypot(iw,ih)':oh='hypot(iw,ih)'")
            # Anim scale (popIn / scaleUp / scaleDown / scaleBeat / bubbleUp) goes LAST so its
            # eval=frame dimension changes don't fight pad's/rotate's static output sizes —
            # otherwise pad caps the output box at the first frame's tiny size (e.g. bubble at scale 0.05)
            # and subsequent larger frames get cropped (the "top of sticker chopped off" bug).
            if sc_factors:
                combined = "*".join("(%s)" % x for x in sc_factors)
                # Plain scale with lanczos + explicit 60fps. The visible "jitter" some users
                # perceive on small-amplitude pulses (amp <0.08) is a fundamental ffmpeg limitation:
                # scale outputs are integer pixels only, so a smoothly-varying scale factor lands
                # on discrete integer sizes with ~1-2 pixel jumps per frame. Larger amps (0.1+)
                # hide it because per-frame motion swamps the quantization step.
                filt.append("scale=w='round(iw*(%s))':h='round(ih*(%s))':eval=frame:flags=lanczos"
                            % (combined, combined))
                filt.append("fps=60")
            # Blur anim: split into original + pre-blurred branches, alpha-modulate the blurred
            # branch by the pattern time-curve, then overlay them. gblur runs once at max sigma;
            # per-frame alpha lerps between the two branches for pulse/in/out patterns.
            blur = next((a for a in (o.get("anims") or []) if a.get("type") == "blur"), None)
            blur_expr = None
            if blur:
                b_aS = s + max(0.0, float(blur.get("tStart", 0) or 0))
                b_aEv = blur.get("tEnd")
                b_aE = s + min(dur_o, float(b_aEv)) if (b_aEv is not None and float(b_aEv) > 0) else (s + dur_o)
                maxR = float(blur.get("amount", 0.008)) * W
                if b_aE > b_aS and maxR > 0.5:
                    pat = str(blur.get("pattern") or "pulse")
                    bspd = float(blur.get("speed", 0.6))
                    gfps = float(blur.get("gifFps", 0) or 0)
                    # geq uses uppercase T for time (lowercase t belongs to standard filter chains)
                    lt = "(T-%g)" % b_aS if gfps <= 0 else "(floor((T-%g)*%g)/%g)" % (b_aS, gfps, gfps)
                    dw = b_aE - b_aS
                    if pat == "uniform":  k_curve = "1"
                    elif pat == "in":     k_curve = "max(0,1-%s/%g)" % (lt, dw)
                    elif pat == "out":    k_curve = "min(1,%s/%g)" % (lt, dw)
                    else:                 k_curve = "(0.5+0.5*sin(2*PI*%g*%s))" % (bspd, lt)
                    kExpr = "if(between(T,%g,%g),%s,0)" % (b_aS, b_aE, k_curve)
                    blur_expr = (maxR, kExpr)
            # Emit filter graph. Distort applies displace (needs a noise-map input); blur applies
            # split+gblur+overlay. Both are graph stages, not chain filters. Chain: filt → distort? → blur? → [oi{k}]
            noise_ii = None
            noise_ii_b = None
            if distort_noise_png and distort_noise_png_b:
                inputs += ["-loop", "1", "-i", distort_noise_png]
                inputs += ["-loop", "1", "-i", distort_noise_png_b]
                noise_ii = ii + 1
                noise_ii_b = ii + 2
            # Stage 1: run the base filter chain, output an intermediate label
            stage_in = f"[{ii}:v]"
            stage_out = f"[oi_a{k}]"
            fc.append(stage_in + ",".join(filt) + stage_out)
            cur = f"oi_a{k}"
            # Stage 2 (optional): distort via displace filter — cross-blend two noise maps over time
            # so the warp FLOWS (matches the client SVG's feComposite k2/k3 oscillation). Without
            # this the render's warp is frozen for the whole clip while the preview animates.
            if noise_ii is not None:
                bsp, (bS, bE) = distort_blend_speed, distort_window
                # blend expression: A * (0.5 + 0.5*sin(2π*bsp*(t-bS))) + B * (0.5 - 0.5*sin(...))
                # Gated to the anim window; outside it, k=0.5 (half-blend, harmless static).
                w_arg = "if(between(T,%g,%g),T-%g,0)" % (bS, bE, bS)
                blend_expr = ("A*(0.5+0.5*sin(2*PI*%g*(%s)))+B*(0.5-0.5*sin(2*PI*%g*(%s)))"
                              % (bsp, w_arg, bsp, w_arg))
                # Blend two static noise maps → animated noise map, then scale2ref to overlay dims,
                # optional gblur to soften sharp transitions (smoothness slider), then displace.
                fc.append(f"[{noise_ii}:v][{noise_ii_b}:v]blend=all_expr='{blend_expr}',format=rgba[dnblend{k}]")
                fc.append(f"[dnblend{k}][{cur}]scale2ref[dnraw{k}][{cur}b]")
                if distort_smooth_sigma > 0.5:
                    fc.append(f"[dnraw{k}]gblur=sigma={distort_smooth_sigma:g},format=gbrp,extractplanes=r+g[dnx{k}][dny{k}]")
                else:
                    fc.append(f"[dnraw{k}]format=gbrp,extractplanes=r+g[dnx{k}][dny{k}]")
                fc.append(f"[{cur}b][dnx{k}][dny{k}]displace=edge=smear[oi_b{k}]")
                cur = f"oi_b{k}"
            # Stage 3 (optional): blur via split + gblur + alpha-modulated overlay
            if blur_expr is not None:
                maxR, kExpr = blur_expr
                fc.append(f"[{cur}]split=2[oa{k}][ob{k}]")
                fc.append(
                    f"[ob{k}]gblur=sigma={maxR:g},format=rgba,"
                    f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({kExpr})'[obm{k}]"
                )
                fc.append(f"[oa{k}][obm{k}]overlay=0:0[oi{k}]")
            else:
                fc.append(f"[{cur}]null[oi{k}]")
            fc.append(f"[{last}][oi{k}]overlay=x='W*{ox}-w/2+({odx})':y='H*{oy}-h/2+({ody})':{en}[v{k}]")
            last = f"v{k}"
            # Advance ii past the main overlay AND both noise inputs (if any).
            ii = (noise_ii_b + 1) if noise_ii_b is not None else (ii + 1)
        # sparkle: composite twinkling copies of an emoji (or image) around the overlay center.
        # Iterate ALL sparkle anims (not just the first) so stacked sparkles all render.
        # The emoji can be a single glyph OR a decorative composite string ("˗ˏˋ ✸ ˎˊ˗", "✶⋆.˚").
        # Either way it bakes as ONE PNG (user's visual intent: each scatter point IS the cluster).
        # ffmpeg scales by HEIGHT (scale=-2:sh) so width grows naturally — no width-driven blow-up
        # when one glyph in the string is a full-color emoji and the others are small modifier marks.
        sparkles = [a for a in (o.get("anims") or []) if a.get("type") == "sparkle" and ((a.get("emoji") or "").strip() or a.get("src"))]
        for ai, spk in enumerate(sparkles):
            if (spk.get("emoji") or "").strip():
                psp = os.path.join(tmp, "emoji_%d_%d.png" % (k, ai))
                try:
                    _emoji_png(spk["emoji"], psp)
                except Exception:
                    psp = None
            else:
                psp = os.path.join(PROJ, *spk["src"].split("/"))
                if not os.path.exists(psp):
                    psp = None
            if not psp: continue
            spread = float(spk.get("spread", 0.12)) * W; ssize = float(spk.get("size", 0.06)) * W; sspeed = float(spk.get("speed", 1.2))
            for i, f in enumerate(_sparkle_field(spk)):
                sh_px = max(2, int(ssize * f["sc"])); offx = f["cx"] * spread; offy = f["cy"] * spread
                twk = "pow(max(0,sin(2*PI*%g*(T-%g)+%g)),3)" % (sspeed, s, f["ph"])
                inputs += ["-loop", "1", "-t", str(e), "-i", psp]
                fc.append(f"[{ii}:v]scale=-2:{sh_px},format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({twk})'[sp{k}_{ai}_{i}]")
                fc.append(f"[{last}][sp{k}_{ai}_{i}]overlay=x='W*{ox}-w/2+({offx})':y='H*{oy}-h/2+({offy})':{en}[vs{k}_{ai}_{i}]")
                last = f"vs{k}_{ai}_{i}"; ii += 1
    if not fc:
        return silent, None
    out = os.path.join(tmp, "overlaid.mp4")
    # Write the filtergraph to a file and pass it via -filter_complex_script. With many overlays
    # (e.g. a per-piece sprinkle expanded to dozens of pieces) the inline -filter_complex string
    # plus all the -i inputs overflows Windows' ~32K command-line limit (WinError 206). A script
    # file keeps the (potentially huge) graph off the command line entirely.
    fc_path = os.path.join(tmp, "filtergraph.txt")
    with open(fc_path, "w", encoding="utf-8") as fh:
        fh.write(";\n".join(fc))
    cmd = ([FFMPEG, "-y", "-loglevel", "error"] + inputs + ["-filter_complex_script", fc_path, "-map", f"[{last}]"]
           + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
              "-r", "60", "-video_track_timescale", "90000", "-an", out])   # 60fps output → smoother scale-pulse / distort animations
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
        if s.get("hidden"):                # hidden ch0 clips don't occupy time — timeline compacts (consistent with overlay/audio hidden)
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
        if s.get("hidden"):                # hidden upper-channel clips don't enter the per-time ranking at all
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
    # Tolerance must exceed round(_, 4)'s upper-bound error (5e-5) — otherwise the rounded
    # vid_total (e.g. 14.7067 from a true 14.706666…) gets culled here, the last interval
    # is never created, and the final clip silently drops from the render.
    bs = sorted(x for x in bset if 0.0 <= x <= vid_total + 5e-4)
    flat = []
    for a, b in zip(bs, bs[1:]):
        if b - a < 0.02:
            continue
        m = (a + b) / 2.0
        top = None
        for c in clips:
            if c["s"].get("hidden"):          # hidden clips don't show (slot becomes black / lower channel shows)
                continue
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


def render(edl, out_dir=None, out_name=None, progress=None):
    def prog(stage, pct):
        if progress is not None:
            progress["stage"] = stage; progress["pct"] = int(pct)
    prog("Preparing…", 2)
    s = edl.get("settings", {})
    canvas = s.get("canvas", "9x16")
    W, H = CANVAS.get(canvas, CANVAS["9x16"])
    segs = edl.get("segments", [])
    # Overlay-only mode: no video clips, just animated overlays + audio. We build a black base of
    # the max overlay/audio end time, then run the same overlay/audio compositing pipeline on top.
    def _content_end(edl_):
        end = 0.0
        for o in (edl_.get("overlays") or []):
            if not isinstance(o, dict) or o.get("hidden"): continue
            end = max(end, float(o.get("start", 0) or 0) + float(o.get("dur", 0) or 0))
        for a in (edl_.get("audio") or []):
            if not isinstance(a, dict) or a.get("hidden"): continue
            end = max(end, float(a.get("start", 0) or 0) + float(a.get("dur", 0) or 0))
        return end
    overlay_only = not segs and _content_end(edl) > 0
    if not segs and not overlay_only:
        return {"ok": False, "log": "Nothing to render — add a clip, an overlay, or an audio track."}
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
        # Overlay-only shortcut: bypass the segment loop and lay down a single black clip whose
        # length covers every overlay + audio. Endcard (if enabled) still appends after.
        if overlay_only:
            base_end = _content_end(edl)
            blk = os.path.join(tmp, "blk_overlay_only.mp4")
            r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", f"color=black:s={W}x{H}:r=60:d={base_end:g}",
                     "-vf", "format=yuv420p"] + ENC + [blk])
            if r.returncode != 0:
                return {"ok": False, "log": f"overlay-only base failed:\n{r.stderr[-1500:]}"}
            lines.append("file '" + blk.replace("\\", "/") + "'")
            total = base_end
        flat, _vt = flatten_segments(edl)   # multi-channel → sequential (top channel covers lower)
        if not flat and not overlay_only:
            return {"ok": False, "log": "Nothing to render."}
        last_real = max((k for k, e in enumerate(flat) if not e.get("black")), default=-1)
        n_flat = len(flat)
        for idx, seg in enumerate(flat):
            prog(f"Rendering clip {idx+1} of {n_flat}…", 5 + int(60 * idx / max(1, n_flat)))
            if seg.get("black"):            # uncovered span (gap, or black under upper clips) → black filler
                d = max(0.05, float(seg.get("dur", 0.1)))
                gp = os.path.join(tmp, f"blk_{idx:02d}.mp4")
                r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                         "-i", f"color=c=black:s={W}x{H}:r=60:d={d}", "-vf", "format=yuv420p"] + ENC + [gp])
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
            base = f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={W}:{H}:{cx}:{cy},setsar=1,fps=60,format=yuv420p"
            dur = float(seg["dur"])                     # dur = SOURCE seconds consumed
            spd = min(10.0, max(0.1, float(seg.get("speed", 1) or 1)))
            outlen = dur / spd                          # timeline (output) seconds
            if abs(spd - 1.0) > 1e-3:
                base = f"setpts={1.0/spd:.6f}*PTS," + base   # slow (<1) / speed up (>1)
            base += f",tpad=stop_mode=clone:stop_duration={outlen:.3f}"   # freeze-fill the last frame so over-length clips hold (matches preview); -t clamps to outlen
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
        prog("Joining clips…", 70)
        # Re-encode the concat (not -c copy): stream-copying tpad freeze-fill segments yields a file
        # that plays fine but is filter-HOSTILE — the overlay/geq pass crawls (90s+ vs 3s). A clean
        # CFR re-encode here makes the overlay compositing fast again.
        r = run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listf,
                 "-vf", "fps=60,format=yuv420p"] + ENC + [silent])
        if r.returncode != 0:
            return {"ok": False, "log": f"concat failed:\n{r.stderr[-1500:]}"}
        # free-floating overlays (text/images) over the whole timeline
        n_ov = len([o for o in (edl.get("overlays") or []) if isinstance(o, dict)])
        prog((f"Compositing {n_ov} overlay(s) — this is the slow step…" if n_ov else "Finishing…"), 78)
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
        # === Audio mix: per-clip EDL.audio overlays + framed-clip source audio ===
        # Each audio source becomes an ffmpeg -i input; its filter chain trims+delays into the right
        # spot on the timeline and applies per-clip volume + fadeIn/fadeOut. All chains are amix'd
        # at the end. Tracks are 8-tuples: (path, start, dur, vol, fadeIn, fadeOut, src_in, speed).
        # src_in seeks into the source file (framed-clip audio); speed uses atempo (framed-clip too).
        tracks = []
        for a in (edl.get("audio") or []):
            if a.get("hidden"): continue
            src_rel = (a.get("src") or "").lstrip("/")
            if not src_rel: continue
            ap = os.path.join(PROJ, src_rel.replace("/", os.sep))
            if not os.path.exists(ap): continue
            st = max(0.0, float(a.get("start", 0) or 0))
            du = max(0.05, float(a.get("dur", 0) or 0))
            vol = max(0.0, min(2.0, float(a.get("volume", 1) if a.get("volume") is not None else 1)))
            if vol <= 0.001: continue
            fi = max(0.0, float(a.get("fadeIn", 0) or 0))
            fo = max(0.0, float(a.get("fadeOut", 0) or 0))
            if st >= total: continue
            if st + du > total: du = total - st
            src_in = max(0.0, float(a.get("srcIn", 0) or 0))   # skip N seconds into the source file
            tracks.append((ap, st, du, vol, fi, fo, src_in, 1.0))
        # Framed-clip (picture-in-picture) source audio — one track per clipframe with srcAudio=true.
        # Reuse id_to_file lookup to resolve the source file path.
        cf_i2f = None
        for o in (edl.get("overlays") or []):
            if not isinstance(o, dict) or o.get("hidden"): continue
            if o.get("type") != "clipframe" or not o.get("srcAudio"): continue
            if cf_i2f is None:
                cf_i2f = id_to_file()
            ap = cf_i2f.get(o.get("srcId"))
            if not ap or not os.path.exists(ap): continue
            st = max(0.0, float(o.get("start", 0) or 0))
            du = max(0.05, float(o.get("dur", 0) or 0))
            vol = max(0.0, min(2.0, float(o.get("srcAudioVol", 1) if o.get("srcAudioVol") is not None else 1)))
            if vol <= 0.001: continue
            src_in = max(0.0, float(o.get("srcIn", 0) or 0))
            spd = max(0.1, float(o.get("srcSpeed", 1) or 1))
            if st >= total: continue
            if st + du > total: du = total - st
            tracks.append((ap, st, du, vol, 0.0, 0.0, src_in, spd))
        prog("Adding audio + finalizing…", 92)
        if tracks:
            ff_in = [FFMPEG, "-y", "-loglevel", "error", "-i", vid_src]
            chains = []
            outs = []
            def _atempo_chain(spd):
                # atempo supports 0.5..100.0 per invocation on modern ffmpeg, but 0.5..2.0 is the
                # portable safe range. Chain factors so their product = spd.
                out = []
                x = spd
                while x > 2.0:  out.append("atempo=2.0"); x /= 2.0
                while x < 0.5:  out.append("atempo=0.5"); x /= 0.5
                if abs(x - 1.0) > 1e-3: out.append("atempo=%.4f" % x)
                return ",".join(out) if out else ""
            for i, (p, st, du, vol, fi, fo, src_in, spd) in enumerate(tracks, start=1):
                # Seek into source (framed-clip audio) uses -ss BEFORE -i for accurate keyframe seek.
                if src_in > 0.001:
                    ff_in += ["-ss", "%.3f" % src_in, "-i", p]
                else:
                    ff_in += ["-i", p]
                delay_ms = int(round(st * 1000))
                # Trim BEFORE atempo so we grab exactly (du*spd) seconds pre-speedup; atempo yields du.
                take = du * spd if spd > 0 else du
                ch = (f"[{i}:a]atrim=0:{take:.3f},asetpts=PTS-STARTPTS")
                tempo = _atempo_chain(spd)
                if tempo:
                    ch += "," + tempo
                if fi > 0: ch += f",afade=t=in:st=0:d={fi:.3f}"
                if fo > 0: ch += f",afade=t=out:st={max(0.0, du-fo):.3f}:d={fo:.3f}"
                if vol != 1.0: ch += f",volume={vol:.3f}"
                if delay_ms > 0: ch += f",adelay={delay_ms}|{delay_ms},apad=whole_dur={total:.3f}"
                else: ch += f",apad=whole_dur={total:.3f}"
                ch += f"[a{i}]"
                chains.append(ch)
                outs.append(f"[a{i}]")
            chains.append("".join(outs) + f"amix=inputs={len(tracks)}:duration=longest:normalize=0,"
                          f"atrim=0:{total:.3f},loudnorm=I=-16:TP=-1.5:LRA=11[a]")
            af = ";".join(chains)
            r = run(ff_in + ["-filter_complex", af, "-map", "0:v", "-map", "[a]",
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
         ".gif": "image/gif", ".webp": "image/webp", ".apng": "image/apng",
         ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2"}


# ---------- in-app self-update ----------
def app_version():
    """The running app's version = the appVer badge in index.html (single source of truth)."""
    try:
        html = open(os.path.join(EDITOR, "index.html"), encoding="utf-8").read()
        m = re.search(r'id="appVer"[^>]*>v?([0-9][0-9.]*)<', html)
        return m.group(1) if m else "0"
    except Exception:
        return "0"


def check_update():
    """Compare the local version to version.json hosted at UPDATE_URL."""
    cur = app_version()
    try:
        with urllib.request.urlopen(UPDATE_URL.rstrip("/") + "/version.json", timeout=15) as r:
            remote = json.loads(r.read().decode("utf-8"))
        latest = str(remote.get("version", "")).strip()
        zip_name = remote.get("zip", "")
        def _vt(v):
            try: return tuple(int(x) for x in str(v).split("."))
            except Exception: return (0,)
        return {"current": cur, "latest": latest, "zip": zip_name,
                "updateAvailable": bool(latest) and _vt(latest) > _vt(cur)}
    except Exception as e:
        return {"current": cur, "latest": None, "updateAvailable": False, "error": str(e)}


def do_self_update():
    """Download the latest zip and extract the app code + music over this install,
    leaving user data (clips/projects/exports/assets) untouched. Returns the new version."""
    info = check_update()
    if not info.get("updateAvailable"):
        return {"ok": False, "log": "Already up to date.", **info}
    zip_name = info.get("zip") or ""
    zip_url = zip_name if zip_name.startswith("http") else (UPDATE_URL.rstrip("/") + "/" + zip_name)
    with urllib.request.urlopen(zip_url, timeout=120) as r:
        data = r.read()
    safe = ("editor/", "sources/music/")   # only ever overwrite app code + music
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            norm = name.replace("\\", "/")
            if not norm.startswith(safe):
                continue
            dest = os.path.join(PROJ, *norm.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    return {"ok": True, "version": info.get("latest")}


def _schedule_restart():
    """Exit with RESTART_EXIT_CODE after the HTTP response flushes; the launcher loop re-runs us."""
    def _go():
        time.sleep(0.6)
        os._exit(RESTART_EXIT_CODE)
    threading.Thread(target=_go, daemon=True).start()


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
        if path == "/api/render-progress":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            jid = (qs.get("job") or [""])[0]
            job = _render_jobs.get(jid)
            if not job:
                return self._json({"error": "no such job"}, 404)
            out = {"stage": job.get("stage"), "pct": job.get("pct", 0), "done": job.get("done", False)}
            if job.get("done"):
                out["result"] = job.get("result")
            return self._json(out)
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
            return self._json({"clips": clips, "music": list_music(), "sfx": list_sfx(),
                               "canvases": list(CANVAS.keys()), "logo": logo, "assets": list_assets(),
                               "exportsDir": EXPORTS})
        if path == "/api/edl":
            return self._json(load_edl())
        if path == "/api/version":
            try:
                on_disk = int(os.path.getmtime(os.path.join(EDITOR, "server.py")))
            except Exception:
                on_disk = 0
            return self._json({"version": app_version(),
                               "serverMtimeBoot": _SERVER_PY_BOOT_MTIME,
                               "serverMtimeDisk": on_disk,
                               "stale": bool(on_disk and _SERVER_PY_BOOT_MTIME
                                             and on_disk > _SERVER_PY_BOOT_MTIME)})
        if path == "/api/check-update":
            return self._json(check_update())
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
        # Streaming raw-bytes upload: avoids the ~4× memory blow-up of base64+JSON for large video files.
        if path == "/api/upload-clip-raw":
            try:
                raw = self.rfile.read(ln) if ln else b""
                if not raw:
                    return self._json({"ok": False, "log": "empty file"}, 400)
                name = urllib.parse.unquote(self.headers.get("X-Filename", "clip.mp4"))
                cat = self.headers.get("X-Category", "uploads")
                rel, cat, cid = ingest_upload(raw, name, cat)
                return self._json({"ok": True, "category": cat, "file": rel, "id": cid})
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
        body = self.rfile.read(ln) if ln else b"{}"
        try:
            data = json.loads(body or b"{}")
        except Exception:
            return self._json({"ok": False, "log": "bad json"}, 400)
        if path == "/api/edl":
            save_edl(data)
            return self._json({"ok": True})
        if path == "/api/self-update":
            try:
                return self._json(do_self_update())
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
        if path == "/api/restart":
            self._json({"ok": True})
            _schedule_restart()
            return
        if path == "/api/clip/recat":
            cid = (data.get("id") or "").strip()
            cat = re.sub(r"[^A-Za-z0-9_-]", "", (data.get("category") or "").lower())
            if not cid or not cat:
                return self._json({"ok": False, "log": "id and category required"}, 400)
            src = id_to_file().get(cid)
            if not src or not os.path.exists(src):
                return self._json({"ok": False, "log": "clip not found"}, 404)
            # Move into <SRC_ROOT>/<brand>/<cat>/ so the folder tree reflects brand AND category.
            bm = load_brand_map()
            cur_product = os.path.basename(os.path.dirname(src))
            brand = _brand_from_path(src, cur_product, bm)
            dest_dir = os.path.join(SRC_ROOT, brand, cat)
            if os.path.abspath(os.path.dirname(src)) == os.path.abspath(dest_dir):
                return self._json({"ok": True, "category": cat, "brand": brand})
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            if os.path.exists(dest):
                return self._json({"ok": False, "log": f"a clip with that name already exists in '{brand}/{cat}'"}, 409)
            try:
                shutil.move(src, dest)
            except Exception as e:
                return self._json({"ok": False, "log": repr(e)}, 500)
            _probe_cache.pop(src, None)
            _probe_disk.pop(src, None)
            _cleanup_empty_dirs(os.path.dirname(src))
            # Remember this product->brand mapping so flat-layout siblings still resolve
            bm[cat] = brand; save_brand_map(bm)
            return self._json({"ok": True, "category": cat, "brand": brand})
        if path == "/api/clip/rename-category":   # rename a product folder; same brand parent kept
            oldcat = (data.get("old") or "").strip()
            newcat = re.sub(r"[^A-Za-z0-9_-]", "", (data.get("new") or "").lower())
            if not oldcat or not newcat:
                return self._json({"ok": False, "log": "old + valid new category required"}, 400)
            if oldcat == newcat:
                return self._json({"ok": True, "old": oldcat, "new": newcat, "renamed": False})
            src_dir = _find_product_dir(oldcat)
            if not src_dir:
                return self._json({"ok": False, "log": f"category folder not found: {oldcat}"}, 404)
            dest_dir = os.path.join(os.path.dirname(src_dir), newcat)   # same brand parent
            if os.path.exists(dest_dir):
                return self._json({"ok": False, "log": f"target folder already exists: {dest_dir}"}, 409)
            try:
                shutil.move(src_dir, dest_dir)
            except Exception as e:
                return self._json({"ok": False, "log": f"rename failed: {e!r}"}, 500)
            # Move probe-cache entries forward so we don't re-probe every clip in the folder
            for k in list(_probe_cache.keys()):
                if k.startswith(src_dir + os.sep): _probe_cache.pop(k, None)
            for k in list(_probe_disk.keys()):
                if k.startswith(src_dir + os.sep):
                    v = _probe_disk.pop(k)
                    _probe_disk[k.replace(src_dir, dest_dir, 1)] = v
            global _probe_dirty; _probe_dirty = True; _probe_cache_save()
            # Migrate brand_map key
            bm = load_brand_map()
            if oldcat in bm:
                bm[newcat] = bm.pop(oldcat); save_brand_map(bm)
            return self._json({"ok": True, "old": oldcat, "new": newcat, "renamed": True})
        if path == "/api/tag/rename":         # rename a tag globally; empty new = delete the tag everywhere
            oldtag = (data.get("old") or "").strip()
            newtag = (data.get("new") or "").strip()
            if not oldtag:
                return self._json({"ok": False, "log": "old required"}, 400)
            tm = dict(load_clip_tags())
            changed = 0
            for cid in list(tm.keys()):
                tags = tm[cid]
                if oldtag in tags:
                    rest = [t for t in tags if t != oldtag]
                    if newtag and newtag not in rest: rest.append(newtag)
                    if rest: tm[cid] = sorted(set(rest))
                    else:    tm.pop(cid, None)
                    changed += 1
            save_clip_tags(tm)
            return self._json({"ok": True, "old": oldtag, "new": newtag, "clips_updated": changed})
        if path == "/api/clip/tags":      # set the tag list for ONE clip (replaces existing tags)
            cid = (data.get("id") or "").strip()
            tags = data.get("tags") or []
            if not cid:
                return self._json({"ok": False, "log": "id required"}, 400)
            tm = dict(load_clip_tags())
            tm[cid] = [str(t).strip() for t in tags if str(t).strip()]
            if not tm[cid]:
                tm.pop(cid, None)
            save_clip_tags(tm)
            return self._json({"ok": True, "id": cid, "tags": load_clip_tags().get(cid, [])})
        if path == "/api/clip/setbrand":   # set the brand of a PRODUCT folder + PHYSICALLY MOVE / MERGE any duplicates under that brand
            product = (data.get("product") or "").strip()
            brand = (data.get("brand") or "").strip().lower()
            if not product or brand not in BRAND_KEYS:
                return self._json({"ok": False, "log": "product + valid brand required"}, 400)
            bm = load_brand_map(); bm[product] = brand; save_brand_map(bm)
            all_dirs = _find_all_product_dirs(product)
            if not all_dirs:
                return self._json({"ok": True, "product": product, "brand": brand, "moved": False})
            dest_dir = os.path.join(SRC_ROOT, brand, product)
            os.makedirs(dest_dir, exist_ok=True)
            moved = 0; merged = 0; conflicts = []
            for src_dir in all_dirs:
                if os.path.abspath(src_dir) == os.path.abspath(dest_dir):
                    continue   # already at destination — nothing to move for this location
                # Walk src_dir and move each file into dest_dir; skip on name collision instead of losing data
                for fn in list(os.listdir(src_dir)):
                    src_f = os.path.join(src_dir, fn); dst_f = os.path.join(dest_dir, fn)
                    if os.path.exists(dst_f):
                        # If it's the exact same file (same size), the src copy is safe to delete; otherwise flag it
                        try:
                            if os.path.isfile(src_f) and os.path.isfile(dst_f) and os.path.getsize(src_f) == os.path.getsize(dst_f):
                                os.remove(src_f); merged += 1; continue
                        except OSError: pass
                        conflicts.append(fn); continue
                    try:
                        shutil.move(src_f, dst_f); moved += 1
                    except Exception as e:
                        conflicts.append(f"{fn}: {e!r}")
                # Cache invalidation for the drained location
                for k in list(_probe_cache.keys()):
                    if k.startswith(src_dir + os.sep): _probe_cache.pop(k, None)
                for k in list(_probe_disk.keys()):
                    if k.startswith(src_dir + os.sep): _probe_disk.pop(k, None)
                _cleanup_empty_dirs(src_dir)
            did_anything = moved > 0 or merged > 0
            return self._json({"ok": True, "product": product, "brand": brand,
                               "moved": did_anything, "merged_dupes": merged, "moved_files": moved,
                               "conflicts": conflicts, "consolidated_from": len(all_dirs)})
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
        if path == "/api/project/rename":
            # Atomic rename: update edl.name, write to new slugged filename, remove old file.
            # Refuses if the target filename collides with a different existing project.
            full_old = safe_project(data.get("file", ""))
            new_name = (data.get("name") or "").strip()
            if not full_old or not os.path.exists(full_old):
                return self._json({"ok": False, "log": "source project not found"}, 404)
            if not new_name:
                return self._json({"ok": False, "log": "new name required"}, 400)
            try:
                with open(full_old, encoding="utf-8") as fh:
                    edl = json.load(fh)
            except Exception as e:
                return self._json({"ok": False, "log": f"could not read source: {e!r}"}, 500)
            new_fname = slug(new_name) + ".json"
            full_new = os.path.join(PROJECTS, new_fname)
            if os.path.abspath(full_new) != os.path.abspath(full_old) and os.path.exists(full_new):
                return self._json({"ok": False, "log": f"'{new_name}' already exists"}, 409)
            edl["name"] = new_name
            with open(full_new, "w", encoding="utf-8") as fh:
                json.dump(edl, fh, indent=2)
            if os.path.abspath(full_new) != os.path.abspath(full_old):
                try: os.remove(full_old)
                except OSError: pass
            return self._json({"ok": True, "file": new_fname, "name": new_name})
        if path == "/api/render":
            out_dir = data.pop("_outDir", None) or None
            out_name = data.pop("_outName", None) or None
            save_edl(data)
            with _render_lock:
                _render_seq[0] += 1; jid = str(_render_seq[0])
                job = {"stage": "Starting…", "pct": 0, "done": False}
                _render_jobs[jid] = job
                for old in [k for k in _render_jobs if k != jid and _render_jobs[k].get("done")]:
                    _render_jobs.pop(old, None)   # keep the table small

            def _work(d=data, od=out_dir, on=out_name, j=job):
                try:
                    j["result"] = render(d, od, on, progress=j)
                except Exception as e:
                    j["result"] = {"ok": False, "log": repr(e)}
                finally:
                    j["pct"] = 100; j["done"] = True
            threading.Thread(target=_work, daemon=True).start()
            return self._json({"ok": True, "job": jid})
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


def _autoreload_watch():
    """Watch server.py mtime; when it changes, re-exec the current process so the running
    server picks up code edits without a manual restart. Fixes the recurring 'stale server'
    trap where a code change wouldn't apply until the user closed and reopened the terminal."""
    import sys, time
    path = os.path.join(EDITOR, "server.py")
    try:
        orig = os.path.getmtime(path)
    except Exception:
        return
    while True:
        time.sleep(2)
        try:
            cur = os.path.getmtime(path)
            if cur != orig:
                print("[autoreload] server.py changed — restarting…", flush=True)
                # os.execv replaces the current process with a fresh Python + fresh server.py.
                # Works cross-platform (Windows execv keeps stdin/stdout attached to the terminal).
                os.execv(sys.executable, [sys.executable, path])
        except Exception:
            pass


def main():
    os.makedirs(EXPORTS, exist_ok=True)
    if not os.path.exists(EDL_PATH):
        save_edl(default_edl())
    seed_projects()
    import threading
    threading.Thread(target=_autoreload_watch, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("SmarterClip editor running at http://127.0.0.1:8765/")
    srv.serve_forever()


if __name__ == "__main__":
    main()

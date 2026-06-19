#!/usr/bin/env python3
"""Package a SmarterClip web release.

Run this whenever you ship an update. It reads the app version from index.html,
builds the distributable zip (app code + music only — NOT user clips), writes
version.json, and drops a ready-to-publish folder in webdist/release/.

Then push the CONTENTS of webdist/release/ to your GitHub Pages site.
Users' launchers read version.json there and auto-update.

ONE-TIME: set BASE_URL below to your GitHub Pages URL.
"""
import os, re, json, shutil, zipfile

# GitHub Pages URL (no trailing slash)
BASE_URL = "https://smarterpaw-llc.github.io/smarterpaw-clip-editor"

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
EDITOR = os.path.join(PROJ, "editor")
OUT = os.path.join(PROJ, "docs")   # GitHub Pages serves from main /docs; this folder is fully managed here

def app_version():
    html = open(os.path.join(EDITOR, "index.html"), encoding="utf-8").read()
    m = re.search(r'id="appVer"[^>]*>v?([0-9.]+)<', html)
    if not m:
        raise SystemExit("Could not find appVer in index.html")
    return m.group(1)

def build():
    ver = app_version()
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    zip_name = f"SmarterClip-Editor-{ver}.zip"

    # 1) the app zip — editor code + fonts + music ONLY (no user clips/projects/exports)
    zpath = os.path.join(OUT, zip_name)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("index.html", "server.py"):
            z.write(os.path.join(EDITOR, f), f"editor/{f}")
        for root, _d, files in os.walk(os.path.join(EDITOR, "fonts")):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, PROJ).replace("\\", "/"))
        music = os.path.join(PROJ, "sources", "music")
        if os.path.isdir(music):
            for f in os.listdir(music):
                z.write(os.path.join(music, f), f"sources/music/{f}")

    # 2) version manifest the launcher polls
    json.dump({"version": ver, "zip": zip_name},
              open(os.path.join(OUT, "version.json"), "w", encoding="utf-8"), indent=2)

    # 3) the self-updating launcher (copied as-is)
    shutil.copy2(os.path.join(HERE, "launcher.ps1"), os.path.join(OUT, "launcher.ps1"))

    # 4) the tiny bootstrap users keep + double-click (BASE baked in)
    bat = (
        "@echo off\r\n"
        "title SmarterClip Editor\r\n"
        f'powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:SCBASE=\'{BASE_URL}\'; '
        'iex (irm $env:SCBASE/launcher.ps1)"\r\n'
        "pause\r\n"
    )
    open(os.path.join(OUT, "SmarterClip.bat"), "w", newline="").write(bat)

    # 5) landing page (the public link) with a download button
    page = LANDING.replace("__BASE__", BASE_URL).replace("__VER__", ver)
    open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(page)

    print(f"Built release {ver} -> {OUT}")
    print("Contents:", ", ".join(sorted(os.listdir(OUT))))
    if "USERNAME" in BASE_URL:
        print("\n!! Set BASE_URL at the top of make_release.py to your real GitHub Pages URL, then re-run.")

LANDING = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SmarterClip Editor</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial;background:#15171c;color:#eee;margin:0;
   display:flex;min-height:100vh;align-items:center;justify-content:center;text-align:center}
 .card{max-width:520px;padding:32px}
 h1{color:#8ab81d;margin:0 0 6px} .ver{color:#888;font-size:13px}
 a.btn{display:inline-block;margin:22px 0 10px;background:#f0a830;color:#111;font-weight:700;
   text-decoration:none;padding:14px 26px;border-radius:10px;font-size:17px}
 ol{text-align:left;color:#cfd3da;line-height:1.7;margin-top:18px}
 code{background:#0006;padding:1px 6px;border-radius:5px}
</style></head><body><div class="card">
 <h1>SmarterClip Editor</h1>
 <div class="ver">latest version __VER__</div>
 <a class="btn" href="SmarterClip.bat" download>⬇ Download launcher</a>
 <ol>
  <li>Click <b>Download launcher</b> (saves <code>SmarterClip.bat</code>).</li>
  <li>Double-click it. First run installs what's needed and downloads the app.</li>
  <li>It opens the editor in your browser. Keep the black window open while editing.</li>
  <li>Every time you run it, it auto-updates to the newest version.</li>
 </ol>
 <p class="ver">Runs locally on your PC — your clips never leave your machine.</p>
</div></body></html>"""

if __name__ == "__main__":
    build()

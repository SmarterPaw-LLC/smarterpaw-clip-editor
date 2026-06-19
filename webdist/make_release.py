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
    os.makedirs(OUT, exist_ok=True)
    zip_name = f"SmarterClip-Editor-{ver}.zip"
    zpath = os.path.join(OUT, zip_name)

    # drop stale zips from older versions so docs/ only ever holds the current one
    for f in os.listdir(OUT):
        if f.startswith("SmarterClip-Editor-") and f.endswith(".zip") and f != zip_name:
            os.remove(os.path.join(OUT, f))

    # 1) the app zip — editor code + fonts + music ONLY (no user clips/projects/exports).
    #    Reuse it if this version's zip already exists (avoids re-committing ~50MB on a no-app change).
    if not os.path.exists(zpath):
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

    # 3) the self-updating launcher, with the Pages URL baked in (so the one-line
    #    PowerShell installer works without needing the .bat to set $env:SCBASE)
    lp = open(os.path.join(HERE, "launcher.ps1"), encoding="utf-8").read().replace("__BASE__", BASE_URL)
    open(os.path.join(OUT, "launcher.ps1"), "w", encoding="utf-8").write(lp)

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
 .card{max-width:600px;padding:32px}
 h1{color:#8ab81d;margin:0 0 6px} .ver{color:#888;font-size:13px}
 h2{font-size:15px;color:#f0a830;margin:26px 0 8px}
 .cmd{display:flex;gap:8px;align-items:stretch;margin:10px 0}
 .cmd code{flex:1;background:#0008;border:1px solid #333;border-radius:8px;padding:12px 14px;
   text-align:left;font-size:12.5px;color:#cfe6a0;overflow:auto;white-space:nowrap}
 button.copy{background:#f0a830;color:#111;font-weight:700;border:0;border-radius:8px;padding:0 16px;cursor:pointer}
 ol{text-align:left;color:#cfd3da;line-height:1.7;margin-top:10px}
 a.alt{color:#9ab;font-size:13px} code.k{background:#0006;padding:1px 6px;border-radius:5px}
 .hr{border-top:1px solid #2a2d33;margin:26px 0}
</style></head><body><div class="card">
 <h1>SmarterClip Editor</h1>
 <div class="ver">latest version __VER__ · runs locally on your PC — your clips never leave your machine</div>

 <h2>Install &amp; run (recommended)</h2>
 <ol>
  <li>Press <b>Windows key</b>, type <b>PowerShell</b>, press <b>Enter</b>.</li>
  <li>Paste this and press <b>Enter</b>:</li>
 </ol>
 <div class="cmd">
  <code id="cmd">iex (irm __BASE__/launcher.ps1)</code>
  <button class="copy" onclick="navigator.clipboard.writeText(document.getElementById('cmd').textContent);this.textContent='Copied'">Copy</button>
 </div>
 <div class="ver">First run installs Python/ffmpeg if needed and downloads the app, then opens the editor.
  Each run auto-updates to the newest version. Keep the PowerShell window open while editing.</div>

 <div class="hr"></div>
 <h2>Prefer a double-click file?</h2>
 <div class="ver">Download <a class="alt" href="SmarterClip.bat" download>SmarterClip.bat</a> and run it.
  Windows may warn (it's an unrecognized file): on the download click <code class="k">Keep</code>,
  and if you see "Windows protected your PC" click <code class="k">More info → Run anyway</code>.</div>
</div></body></html>"""

if __name__ == "__main__":
    build()

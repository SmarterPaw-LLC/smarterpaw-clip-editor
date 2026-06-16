# Meowi Clip Editor

A lightweight, browser-based editor for cutting UGC video clips into short, story-driven
ads across multiple channel placements (9:16 Reels/Stories, 4:5 Meta feed, 16:9 YouTube).
Built for the Meowijuana brand workflow, but the tooling is generic.

No build step, no cloud, no heavy NLE — a tiny stdlib Python server drives `ffmpeg`, and a
single-file HTML UI lets you trim clips, sequence a timeline, caption it, preview, and
render real MP4s.

## Features

- **Source bin** — every clip with thumbnails, grouped by product
- **Trimmer** — scrub a clip and set IN / duration; per-clip zoom + crop anchor
  (handy for cropping out baked-in watermarks)
- **Timeline** — reorder / duplicate / delete segments, edit captions inline
- **Live preview** — play the whole timeline with captions, music, and an end card
- **Settings** — canvas (9:16 / 4:5 / 16:9), music track, end-card text
- **Render** — exports `Meowi_rollies_hero_<canvas>.mp4` via ffmpeg, no terminal

## Requirements

- [ffmpeg](https://ffmpeg.org/) (with `ffprobe`) — `winget install Gyan.FFmpeg`
- Python 3.10+
- A Chromium-based browser (for VP9/AV1 source preview)

The ffmpeg path is configured near the top of `editor/server.py`; it falls back to PATH.

## Run

Double-click **`Start Meowi Editor.bat`** (Windows), or:

```sh
python editor/server.py
# then open http://127.0.0.1:8765/
```

## How it works

- `editor/server.py` — static file server (with HTTP Range for video seeking) + JSON APIs:
  `/api/manifest`, `/api/edl` (GET/POST), `/api/render`.
- `editor/index.html` — the entire UI (vanilla JS).
- `edl.json` — the timeline (segments + settings). Each segment references a clip by its
  YouTube id, with `in`, `dur`, `cap`, `zoom`, `anchor`.
- `_build_rollies_hero.ps1` — standalone CLI renderer with the same EDL logic.

Render pipeline per segment: `scale` (cover) → directional `crop` → `drawtext` caption →
concat → music bed (loudnorm to -16 LUFS, fades) → `+faststart` H.264/AAC.

## Note on assets

Source clips (creators' UGC) and music (Mixkit-licensed) are **not** included in this repo
— see `.gitignore`. Supply your own under `sources/`.

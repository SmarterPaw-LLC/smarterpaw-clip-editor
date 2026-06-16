# CLAUDE.md — Meowi UGC Story Edit (multi-placement)

Project context for Claude sessions. This folder builds **story-driven cuts from UGC
footage** for Meowi, exported into multiple channel placements. Different project from
the KKZ "Zoom Pops" commercial in `..\smarterpaw-video\` — that folder's CLAUDE.md is a
**reference for the ffmpeg/EDL workflow only**, not this project's content.

## What this project is

Take UGC clips (pulled from YouTube links and/or dropped into `sources/`), assemble a
**single story EDL**, then export that one story into several placement canvases. Cut
once, reframe/retime per channel.

## Placements (target canvases)

| Placement | Canvas | Aspect | Typical len | Notes |
|---|---|---|---|---|
| TikTok / Reels / Shorts | 1080×1920 | 9:16 | 9–30s | sound-on, hook in first 1–2s |
| Stories ad | 1080×1920 | 9:16 | ≤15s | same canvas as above; swipe-up/CTA end |
| Meta Feed (IG/FB) | 1080×1350 | 4:5 | 15–30s | works sound-off — burn captions |
| YouTube / CTV | 1920×1080 | 16:9 | 15s/30s | horizontal; landscape-safe framing |

Vertical (9:16) is the priority master for UGC. The 4:5 and 16:9 are reframes of the
same story. When source is vertical UGC, the 16:9 cut needs deliberate reframing
(blurred-fill background or punch-in) — don't just letterbox.

## Folder layout

- `sources/youtube/` — yt-dlp downloads (keep original filenames + a `urls.txt` log).
- `sources/dropped/` — clips Jason places here manually.
- `_analysis/` — ffprobe dumps, contact sheets, scene-cut detection, caption transcripts.
- `exports/` — final per-placement masters. Name `Meowi_<story>_<placement>_<len>.mp4`
  e.g. `Meowi_unboxing_9x16_15s.mp4`.
- `old/` — superseded renders. Never auto-delete; only move here.

## Toolchain (Windows 11, this PC)

- **ffmpeg/ffprobe** — installed via winget (`Gyan.FFmpeg`). If not on PATH in a fresh
  shell, find via `(Get-Command ffmpeg).Source` or under
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*`.
- **yt-dlp** — installed via winget (`yt-dlp.yt-dlp`). Pull best mp4:
  `yt-dlp -f "bv*+ba/b" -o "sources/youtube/%(title)s.%(ext)s" <URL>`.
- Run terminal steps for Jason — he prefers GUI/chat over the terminal.
- Long encodes: run in background (`run_in_background`) and poll, don't block.

## Workflow

1. **Gather** — download YouTube links into `sources/youtube/`, log URLs; ingest dropped
   clips. ffprobe every source (res, fps, duration, has-audio) into `_analysis/`.
2. **Story EDL** — pick segments + order that tell the story. One continuous shot per
   segment; note in/out trims in seconds. Build a vertical master first.
3. **Reframe/retime** — derive 4:5 and 16:9 from the locked EDL. Reframe vertical→16:9
   intentionally (punch-in or blurred fill).
4. **Captions** — burn open captions for sound-off placements (Meta feed especially).
5. **Export** — H.264/AAC, yuv420p, +faststart. Per-placement files into `exports/`.

## Rights / content notes

- UGC reuse assumes Meowi holds rights to the creator footage (own channel or licensed).
  Jason owns that call — flag once, don't re-litigate.
- SynthID/visible AI watermarks: never edit them out (see KKZ reference). Not expected
  in real UGC, but applies if any clip is AI-generated.

## Editor (browser-based, no terminal)

- Double-click **`Start Meowi Editor.bat`** → opens `http://127.0.0.1:8765/`.
- `editor/server.py` (stdlib Python, calls ffmpeg) + `editor/index.html` (vanilla JS UI).
  Source bin · trimmer · timeline (reorder/dup/delete/caption) · live preview · Render.
- Timeline state lives in `edl.json`; Render writes `exports/Meowi_rollies_hero_<canvas>.mp4`.
  `_build_rollies_hero.ps1` is the standalone CLI equivalent (same EDL logic).

## Open items

- Awaiting YouTube links / dropped source clips.
- Confirm per-placement target lengths once footage + story are set.

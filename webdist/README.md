# Web distribution — self-updating launcher

This lets other people run SmarterClip Editor from a **link**, while the app still
runs **locally** on their machine (so ffmpeg rendering stays fast and free).

How it works:
- A small page is hosted at a public URL (GitHub Pages).
- Users download one tiny file (`SmarterClip.bat`) and keep it.
- Every time they run it, it checks the hosted `version.json`, downloads the app
  **only if** their copy is out of date, then runs the editor locally.
- Updates never touch a user's clips/projects/renders (the zip contains app code +
  music only; user data lives alongside and is left alone).

> A clicked browser link can't run a local program (browser security). That's why
> users keep the tiny launcher `.bat` — it's what does "check → update → run."

---

Configured for: `https://smarterpaw-llc.github.io/smarterpaw-clip-editor`
(repo `SmarterPaw-LLC/smarterpaw-clip-editor`, Pages = branch `main` / folder `/docs`).

## One-time setup

1. Repo **Settings → Pages → Build and deployment**: Source = *Deploy from a branch*,
   Branch = `main`, folder = **`/docs`**, then **Save**.
   (If you ever move the repo, update `BASE_URL` in `make_release.py`.)

## Each time you ship an update

2. `python webdist/make_release.py`
   - Reads the version from `editor/index.html` (the `appVer` badge).
   - Writes the whole Pages site into **`docs/`**: the app zip, `version.json`,
     `launcher.ps1`, `SmarterClip.bat`, and `index.html` (landing page).
   - `docs/` is fully managed by this script (it's rebuilt each run).

3. Commit `docs/` and push (GitHub Desktop). Pages redeploys in ~1 min.

4. Share the link: `https://smarterpaw-llc.github.io/smarterpaw-clip-editor/`
   The page has a **Download launcher** button — that's the link you give people.

Users who already have it auto-update on next launch — you don't resend anything.

---

## Avoiding git bloat (optional but recommended)

The app zip is ~50 MB (it includes the music library). Committing a new 50 MB zip
to git every release bloats history. Two clean options:

- **GitHub Releases:** upload the zip as a Release asset instead of committing it,
  then set `version.json`'s `"zip"` to the asset's full `https://...` URL. The
  launcher already accepts absolute zip URLs. Only the tiny files go to Pages.
- Or keep Pages simple and just let the zip live in `/docs` (fine for a few users;
  history grows by ~50 MB per release).

`webdist/release/` is a build output — it's gitignored, don't commit it.

---

## Files here

- `make_release.py` — builds a release into `webdist/release/`. Set `BASE_URL` first.
- `launcher.ps1` — the self-updating launcher (hosted; fetched by the .bat each run).
- `README.md` — this file.

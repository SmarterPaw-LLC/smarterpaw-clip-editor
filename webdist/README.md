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

## One-time setup (you do this once)

1. Create a GitHub repo, e.g. `smarterclip`, and turn on **GitHub Pages**
   (Settings → Pages → deploy from a branch, e.g. `main` / `/docs`).
   Your URL will be like `https://YOURNAME.github.io/smarterclip`.

2. Open `webdist/make_release.py` and set **`BASE_URL`** to that exact URL
   (no trailing slash).

## Each time you ship an update

3. `python webdist/make_release.py`
   - Reads the version from `editor/index.html` (the `appVer` badge).
   - Builds `webdist/release/` with: the app zip, `version.json`, `launcher.ps1`,
     `SmarterClip.bat`, and `index.html` (the landing page).

4. Publish the **contents of `webdist/release/`** to your Pages location
   (e.g. copy them into the repo's `/docs` folder and push).

5. Share the link: `https://YOURNAME.github.io/smarterclip/`
   The page has a **Download launcher** button. That's the link you give people.

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

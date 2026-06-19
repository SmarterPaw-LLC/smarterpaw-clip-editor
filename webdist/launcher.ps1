# SmarterClip Editor — self-updating launcher.
# Fetched and run by SmarterClip.bat:  $env:SCBASE='<pages-url>'; irm $SCBASE/launcher.ps1 | iex
# It checks the hosted version, downloads the app only if the local copy is out of date,
# makes sure Python + ffmpeg + Pillow are present, then runs the editor locally.

$ErrorActionPreference = 'Stop'
$BASE = $env:SCBASE
if (-not $BASE) { $BASE = '__BASE__' }   # baked default so the one-line PowerShell installer works without the .bat
$BASE = $BASE.TrimEnd('/')

$Install = Join-Path $env:LOCALAPPDATA 'SmarterClip'
$AppDir  = Join-Path $Install 'app'
$VerFile = Join-Path $Install 'version.txt'
New-Item -ItemType Directory -Force $Install | Out-Null

Write-Host '== SmarterClip Editor ==' -ForegroundColor Cyan

# 1) Compare local vs hosted version
try { $remote = Invoke-RestMethod "$BASE/version.json" -TimeoutSec 25 } catch { $remote = $null; Write-Host 'Could not reach update server (offline?). Will run the local copy if present.' -ForegroundColor Yellow }
$local = if (Test-Path $VerFile) { (Get-Content $VerFile -Raw).Trim() } else { '' }

if ($remote -and $remote.version -and ($remote.version -ne $local)) {
  Write-Host ("Updating {0} -> {1} ..." -f ($(if($local){$local}else{'(none)'})), $remote.version) -ForegroundColor Yellow
  $zipUrl = if ($remote.zip -match '^https?://') { $remote.zip } else { "$BASE/$($remote.zip)" }
  $tmp = Join-Path $env:TEMP ("smarterclip_{0}.zip" -f $remote.version)
  Invoke-WebRequest $zipUrl -OutFile $tmp
  New-Item -ItemType Directory -Force $AppDir | Out-Null
  Expand-Archive -Path $tmp -DestinationPath $AppDir -Force   # overwrites app code + music; leaves user data dirs alone
  Remove-Item $tmp -Force
  Set-Content $VerFile $remote.version
  Write-Host ("Updated to {0}." -f $remote.version) -ForegroundColor Green
} elseif (-not (Test-Path (Join-Path $AppDir 'editor\server.py'))) {
  Write-Host 'No local copy installed and the update server is unreachable.' -ForegroundColor Red; Read-Host 'Press Enter to exit'; exit 1
} else {
  Write-Host ("Up to date (v{0})." -f $local) -ForegroundColor Green
}

# 2) Make sure the user-data folders exist (they live alongside the app and are never in the zip,
#    so updates never overwrite a user's clips/projects/renders).
foreach ($d in 'sources\youtube','sources\music','exports','projects','assets','ai-builds') {
  New-Item -ItemType Directory -Force (Join-Path $AppDir $d) | Out-Null
}

# 3) Dependencies (per-user, no admin)
function Missing($cmd){ -not (Get-Command $cmd -ErrorAction SilentlyContinue) }
if (Missing python) { Write-Host 'Installing Python...' -ForegroundColor Yellow; winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements }
if (Missing ffmpeg) { Write-Host 'Installing ffmpeg...'  -ForegroundColor Yellow; winget install -e --id Gyan.FFmpeg     --accept-source-agreements --accept-package-agreements }
try { python -m pip install --user --quiet pillow 2>$null } catch {}

# 4) Launch
$editor = Join-Path $AppDir 'editor'
if (Missing python) { Write-Host 'Python was just installed — please reboot once, then run SmarterClip again.' -ForegroundColor Yellow; Read-Host 'Press Enter to exit'; exit 0 }
Start-Process 'http://127.0.0.1:8765/'
Write-Host 'Editor running at http://127.0.0.1:8765/  — keep this window open; close it to quit.' -ForegroundColor Cyan
Set-Location $editor
do {                                   # exit code 42 = in-app "Update & restart" — re-run the (now updated) server
  python server.py
  $code = $LASTEXITCODE
  if ($code -eq 42) { Write-Host 'Restarting after update...' -ForegroundColor Yellow; Start-Sleep -Milliseconds 500 }
} while ($code -eq 42)

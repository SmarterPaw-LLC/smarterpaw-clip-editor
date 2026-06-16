$ErrorActionPreference = "Stop"
$bin     = "C:\Users\Jason\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin"
$ffmpeg  = Join-Path $bin "ffmpeg.exe"
$proj    = "C:\Users\Jason\smarterpaw-video-meowi"
$src     = Join-Path $proj "sources\youtube\rollies"
$tmp     = Join-Path $proj "_build_tmp"
$exp     = Join-Path $proj "exports"
$music   = Join-Path $proj "sources\music\1076_smile.mp3"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Path $tmp | Out-Null
if (-not (Test-Path $exp)) { New-Item -ItemType Directory -Path $exp | Out-Null }

function Find-Src($idsub) {
  $f = Get-ChildItem -Path $src -Filter *.mp4 | Where-Object { $_.Name -like "*$idsub*" } | Select-Object -First 1
  if (-not $f) { throw "source not found: $idsub" }
  return $f.FullName
}

# EDL: id substring, in (s), dur (s), caption, zoom
$EDL = @(
  @{id="8bjuag2ceXA"; in=1.0;  dur=2.6; cap="when the joints come out";  zoom=1.0; anchor="center"} # 11 white reveal
  @{id="J22p8Etnkrc"; in=1.0;  dur=2.0; cap="your cats new obsession";   zoom=1.0; anchor="center"} # 12 tabby beauty
  @{id="8bjuag2ceXA"; in=5.5;  dur=1.6; cap="they cant get enough";      zoom=1.0; anchor="center"} # 11 white play
  @{id="J22p8Etnkrc"; in=17.5; dur=1.6; cap="every single cat";          zoom=1.0; anchor="center"} # 12 tabby trot
  @{id="8bjuag2ceXA"; in=12.5; dur=1.4; cap="real catnip they play with";zoom=1.0; anchor="center"} # 11 white paw pack
  @{id="J22p8Etnkrc"; in=29.5; dur=2.3; cap="then total zen";            zoom=1.0; anchor="center"} # 12 tabby curl/sleep
  @{id="8bjuag2ceXA"; in=39.0; dur=3.2; cap="";                          zoom=1.0; anchor="center"} # 11 white zen (end)
)
$ENDCARD_DUR = 2.6
$FONT = "C\:/Windows/Fonts/arialbd.ttf"
$ENC = @("-c:v","libx264","-preset","veryfast","-crf","19","-pix_fmt","yuv420p","-r","30","-video_track_timescale","90000","-an")

function Esc-Path($p) { ($p -replace '\\','/') -replace ':','\:' }

function Build-Format($W,$H,$outName) {
  Write-Host "===== Building $outName ($W x $H) ====="
  $fs   = [int]($W * 0.058)
  $capY = [int]($H * 0.62)
  $fdir = Join-Path $tmp ("{0}x{1}" -f $W,$H)
  New-Item -ItemType Directory -Path $fdir | Out-Null
  $listLines = @()
  for ($i=0; $i -lt $EDL.Count; $i++) {
    $s = $EDL[$i]
    $sf = Find-Src $s.id
    $z = [math]::Max([double]$s.zoom, 1.0)
    $sw = [math]::Ceiling($W*$z); $sh = [math]::Ceiling($H*$z)
    switch ($s.anchor) {
      "bottom" { $cx = "(iw-${W})/2"; $cy = "(ih-${H})" }
      "top"    { $cx = "(iw-${W})/2"; $cy = "0" }
      "right"  { $cx = "(iw-${W})";   $cy = "(ih-${H})/2" }
      "left"   { $cx = "0";           $cy = "(ih-${H})/2" }
      default  { $cx = "(iw-${W})/2"; $cy = "(ih-${H})/2" }
    }
    $vf = "scale=${sw}:${sh}:force_original_aspect_ratio=increase,crop=${W}:${H}:${cx}:${cy}"
    $vf += ",setsar=1,fps=30,format=yuv420p"
    if ($s.cap -ne "") {
      $capFile = Join-Path $fdir ("cap_$i.txt")
      [System.IO.File]::WriteAllText($capFile, $s.cap)
      $cfe = Esc-Path $capFile
      $vf += ",drawtext=fontfile='${FONT}':textfile='${cfe}':fontcolor=white:fontsize=${fs}:borderw=7:bordercolor=black@0.95:box=1:boxcolor=black@0.30:boxborderw=18:x=(w-text_w)/2:y=${capY}"
    }
    $segOut = Join-Path $fdir ("seg_{0:D2}.mp4" -f $i)
    & $ffmpeg -y -loglevel error -ss $s.in -i $sf -t $s.dur -vf $vf @ENC $segOut
    if ($LASTEXITCODE -ne 0) { throw "seg $i failed" }
    $listLines += "file '$($segOut -replace '\\','/')'"
  }
  # end card
  $ec = Join-Path $fdir "seg_99_endcard.mp4"
  $f1 = [int]($W*0.085); $f2 = [int]($W*0.048); $f3 = [int]($W*0.052)
  $y1 = [int]($H*0.40);  $y2 = $y1 + [int]($f1*1.15); $y3 = [int]($H*0.60)
  $ecvf = "drawtext=fontfile='${FONT}':text='MEOWIJUANA':fontcolor=white:fontsize=${f1}:x=(w-text_w)/2:y=${y1}," +
          "drawtext=fontfile='${FONT}':text='CATNIP JOINTS':fontcolor=0xF5A623:fontsize=${f2}:x=(w-text_w)/2:y=${y2}," +
          "drawtext=fontfile='${FONT}':text='SHOP NOW':fontcolor=black:fontsize=${f3}:box=1:boxcolor=0xF5A623:boxborderw=26:x=(w-text_w)/2:y=${y3}," +
          "format=yuv420p"
  & $ffmpeg -y -loglevel error -f lavfi -i "color=c=0x111111:s=${W}x${H}:r=30:d=${ENDCARD_DUR}" -vf $ecvf @ENC $ec
  if ($LASTEXITCODE -ne 0) { throw "endcard failed" }
  $listLines += "file '$($ec -replace '\\','/')'"

  # concat
  $listFile = Join-Path $fdir "concat.txt"
  [System.IO.File]::WriteAllLines($listFile, $listLines)
  $silent = Join-Path $fdir "silent.mp4"
  & $ffmpeg -y -loglevel error -f concat -safe 0 -i $listFile -c copy $silent
  if ($LASTEXITCODE -ne 0) { throw "concat failed" }

  # total duration
  $total = 0.0; foreach ($s in $EDL) { $total += [double]$s.dur }; $total += $ENDCARD_DUR
  $fadeSt = [math]::Round($total - 1.0, 2)
  $outFile = Join-Path $exp $outName
  $afilter = "[1:a]atrim=0:${total},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,afade=t=out:st=${fadeSt}:d=1,loudnorm=I=-16:TP=-1.5:LRA=11[a]"
  & $ffmpeg -y -loglevel error -i $silent -i $music -filter_complex $afilter -map 0:v -map "[a]" -c:v copy -c:a aac -b:a 192k -movflags +faststart -shortest $outFile
  if ($LASTEXITCODE -ne 0) { throw "mux failed" }
  Write-Host "WROTE $outFile  (total ~${total}s)"
}

Build-Format 1080 1920 "Meowi_rollies_hero_9x16.mp4"
Build-Format 1080 1350 "Meowi_rollies_hero_4x5.mp4"
Write-Host "ALL DONE"

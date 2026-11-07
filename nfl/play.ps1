# play.ps1 -- one click: find the best feed, max the quality, buffer it locally, play it.
#
#   NFL.bat                     normal use (double-click)
#   powershell -File play.ps1 -Check        list feeds + quality, play nothing
#   powershell -File play.ps1 -Menu         pick a feed yourself
#   powershell -File play.ps1 -Record       save the game into rec\ while watching
#   powershell -File play.ps1 -LowLatency   less buffering, closer to live
param(
    [switch]$Check,
    [switch]$Menu,
    [switch]$Record,
    [switch]$LowLatency,
    [switch]$NoRelay,
    [int]$Minutes = 180
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'
$root     = $PSScriptRoot
$feedFile = Join-Path $root 'feeds.txt'
$liveFile = Join-Path $root 'live.txt'
$recDir   = Join-Path $root 'rec'
$relayPy  = Join-Path $root 'relay.py'
$UA       = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'

function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Pause-Exit { Read-Host 'Press Enter to close' | Out-Null; exit }

# ---------------------------------------------------------------- tools
function Find-Tool([string]$name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $winget = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Directory -Filter 'Gyan.FFmpeg*' -ErrorAction SilentlyContinue
    foreach ($d in $winget) {
        $hit = Get-ChildItem $d.FullName -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    foreach ($p in @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\$name.exe", "$env:ProgramFiles\ffmpeg\bin\$name.exe", "C:\ffmpeg\bin\$name.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$ffm     = Find-Tool 'ffmpeg'
$ffprobe = Find-Tool 'ffprobe'
$py      = Find-Tool 'python'
if (-not $py) { $py = Find-Tool 'py' }

$player = $null
if (Get-Command mpv -ErrorAction SilentlyContinue) { $player = @{ exe = (Get-Command mpv).Source; kind = 'mpv' } }
if (-not $player) {
    foreach ($p in @("$env:ProgramFiles\VideoLAN\VLC\vlc.exe",
                     "${env:ProgramFiles(x86)}\VideoLAN\VLC\vlc.exe",
                     "$env:LOCALAPPDATA\Programs\vlc\vlc.exe",
                     "$env:LOCALAPPDATA\Programs\VideoLAN\VLC\vlc.exe")) {
        if (Test-Path $p) { $player = @{ exe = $p; kind = 'vlc' }; break }
    }
}
if (-not $player -and $ffm) {
    $fp = Join-Path (Split-Path $ffm) 'ffplay.exe'
    if (Test-Path $fp) { $player = @{ exe = $fp; kind = 'ffplay' } }
}

# ---------------------------------------------------------------- feeds
function Read-Feeds([string]$path) {
    if (-not (Test-Path $path)) { return @() }
    $list = @()
    foreach ($raw in (Get-Content -LiteralPath $path -Encoding UTF8)) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -match '^https?://') { $list += [pscustomobject]@{ Name = 'feed'; Url = $line }; continue }
        $parts = $line -split '\s*\|\s*'
        if ($parts.Count -ge 2) {
            $u = $parts[-1].Trim()
            if ($u -match '^https?://') {
                $list += [pscustomobject]@{ Name = (($parts[0..($parts.Count - 2)]) -join ' | ').Trim(); Url = $u }
            }
        }
    }
    return $list
}

$feeds = @()
$feeds += Read-Feeds $liveFile      # game-specific, written by refresh.ps1 -- tried first
$feeds += Read-Feeds $feedFile
$seen = @{}; $uniq = @()
foreach ($f in $feeds) { if (-not $seen.ContainsKey($f.Url)) { $seen[$f.Url] = 1; $uniq += $f } }
$feeds = $uniq

if ($feeds.Count -eq 0) {
    Say ''
    Say '  feeds.txt is empty.' 'Red'
    Say "  Open it and add one line:   NAME | http://something/index.m3u8" 'Yellow'
    Pause-Exit
}

# ---------------------------------------------------------------- quality probe
# Asks relay.py to: resolve the master playlist, pick the highest rendition,
# download one segment, and report resolution / declared bitrate / real speed.
function Probe-Feed([string]$url) {
    if (-not $py -or -not (Test-Path $relayPy)) { return $null }
    $out = & $py $relayPy --url $url --probe-only 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = ($out | Where-Object { $_ -match '^\s*\{.*\}\s*$' } | Select-Object -Last 1)
    if (-not $line) { return $null }
    try { $p = ($line | ConvertFrom-Json) } catch { return $null }

    # Direct media playlists (no master) report no resolution and no bitrate.
    # Derive the real bitrate from the segment we already downloaded, and ask
    # ffprobe for the resolution, so ranking between feeds stays honest.
    if (-not $p.bw_mbps -and $p.measured_bitrate_mbps) { $p.bw_mbps = $p.measured_bitrate_mbps }
    if (-not $p.bw_mbps -and $p.seg_seconds -gt 0) {
        $p.bw_mbps = [Math]::Round(($p.seg_MB * 8) / $p.seg_seconds, 2)
    }
    if ((-not $p.w -or -not $p.h) -and $ffprobe) {
        $o = & $ffprobe -v error -rw_timeout 9000000 -user_agent $UA -select_streams v:0 `
                        -show_entries stream=width,height -of csv=p=0 -i $url 2>&1
        $hit = $o | Where-Object { $_ -match '^\d+,\d+' } | Select-Object -Last 1
        if ($hit) { $f = $hit -split ','; $p.w = [int]$f[0]; $p.h = [int]$f[1] }
    }
    return $p
}

# ---------------------------------------------------------------- check mode
if ($Check) {
    Say ''
    Say '  QUALITY CHECK  (resolution / declared bitrate / measured download)' 'Cyan'
    Say '  ------------------------------------------------------------------------' 'DarkGray'
    $i = 0
    foreach ($f in $feeds) {
        $i++
        Write-Host ("  [{0}] {1,-40} " -f $i, $f.Name) -NoNewline
        $p = Probe-Feed $f.Url
        if (-not $p) { Say 'DEAD' 'DarkYellow'; continue }
        $flag = if ($p.h -ge 1080) { 'Green' } elseif ($p.h -ge 720) { 'Yellow' } else { 'DarkYellow' }
        Say ("{0}x{1}@{2} {3,6:N1} Mbps declared, {4,6:N1} Mbps actual" -f $p.w, $p.h, $p.fps, $p.bw_mbps, $p.fetch_Mbps) $flag
    }
    Say ''
    Say '  Frames are pulled straight from the CDN, so "actual" is your real ceiling.' 'DarkGray'
    Say ''
    Pause-Exit
}

# ---------------------------------------------------------------- menu
$order = $feeds
if ($Menu) {
    Say ''
    Say '  Pick a stream:' 'Cyan'
    for ($i = 0; $i -lt $feeds.Count; $i++) { Write-Host ('  [{0}] {1}' -f ($i + 1), $feeds[$i].Name) }
    $sel = Read-Host '  Number (Enter = auto-pick best)'
    if ($sel -match '^\d+$' -and [int]$sel -ge 1 -and [int]$sel -le $feeds.Count) { $order = @($feeds[[int]$sel - 1]) }
}

# ---------------------------------------------------------------- pick
Say ''
if (-not $Menu) {
    Say '  Straight to the channel, no menu. For one specific game, use the "NFL Games" icon.' 'DarkGray'
}
Say '  Looking for the best stream...' 'Cyan'
$pick = $null
$n = 0
foreach ($f in $order) {
    $n++
    Write-Host ("   {0}/{1}  {2}" -f $n, $order.Count, $f.Name) -NoNewline
    $p = Probe-Feed $f.Url
    if (-not $p) { Say '  -- dead' 'DarkYellow'; continue }
    Say ("  {0}x{1} @ {2:N1} Mbps" -f $p.w, $p.h, $p.bw_mbps) 'Green'
    $f | Add-Member -NotePropertyName Probe -NotePropertyValue $p -Force
    $pick = $f
    # already the best thing that exists: 1080p and comfortably fast -- stop looking
    if ($p.h -ge 1080 -and $p.fetch_Mbps -ge 2.5 * [Math]::Max(1, $p.bw_mbps)) { break }
}

if (-not $pick) {
    Say ''
    Say '  Nothing is playing right now.' 'Red'
    Say '  Try again shortly, or run  refresh.ps1  to hunt for new links.' 'Yellow'
    Say ''
    Pause-Exit
}

$p = $pick.Probe
Say ''
Say ("  SELECTED: " + $pick.Name) 'Green'
Say ("  {0}x{1} @ {2} fps, {3:N2} Mbps  (link: {4:N1} Mbps measured)" -f $p.w, $p.h, $p.fps, $p.bw_mbps, $p.fetch_Mbps) 'DarkGray'

# ---------------------------------------------------------------- record
if ($Record) {
    New-Item -ItemType Directory -Force -Path $recDir | Out-Null
    $file = Join-Path $recDir ("nfl_{0}.ts" -f (Get-Date -Format 'yyyyMMdd_HHmm'))
    if (-not $ffm) { Say '  ffmpeg not found, cannot record.' 'Red'; Pause-Exit }
    Say "  Recording -> $file   (close this window to stop)" 'Cyan'
    & $ffm -hide_banner -loglevel warning -stats -y -user_agent $UA `
        -rw_timeout 20000000 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 `
        -i $pick.Url -t ($Minutes * 60) -map 0:v:0 -map 0:a:0 -c copy -f mpegts $file
    Say "  Saved: $file" 'Green'
    Pause-Exit
}

if (-not $player) {
    Say '  No player found. Install VLC, or copy the link:' 'Red'
    Say "  $($pick.Url)" 'White'
    $pick.Url | Set-Clipboard
    Pause-Exit
}

# ---------------------------------------------------------------- local relay
# The relay holds ~N seconds of already-downloaded video and serves it over
# localhost. The player stops talking to the internet entirely, so a bad
# upstream segment can never freeze the picture. It also picks the top
# rendition for us, so we never get handed a 480p variant by the CDN.
$playUrl = $pick.Url
$relayProc = $null
$port = 0

if (-not $NoRelay -and $py -and (Test-Path $relayPy)) {
    foreach ($cand in 8823..8840) {
        $tc = New-Object Net.Sockets.TcpClient
        try { $tc.Connect('127.0.0.1', $cand); $tc.Close() } catch { $port = $cand; break }
    }
    if ($port -gt 0) {
        # more upstream sockets = more aggregate throughput + hides jitter
        $prefetch = [Math]::Min(8, [Math]::Max(2, [int][Math]::Ceiling($p.bw_mbps / 1.5)))
        $window   = if ($LowLatency) { 6 } else { 12 }
        $argStr = '"{0}" --url "{1}" --port {2} --window {3} --prefetch {4} --idle-timeout 600' -f `
                  $relayPy, $pick.Url, $port, $window, $prefetch
        $relayProc = Start-Process -FilePath $py -ArgumentList $argStr -PassThru -WindowStyle Hidden `
                        -RedirectStandardOutput (Join-Path $root 'relay.log') `
                        -RedirectStandardError  (Join-Path $root 'relay.err.log')

        Write-Host ("   buffering locally (window {0} seg, {1} parallel connections) " -f $window, $prefetch) -NoNewline
        $ok = $false
        $deadline = (Get-Date).AddSeconds(50)
        while ((Get-Date) -lt $deadline) {
            try {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2 -UseBasicParsing
                if ($r.StatusCode -eq 200) { $ok = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 800
        }
        if ($ok) {
            Say 'ready' 'Green'
            $playUrl = "http://127.0.0.1:$port/live.m3u8"
            $st = $null
            try { $st = Invoke-RestMethod -Uri "http://127.0.0.1:$port/stats" -TimeoutSec 3 } catch { }
            if ($st) { Say ("   {0} MB / {1}s pre-buffered, upstream pull {2} Mbps" -f $st.buffered_MB, $st.buffered_seconds, $st.upstream_Mbps) 'DarkGray' }
        } else {
            Say 'timed out -> playing the direct link instead' 'DarkYellow'
            if ($relayProc -and -not $relayProc.HasExited) { $relayProc.Kill() }
            $relayProc = $null
        }
    }
}

# ---------------------------------------------------------------- play
Say ''
switch ($player.kind) {
    'mpv' {
        Start-Process -FilePath $player.exe -ArgumentList @(
            '--fs', '--cache=yes', '--demuxer-max-bytes=512MiB', '--demuxer-readahead-secs=60',
            '--hls-live-edge=3', '--force-window=yes', '--title=NFL', $playUrl) | Out-Null
    }
    'vlc' {
        $netCache = if ($playUrl -like 'http://127.0.0.1*') { 3000 } else { 4000 }
        Start-Process -FilePath $player.exe -ArgumentList @(
            '--fullscreen', '--no-video-title-show', "--network-caching=$netCache",
            '--adaptive-logic=highest', '--no-one-instance', $playUrl) | Out-Null
    }
    'ffplay' {
        Start-Process -FilePath $player.exe -ArgumentList @(
            '-hide_banner', '-loglevel', 'error', '-infbuf', '-fs', '-user_agent', $UA, $playUrl) | Out-Null
    }
}
Say ("  Playing in {0}.  F = fullscreen   Esc/Q = quit" -f $player.kind) 'Cyan'
Say '  (this window can be closed; the relay shuts itself down afterwards)' 'DarkGray'
Start-Sleep -Seconds 4
exit 0

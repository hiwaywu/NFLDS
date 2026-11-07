# deploy.ps1 -- push this tree onto the machine that actually serves the phone.
#
#   powershell -ExecutionPolicy Bypass -File tools\deploy.ps1
#   powershell -ExecutionPolicy Bypass -File tools\deploy.ps1 -Live "D:\nfl" -Port 8795
#
# Why a script: the repo is in one folder and the running install is in another, and
# fixing a stream bug in the source while the phone keeps talking to yesterday's
# copy has cost real time. This copies the *runtime* files only (never the browser
# profiles or logs), backs up what it overwrites, restarts the hub detached from
# this shell, and proves it answers before returning.
#
# NOTE ON THE FOLDER: do not deploy under C:\AI STUFF\Local\. On this machine the
# launcher .NET exe dies before running a single line there (no log, no window, no
# AppLocker/Defender event) while the very same bytes run from C:\AI STUFF\NFLHub\
# and C:\Users\...\Temp. Native exes are unaffected, so it is specific to managed
# image activation in that tree. C:\AI STUFF\NFLHub is the verified home.
[CmdletBinding()]
param(
    [string]$Live = "C:\AI STUFF\NFLHub",
    [int]$Port = 8795,
    [string]$BindHost = "0.0.0.0"
)

$ErrorActionPreference = 'Continue'

$src = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $src 'nfl\hub\hub.py'))) {
    throw "run me from the repo: tools\deploy.ps1 (looked for nfl\hub\hub.py under $src)"
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$files = @(
    @{ from = 'nfl\hub\hub.py';          to = 'hub\hub.py' },
    @{ from = 'nfl\hub\index.html';      to = 'hub\index.html' },
    @{ from = 'nfl\hub\hls.min.js';      to = 'hub\hls.min.js' },
    @{ from = 'nfl\hub\probe_play.py';   to = 'hub\probe_play.py' },
    @{ from = 'nfl\hub\find_stream.py';  to = 'hub\find_stream.py' },
    @{ from = 'nfl\hub\FIREWALL.bat';    to = 'hub\FIREWALL.bat' },
    @{ from = 'nfl\hub\NFLHUB.bat';      to = 'hub\NFLHUB.bat' },
    @{ from = 'nfl\hub\NFLHub.exe';      to = 'hub\NFLHub.exe' },
    @{ from = 'nfl\relay.py';            to = 'relay.py' },
    @{ from = 'nfl\feeds.txt';           to = 'feeds.txt' },
    @{ from = 'nfl\sites.txt';           to = 'sites.txt' },
    @{ from = 'nfl\games.ps1';           to = 'games.ps1' },
    @{ from = 'nfl\README.txt';          to = 'README.txt' }
)

Write-Host "[1/4] copying runtime files to $Live" -ForegroundColor Cyan
foreach ($f in $files) {
    $from = Join-Path $src $f.from
    $to = Join-Path $Live $f.to
    if (-not (Test-Path $from)) { Write-Host "  skip (missing in repo): $($f.from)" -ForegroundColor DarkYellow; continue }
    $dir = Split-Path -Parent $to
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if (Test-Path $to) {
        $same = (Get-FileHash $from).Hash -eq (Get-FileHash $to).Hash
        if ($same) { Write-Host "  = $($f.to)" -ForegroundColor DarkGray; continue }
        Copy-Item $to "$to.bak-$stamp" -Force
        Write-Host "  ~ $($f.to)  (backup $($f.to).bak-$stamp)" -ForegroundColor Yellow
    } else {
        Write-Host "  + $($f.to)" -ForegroundColor Green
    }
    Copy-Item $from $to -Force
}

Write-Host "[2/4] stopping the old hub and its relays" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'hub\.py|relay\.py' } |
    ForEach-Object {
        Write-Host "  stop $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

Write-Host "[3/4] starting the hub (detached)" -ForegroundColor Cyan
# Resolve pythonw properly: only accept a *file* (Test-Path on 'C:\Users\' succeeds and
# a directory is not an interpreter), prefer a bundled runtime, then installed Pythons,
# then PATH.
$candidates = New-Object System.Collections.Generic.List[string]
$candidates.Add((Join-Path (Split-Path -Parent $Live) 'python\pythonw.exe'))
foreach ($root in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles)) {
    if (-not (Test-Path $root)) { continue }
    Get-ChildItem $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { $candidates.Add((Join-Path $_.FullName 'pythonw.exe')) }
}
$onPath = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if ($onPath) { $candidates.Add($onPath) }

$pythonw = $null
foreach ($cand in $candidates) {
    if (-not $cand) { continue }
    if (Test-Path -LiteralPath $cand -PathType Leaf) { $pythonw = (Resolve-Path -LiteralPath $cand).Path; break }
}
if (-not $pythonw) { throw 'pythonw.exe not found (use the portable bundle, or install Python 3)' }
Write-Host "  interpreter $pythonw"

$cmd = '"{0}" hub.py --port {1} --host {2}' -f $pythonw, $Port, $BindHost
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $cmd
    CurrentDirectory = (Join-Path $Live 'hub')
}
if (-not $r.ProcessId) { throw "could not start the hub (Win32_Process rc=$($r.ReturnValue))" }
Write-Host "  pid $($r.ProcessId)"

Write-Host "[4/4] waiting for /api/ping" -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $p = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/ping" -TimeoutSec 3
        if ($p.app -eq 'nflhub') { $ok = $true; break }
    } catch { }
}
if (-not $ok) { throw "hub did not answer on 127.0.0.1:$Port - run it in a console to see why" }

Write-Host ''
Write-Host "hub up    http://127.0.0.1:$Port/" -ForegroundColor Green
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike '127.*' -and
        $_.IPAddress -notlike '169.254*' -and
        $_.IPAddress -notlike '172.17.*'          # WSL bridge, not reachable from a phone
    } | Sort-Object InterfaceAlias
if ($ips) {
    Write-Host "phone     pick the one on your wifi:" -ForegroundColor Green
    foreach ($a in $ips) {
        Write-Host ("          http://{0}:{1}/   ({2})" -f $a.IPAddress, $Port, $a.InterfaceAlias) -ForegroundColor Green
    }
}
Write-Host "browser   $($p.browser)  $($p.browser_exe)" -ForegroundColor DarkGray
Write-Host "files     $(($files | Where-Object { Test-Path (Join-Path $Live $_.to) }).Count)/$($files.Count) present in $Live"

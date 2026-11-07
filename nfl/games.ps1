# games.ps1 -- list the NFL games the stream sites are carrying, then open one.
#
#   GAMES.bat                       list games, pick a number
#   powershell -File games.ps1 -Raw  just print the list
#
# The listing pages are plain HTML, so no browser is needed here. Picking a
# number opens that game's stream page in your normal browser (where you can
# hit Play, and where your ad blocker is already doing its job).
param(
    [string[]]$Site = @('https://methstreams.st/NFL', 'https://crackstreams.st/NFL'),
    [switch]$Raw
)

$ErrorActionPreference = 'SilentlyContinue'
$UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'

function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

function Get-Games([string]$page) {
    $html = (& curl.exe -sL -m 20 -A $UA $page 2>$null) -join "`n"
    if (-not $html) { return @() }
    $out = @()

    # the listing pages embed a schema.org ItemList -- cleanest source by far
    foreach ($m in [regex]::Matches($html, '(?s)<script type="application/ld\+json">(.*?)</script>')) {
        $json = $m.Groups[1].Value
        if ($json -notmatch 'ItemList') { continue }
        try { $obj = $json | ConvertFrom-Json } catch { continue }
        foreach ($it in $obj.itemListElement) {
            if (-not $it.url) { continue }
            $out += [pscustomobject]@{ Game = $it.name; When = (Get-DateLabel $it.url); Time = ''; Url = $it.url }
        }
    }

    # fallback: plain anchor scan
    if ($out.Count -eq 0) {
        foreach ($m in [regex]::Matches($html, 'href="(?<u>[^"]*/event/[^"]+)"')) {
            $url = $m.Groups['u'].Value
            if (-not $url.StartsWith('http')) {
                $root = [uri]$page
                $url = "$($root.Scheme)://$($root.Host)$url"
            }
            $slug = ($url -split '/event/')[-1] -replace '^m-', '' -replace '-\d{4}$', ''
            $name = (Get-Culture).TextInfo.ToTitleCase(($slug -replace '-vs-', ' vs ' -replace '-', ' '))
            $out += [pscustomobject]@{ Game = $name; When = (Get-DateLabel $url); Time = ''; Url = $url }
        }
    }
    return $out
}

function Get-DateLabel([string]$url) {
    $md = [regex]::Match($url, '-(\d{2})(\d{2})$')
    if ($md.Success) {
        $mon = [int]$md.Groups[1].Value; $day = [int]$md.Groups[2].Value
        if ($mon -ge 1 -and $mon -le 12 -and $day -ge 1 -and $day -le 31) {
            try { return (Get-Date -Year (Get-Date).Year -Month $mon -Day $day).ToString('MMM d') } catch { }
        }
    }
    return ''
}

$all = @()
foreach ($s in $Site) { $all += Get-Games $s }
$seen = @{}; $uniq = @()
foreach ($g in $all) {
    if (-not $g.Game) { continue }
    $key = "$($g.Game)|$($g.When)"
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = 1
    $uniq += $g
}
$all = $uniq | Sort-Object When, Game

if ($all.Count -eq 0) {
    Say ''
    Say '  No games listed right now. Sites usually post the slate in the morning.' 'Yellow'
    Say '  You can still watch NFL TV: double-click NFL.bat' 'Gray'
    Say ''
    if (-not $Raw) { Read-Host 'Press Enter to close' | Out-Null }
    exit 0
}

Say ''
Say ('  NFL GAMES ON THE STREAM SITES  ({0})' -f (Get-Date -Format 'MMM d, HH:mm')) 'Cyan'
Say '  ------------------------------------------------------------' 'DarkGray'
$i = 0
foreach ($g in $all) {
    $i++
    Write-Host ('  [{0,2}] ' -f $i) -NoNewline -ForegroundColor DarkGray
    Write-Host ('{0,-46}' -f $g.Game) -NoNewline
    Write-Host ('{0,-8} {1}' -f $g.When, $g.Time) -ForegroundColor DarkCyan
}
Say ''
Say '  NFL TV (the always-on channel) plays with:   NFL.bat' 'DarkGray'
Say ''

if ($Raw) { exit 0 }

$sel = Read-Host '  Open game number (Enter to quit)'
if ($sel -match '^\d+$' -and [int]$sel -ge 1 -and [int]$sel -le $all.Count) {
    $pick = $all[[int]$sel - 1]
    Say ("  Opening " + $pick.Game) 'Green'

    # the event page lists "Main 1 / Main 2" player links; skip a click by
    # jumping straight to the first one when it is present in the page source
    $target = $pick.Url
    $html = (& curl.exe -sL -m 15 -A $UA $pick.Url 2>$null) -join "`n"
    $m = [regex]::Match($html, 'https?://[^"''<>\s]+?/main/1')
    if ($m.Success) {
        $target = $m.Value
        Say '  (jumped straight to Main 1)' 'DarkGray'
    }

    # open it in Chrome/Firefox, not in whatever Windows calls the default
    # (that is Edge here, and Edge kept being the browser that came up).
    $exe = $null
    $txt = Join-Path $PSScriptRoot 'hub\browser.txt'
    $names = @{
        'chrome'  = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
        'firefox' = "$env:ProgramFiles\Mozilla Firefox\firefox.exe"
        'edge'    = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
        'brave'   = "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"
    }
    if (Test-Path $txt) {
        $want = (Get-Content $txt | Where-Object { $_ -and -not $_.StartsWith('#') } | Select-Object -First 1)
        if ($want) {
            $want = $want.Trim()
            if ($names.ContainsKey($want.ToLower())) { $want = $names[$want.ToLower()] }
            if (Test-Path $want) { $exe = $want }
        }
    }
    if (-not $exe) {
        foreach ($k in 'chrome', 'firefox', 'brave', 'edge') {
            if (Test-Path $names[$k]) { $exe = $names[$k]; break }
        }
    }
    if ($exe) {
        Start-Process -FilePath $exe -ArgumentList $target
        Say ("  Opened in " + (Split-Path $exe -Leaf)) 'DarkGray'
    } else {
        Start-Process $target
        Say '  Opened in the default browser.' 'DarkGray'
    }
    Say '  Press play in the browser.' 'DarkGray'
    Start-Sleep -Seconds 2
}

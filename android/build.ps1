# build.ps1 -- builds NFLHub.apk with the SDK tools directly (no Gradle).
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Continue'   # native tools write to stderr; check $LASTEXITCODE instead

$root = $PSScriptRoot
# The SDK is wherever this machine put it: C:\Android\Sdk on the build box, the
# per-user install on a dev laptop. ANDROID_SDK wins when it is set.
$sdk  = $env:ANDROID_SDK
if (-not $sdk -or -not (Test-Path $sdk)) {
    foreach ($cand in @('C:\Android\Sdk', (Join-Path $env:LOCALAPPDATA 'Android\Sdk'))) {
        if ($cand -and (Test-Path $cand)) { $sdk = $cand; break }
    }
}
if (-not $sdk -or -not (Test-Path $sdk)) { throw "Android SDK not found (set ANDROID_SDK)" }
$bt   = Join-Path $sdk 'build-tools\36.0.0'   # 34.0.0 d8 crashes on inner classes
if (-not (Test-Path $bt)) { $bt = Join-Path $sdk 'build-tools\34.0.0' }
$plat = @(Get-ChildItem (Join-Path $sdk 'platforms') -Directory -ErrorAction SilentlyContinue |
          Sort-Object Name -Descending | Select-Object -First 1)
$jar  = if ($plat) { Join-Path $plat[0].FullName 'android.jar' } else { Join-Path $sdk 'platforms\android-35\android.jar' }
$out  = Join-Path $root 'build'

New-Item -ItemType Directory -Force -Path $out | Out-Null
Get-ChildItem $out -Recurse -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$out\gen", "$out\classes", "$out\dex" | Out-Null
Get-ChildItem $out -Recurse -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'assets' } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# the standalone app carries the PC page, hls.js and the channel list itself
New-Item -ItemType Directory -Force -Path "$out\assets" | Out-Null
$repo = Split-Path -Parent $root
Copy-Item "$repo\nfl\hub\index.html", "$repo\nfl\hub\hls.min.js", "$repo\nfl\hub\shaka.js", "$repo\nfl\feeds.txt" "$out\assets\" -Force

Write-Host '[1/6] compiling resources' -ForegroundColor Cyan
& "$bt\aapt2.exe" compile --dir "$root\res" -o "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw 'aapt2 compile failed' }

Write-Host '[2/6] linking resources + manifest' -ForegroundColor Cyan
& "$bt\aapt2.exe" link -o "$out\base.apk" -I $jar `
    --manifest "$root\AndroidManifest.xml" -R "$out\res.zip" -A "$out\assets" `
    --java "$out\gen" --auto-add-overlay `
    --min-sdk-version 24 --target-sdk-version 34 `
    --version-code 1 --version-name 1.0
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link failed' }

Write-Host '[3/6] compiling java' -ForegroundColor Cyan
$srcs = @(Get-ChildItem "$root\src" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
$srcs += @(Get-ChildItem "$out\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
& javac -source 8 -target 8 -nowarn -encoding UTF-8 -bootclasspath $jar -classpath $jar -d "$out\classes" $srcs
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }

Write-Host '[4/6] dexing' -ForegroundColor Cyan
Push-Location "$out\classes"
& jar cf "$out\classes.jar" -C "$out\classes" .
Pop-Location
& "$bt\d8.bat" --lib $jar --min-api 24 --output "$out\dex" "$out\classes.jar"
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }

Write-Host '[5/6] packaging' -ForegroundColor Cyan
Copy-Item "$out\base.apk" "$out\unsigned.apk" -Force
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open("$out\unsigned.apk", 'Update')
[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
    $zip, "$out\dex\classes.dex", 'classes.dex',
    [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
$zip.Dispose()

Write-Host '[6/6] aligning + signing' -ForegroundColor Cyan
$ks = Join-Path $root 'debug.keystore'
if (-not (Test-Path $ks)) {
    & keytool -genkeypair -v -keystore $ks -storepass android -keypass android `
        -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 `
        -dname 'CN=Android Debug,O=Android,C=US' *> $null
}
& "$bt\zipalign.exe" -f 4 "$out\unsigned.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }

& "$bt\apksigner.bat" sign --ks $ks --ks-pass pass:android --key-pass pass:android `
    --out (Join-Path $root 'NFLHub.apk') "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'apksigner failed' }

& "$bt\apksigner.bat" verify --print-certs (Join-Path $root 'NFLHub.apk') | Out-Null
Write-Host ''
Write-Host ('OK -> ' + (Join-Path $root 'NFLHub.apk')) -ForegroundColor Green
Write-Host ('size  ' + [math]::Round((Get-Item (Join-Path $root 'NFLHub.apk')).Length/1KB) + ' KB')

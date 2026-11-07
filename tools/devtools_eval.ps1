# evaluate a JS expression inside the app's WebView over the DevTools WS protocol
param(
    [string]$Expression,
    [string]$ExpressionFile,
    [string]$Port = '9222',
    [int]$TimeoutSec = 90
)
if ($ExpressionFile -and (Test-Path $ExpressionFile)) {
    $Expression = [IO.File]::ReadAllText($ExpressionFile, [Text.Encoding]::UTF8)
}
if (-not $Expression) { throw 'no expression given' }
$ErrorActionPreference = 'Stop'
$targets = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json" -UseBasicParsing -TimeoutSec 8).Content | ConvertFrom-Json
$page = $targets | Where-Object { $_.type -eq 'page' } | Select-Object -First 1
if (-not $page) { throw 'no page target' }
$wsUrl = $page.webSocketDebuggerUrl -replace '^(ws://[^:]+):\d+/', "ws://127.0.0.1:$Port/"

$ws = [System.Net.WebSockets.ClientWebSocket]::new()
$ct = [System.Threading.CancellationToken]::None
$ws.ConnectAsync([Uri]$wsUrl, $ct).Wait(5000) | Out-Null

function Send-Cdp($obj){
    $json = $obj | ConvertTo-Json -Depth 6 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $seg = [ArraySegment[byte]]::new($bytes)
    try { $ws.SendAsync($seg, 'Text', $true, $ct).Wait(15000) | Out-Null } catch { Write-Host "send failed: $($_.Exception.InnerException.Message)" }
}
function Recv-Cdp($id, $timeoutSec){
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSec)
    $buf = New-Object byte[] (2MB)
    while ([DateTime]::UtcNow -lt $deadline){
        $ms = New-Object System.IO.MemoryStream
        while ($ws.State -eq [System.Net.WebSockets.WebSocketState]::Open){
            $s = New-Object System.ArraySegment[byte] -ArgumentList (, $buf)
            $r = $ws.ReceiveAsync($s, $ct)
            try { if (-not $r.Wait(20000)) { return $null } } catch { return $null }
            $ms.Write($buf, 0, $r.Result.Count)
            if ($r.Result.EndOfMessage) { break }
        }
        $text = [Text.Encoding]::UTF8.GetString($ms.ToArray())
        try { $j = $text | ConvertFrom-Json } catch { continue }
        if ($j.id -eq $id) { return $j }
    }
    return $null
}

$theId = 1
Send-Cdp @{ id = $theId; method = 'Runtime.evaluate'; params = @{ expression = $Expression; awaitPromise = $true; returnByValue = $true; timeout = $TimeoutSec * 1000 } }
$resp = Recv-Cdp $theId ($TimeoutSec + 10)
if ($resp -and $resp.result -and $resp.result.result){
    $r = $resp.result.result
    if ($r.type -eq 'string') { $r.value }
    elseif ($r.value -ne $null) { ($r.value | ConvertTo-Json -Depth 4 -Compress) }
    else { "type: $($r.type) subtype: $($r.subtype) desc: $($r.description)" }
    if ($resp.result.exceptionDetails){
        $resp.result.exceptionDetails.exception.description
    }
} else {
    "no response / closed"
}
try { $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, 'done', $ct).Wait(2000) | Out-Null } catch { }

# check.ps1 -- which feeds are alive right now, and at what quality.
# Double-click CHECK.bat, or:  powershell -File check.ps1
& (Join-Path $PSScriptRoot 'play.ps1') -Check

@echo off
title NFL - finding new links
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh.ps1"

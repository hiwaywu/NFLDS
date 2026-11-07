@echo off
title NFL
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0play.ps1" %*
if errorlevel 1 pause

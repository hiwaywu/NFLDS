@echo off
title NFL - game list
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0games.ps1"

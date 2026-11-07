@echo off
title NFL - quality check
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check.ps1"

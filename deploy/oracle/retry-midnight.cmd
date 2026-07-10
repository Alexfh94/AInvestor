@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0retry-midnight.ps1" %*

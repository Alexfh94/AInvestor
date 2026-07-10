@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-oci-api.ps1" %*

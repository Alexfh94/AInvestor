@echo off
cd /d "%~dp0.."
set PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%
echo Iniciando AInvestor en http://127.0.0.1:8000
python -m uvicorn ainvestor.main:app --host 127.0.0.1 --port 8000
pause

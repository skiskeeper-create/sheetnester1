@echo off
rem SheetNester (portable) - local server 127.0.0.1, opens the browser. Keep this window open while working.
cd /d "%~dp0"
python\python.exe server.py
if errorlevel 1 pause

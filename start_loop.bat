@echo off
cd /d "%~dp0"
if not exist data mkdir data
:loop
if exist WATCH_STOP goto end
node watch.mjs >> data\watch_console.log 2>&1
if exist WATCH_STOP goto end
timeout /t 5 /nobreak >nul
goto loop
:end

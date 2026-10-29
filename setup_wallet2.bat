@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 第二個錢包用獨立的 baw 副本：停用系統鑰匙圈（keytar），session 存在 baw2 資料夾，不會跟第一個錢包互相踢下線
if not exist baw2pkg mkdir baw2pkg
call npm install @binance/agentic-wallet@1.12.0 --prefix baw2pkg --no-audit --no-fund
if exist baw2pkg\node_modules\@binance\agentic-wallet\node_modules\@github move baw2pkg\node_modules\@binance\agentic-wallet\node_modules\@github baw2pkg\keytar_disabled_1
if exist baw2pkg\node_modules\@github move baw2pkg\node_modules\@github baw2pkg\keytar_disabled_2
if not exist baw2 mkdir baw2
node login.mjs 2
pause

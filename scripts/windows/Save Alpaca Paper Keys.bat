@echo off
title ViceKrack - Save Alpaca PAPER keys
call "%~dp0_find_python.bat"
cd /d "%VK_ROOT%"
%VK_PY% "%VK_ROOT%\scripts\save_alpaca_paper_keys.py"
echo.
pause

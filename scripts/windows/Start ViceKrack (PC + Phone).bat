@echo off
title ViceKrack - PC + Phone (keep this window open)
call "%~dp0_find_python.bat"
cd /d "%VK_ROOT%"
echo Starting ViceKrack HQ (Video Studio) and the Mobile Command Center...
echo Desktop HQ: http://127.0.0.1:8765/
echo.
%VK_PY% -m vicekrack mobile-start
echo.
echo ViceKrack stopped.
pause

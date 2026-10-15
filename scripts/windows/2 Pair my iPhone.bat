@echo off
title ViceKrack Mobile - Pair my iPhone
call "%~dp0_find_python.bat"
cd /d "%VK_ROOT%"
%VK_PY% -m vicekrack mobile-pair
pause

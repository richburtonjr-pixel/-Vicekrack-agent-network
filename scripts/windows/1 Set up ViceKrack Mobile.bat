@echo off
title ViceKrack Mobile - Setup
call "%~dp0_find_python.bat"
cd /d "%VK_ROOT%"
echo.
echo  Step 1 of 2: save your xAI key on this PC (Windows Credential Manager)
echo  ----------------------------------------------------------------------
choice /M " Save or replace the xAI key now"
if errorlevel 2 goto setup
%VK_PY% -m vicekrack mobile-save-key
:setup
echo.
echo  Step 2 of 2: PIN, daily limit and the private connection
echo  --------------------------------------------------------
%VK_PY% -m vicekrack mobile-setup
echo.
pause

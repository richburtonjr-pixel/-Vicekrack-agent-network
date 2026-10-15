@echo off
title ViceKrack - Turn on auto-start
rem Puts a "ViceKrack" shortcut in your Startup folder so ViceKrack starts (minimized) when you sign in to Windows.
set "TARGET=%~dp0Start ViceKrack (PC + Phone).bat"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\ViceKrack.lnk');" ^
  "$s.TargetPath=$env:TARGET; $s.WorkingDirectory=(Split-Path $env:TARGET); $s.WindowStyle=7; $s.Save()"
if errorlevel 1 (echo Could not add the shortcut.) else (echo Done. ViceKrack will start by itself when you sign in to Windows.)
echo.
echo Also set Windows to never sleep while plugged in:
echo   Settings ^> System ^> Power ^> Screen and sleep ^> "When plugged in, put my device to sleep after" = Never
echo.
pause

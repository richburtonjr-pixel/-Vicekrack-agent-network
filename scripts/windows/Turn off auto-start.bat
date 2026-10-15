@echo off
title ViceKrack - Turn off auto-start
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p=[Environment]::GetFolderPath('Startup')+'\ViceKrack.lnk'; if (Test-Path $p) { Remove-Item $p; 'Auto-start is off.' } else { 'Auto-start was already off.' }"
pause

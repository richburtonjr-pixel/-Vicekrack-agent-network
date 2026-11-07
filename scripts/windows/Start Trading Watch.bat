@echo off
title ViceKrack - Trading Watch (keep this window open; close it to stop)
call "%~dp0_find_python.bat"
cd /d "%VK_ROOT%"
echo ViceKrack Trading Watch: free Kraken BTC/ETH prices every 5 minutes.
echo Practice trades with SIMULATED money only, after every agent approves. The $80 shutdown is checked every cycle.
echo While this window is open the PC will not go to sleep. Close it to stop.
echo.
%VK_PY% -m vicekrack practice-status >nul 2>nul || %VK_PY% -m vicekrack practice-start >nul
%VK_PY% -m vicekrack watch-run
echo.
echo Trading Watch stopped.
pause

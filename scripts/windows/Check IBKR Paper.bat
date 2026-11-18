@echo off
title ViceKrack - Check Interactive Brokers (PAPER, read-only)
call "%~dp0_find_python.bat"
cd /d "%VK_ROOT%"
echo Checks the connection to Interactive Brokers PAPER trading. Read-only: no orders are placed.
echo Before running: open IB Gateway, log in with your PAPER username, and turn on the API (port 4002).
echo.
%VK_PY% -m pip install -q -r requirements-brokers.txt
%VK_PY% -m vicekrack ibkr-paper-check --allow-network
echo.
pause

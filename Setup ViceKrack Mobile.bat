@echo off
rem ============================================================================
rem  Setup ViceKrack Mobile  -  double-click this file.
rem  One-time setup for the Mobile Command Center (docs/mobile.md):
rem    1. checks Tailscale (the private, phone-only connection)
rem    2. saves your xAI key in Windows Credential Manager (hidden; never in a file or on screen)
rem    3. sets your PIN for paid actions and the daily spending limit
rem    4. turns on Tailscale Serve (private to your Tailscale account; Funnel is never used)
rem    5. puts "ViceKrack (PC + Phone)" and "Pair my iPhone" shortcuts on your desktop
rem  Safe to run again: it keeps what is already set unless you choose to change it.
rem ============================================================================
setlocal
title ViceKrack Mobile - Setup
cd /d "%~dp0"
set "VK_WIN=%~dp0scripts\windows"
if not exist "%VK_WIN%\_find_python.bat" goto :missing
call "%VK_WIN%\_find_python.bat"
cd /d "%VK_ROOT%"

echo.
echo  ==============================================================
echo    ViceKrack Mobile Command Center - one-time setup
echo  ==============================================================
echo.

rem ---- Step 1 of 5: is ViceKrack ready on this PC? --------------------------
echo  Step 1 of 5: checking ViceKrack on this PC...
git pull --quiet >nul 2>&1
%VK_PY% -c "import vicekrack.mobile.cli" >nul 2>&1
if errorlevel 1 goto :nopython
echo     OK
echo.

rem ---- Step 2 of 5: Tailscale ------------------------------------------------
echo  Step 2 of 5: checking Tailscale (the private connection to your phone)...
set "VK_TS=C:\Program Files\Tailscale\tailscale.exe"
if not exist "%VK_TS%" goto :notailscale
"%VK_TS%" status >nul 2>&1
if errorlevel 1 goto :tailscaleoff
echo     OK - Tailscale is installed and signed in.
echo.

rem ---- Step 3 of 5: xAI key --------------------------------------------------
echo  Step 3 of 5: your xAI key (kept in Windows Credential Manager)
%VK_PY% -c "import sys; from vicekrack.mobile.winkey import load_key; sys.exit(0 if load_key() else 1)" >nul 2>&1
if errorlevel 1 goto :nokey
echo     A key is already saved.
choice /C YN /N /M "    Replace it with a different key? [Y/N] "
if errorlevel 2 goto :keydone
goto :askkey

:nokey
set "VK_OLDKEY=%LOCALAPPDATA%\ViceKrack\xai-key.secure"
if not exist "%VK_OLDKEY%" goto :askkey
echo     Found the key you already saved with ViceKrack.bat on this PC.
choice /C YN /N /M "    Use that same key for the phone too? [Y/N] "
if errorlevel 2 goto :askkey
rem The key goes straight from the encrypted file into Credential Manager. It is never shown.
powershell -NoProfile -Command "try { $s = (Get-Content -LiteralPath $env:VK_OLDKEY -TotalCount 1) | ConvertTo-SecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) } catch { }" | %VK_PY% -c "import sys; from vicekrack.mobile.winkey import save_key; save_key(sys.stdin.readline())" >nul 2>&1
if errorlevel 1 goto :oldkeyfailed
echo     Saved.
goto :keydone

:oldkeyfailed
echo     That saved key could not be copied. Please paste it instead.
:askkey
echo.
echo     Paste your xAI key and press Enter. Right-click pastes.
echo     Nothing appears on screen while you paste - that is normal.
%VK_PY% -m vicekrack mobile-save-key
if errorlevel 1 goto :keyfailed
:keydone
echo.

rem ---- Step 4 of 5: PIN, daily limit, private address -----------------------
echo  Step 4 of 5: your PIN for paid actions, the daily limit and the private address
echo     The PIN is 4 to 8 digits. Your phone must enter it before anything that costs money.
echo     Five wrong tries lock paid actions for 15 minutes.
echo     Press Enter at the daily limit question to keep the amount shown.
echo.
%VK_PY% -m vicekrack mobile-setup
if errorlevel 1 goto :setupfailed
echo.

rem ---- Step 5 of 5: desktop shortcuts ----------------------------------------
echo  Step 5 of 5: adding shortcuts to your desktop...
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $w=New-Object -ComObject WScript.Shell; foreach($p in @(@('ViceKrack (PC + Phone)','Start ViceKrack (PC + Phone).bat'),@('Pair my iPhone','2 Pair my iPhone.bat'))){ $s=$w.CreateShortcut((Join-Path $d ($p[0]+'.lnk'))); $s.TargetPath=(Join-Path $env:VK_WIN $p[1]); $s.WorkingDirectory=$env:VK_ROOT; $s.Save() }" >nul 2>&1
if errorlevel 1 goto :noshortcuts
echo     Added "ViceKrack (PC + Phone)" and "Pair my iPhone" to your desktop.
goto :finished
:noshortcuts
echo     Could not add the shortcuts. You can still use the files in the scripts\windows folder.
:finished

echo.
echo  ==============================================================
echo    Setup finished.
echo.
echo    Next:
echo      1. Double-click "ViceKrack (PC + Phone)" on your desktop
echo         and keep that window open.
echo      2. On your iPhone, turn on Tailscale and open the
echo         https address shown above.
echo      3. Double-click "Pair my iPhone" and type the 6-digit
echo         code on your phone.
echo  ==============================================================
echo.
pause
exit /b 0

rem ---- Problems: each one says exactly what to do ----------------------------
:missing
echo.
echo  This file must stay in the main ViceKrack folder (next to the "scripts" folder).
echo  Nothing was changed.
echo.
pause
exit /b 1

:nopython
echo.
echo  ViceKrack's Python could not load the mobile code on this PC.
echo  Double-click ViceKrack.bat once to update, close it, then run this setup again.
echo  Nothing was changed.
echo.
pause
exit /b 1

:notailscale
echo.
echo  Tailscale is not installed. Your browser will open the download page.
echo  Install it, sign in with the same account as on your iPhone,
echo  then double-click this setup again.
start "" "https://tailscale.com/download/windows"
echo.
pause
exit /b 1

:tailscaleoff
echo.
echo  Tailscale is installed but not connected.
echo  Click the Tailscale icon near the clock, choose "Log in" or "Connect",
echo  then double-click this setup again.
echo.
pause
exit /b 1

:keyfailed
echo.
echo  The key was not saved. Run this setup again and paste the key from the xAI console.
echo.
pause
exit /b 1

:setupfailed
echo.
echo  Setup stopped before finishing. Take a screenshot of this window for Claude.
echo.
pause
exit /b 1

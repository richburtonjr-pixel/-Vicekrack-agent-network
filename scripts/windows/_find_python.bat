@echo off
rem Shared helper: sets VK_ROOT (the ViceKrack folder) and VK_PY (its Python, quoted). Used by the other .bat files.
set "VK_ROOT=%~dp0..\.."
pushd "%VK_ROOT%"
set "VK_ROOT=%CD%"
popd
if exist "%VK_ROOT%\.venv\Scripts\python.exe" (
  set VK_PY="%VK_ROOT%\.venv\Scripts\python.exe"
  goto :eof
)
if exist "%USERPROFILE%\vicekrack\.venv\Scripts\python.exe" (
  set VK_PY="%USERPROFILE%\vicekrack\.venv\Scripts\python.exe"
  goto :eof
)
where py >nul 2>nul
if not errorlevel 1 (
  set VK_PY=py -3
  goto :eof
)
set VK_PY=python

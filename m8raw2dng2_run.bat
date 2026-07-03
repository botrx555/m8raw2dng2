@echo off
REM m8raw2dng2 launcher (Windows) - see README "Launchers" for setup and flags.
setlocal
chcp 65001 >nul

set "PYTHON=python"
set "SCRIPT=%~dp0m8raw2dng2.py"
set "INPUT=C:\Photos\M8\RAW"
set "OUTPUT="
set "FLAGS=-v -p -b -s --no-crop --cfa RGGB"

if not exist "%SCRIPT%" (
  echo [ERROR] Script not found: %SCRIPT%
  pause >nul & exit /b 1
)
echo Running:  %FLAGS%  -i "%INPUT%"
if defined OUTPUT (
  "%PYTHON%" "%SCRIPT%" %FLAGS% -i "%INPUT%" -o "%OUTPUT%"
) else (
  "%PYTHON%" "%SCRIPT%" %FLAGS% -i "%INPUT%"
)
endlocal

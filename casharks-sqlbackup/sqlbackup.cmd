@echo off
rem Casharks SQL Backup launcher. Set SQLBACKUP_PYTHON to force a specific python.exe.
setlocal
set "PYTHONPATH=%~dp0.;%PYTHONPATH%"
set "PYEXE=%SQLBACKUP_PYTHON%"
set "PYARGS="
if not defined PYEXE (
  where py >nul 2>&1
  if not errorlevel 1 (
    set "PYEXE=py"
    set "PYARGS=-3"
  )
)
if not defined PYEXE set "PYEXE=python"
"%PYEXE%" %PYARGS% -m fivem_sqlbackup %*
exit /b %ERRORLEVEL%

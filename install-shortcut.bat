@echo off
REM install-shortcut.bat - double-click this to put HELIX on your Desktop and Start Menu.
REM
REM Runs scripts\make_shortcut.ps1 from THIS folder, whatever directory the terminal
REM happens to be sitting in. %~dp0 is the folder this .bat lives in, with a trailing
REM backslash, and it is quoted so a space in the path cannot split the command.
REM
REM Anything you type after the file name is passed straight through, so:
REM     install-shortcut.bat -Remove
REM     install-shortcut.bat -Frozen
REM     install-shortcut.bat -Python C:\path\to\python.exe

setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_shortcut.ps1" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo Finished with errors (exit code %RC%). Read the lines above.
pause
endlocal

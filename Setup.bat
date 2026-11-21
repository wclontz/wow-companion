@echo off
rem One-time setup: Python packages, finds the game, copies the add-on. Safe to run again.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" in its installer. Then run Setup.bat.
if errorlevel 1 pause
if errorlevel 1 exit /b 1
python setup_companion.py %*
pause

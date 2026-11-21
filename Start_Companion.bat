@echo off
rem Starts the WoW second-screen companion and opens it in your browser.
rem Drag the browser window to your second monitor, or open the address it prints on a tablet.
rem Close this window to stop.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" in its installer. Then run Setup.bat.
if errorlevel 1 pause
if errorlevel 1 exit /b 1
python companion.py
pause

@echo off
cd /d "%~dp0"

where python >nul 2>nul
if not errorlevel 1 goto run_bot

where py >nul 2>nul
if not errorlevel 1 goto run_py

goto no_python

:run_bot
python bot.py
if errorlevel 1 goto error_exit
exit /b 0

:run_py
py -3 bot.py
if errorlevel 1 goto error_exit
exit /b 0

:no_python
echo.
echo Python was not found.
echo Install Python 3.9 or newer from https://www.python.org/downloads/
echo During installation check "Add python.exe to PATH".
echo.
pause
exit /b 1

:error_exit
echo.
echo The bot stopped with an error.
echo See the messages above and the file bot.log.
pause
exit /b 1

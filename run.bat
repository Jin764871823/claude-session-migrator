@echo off
rem Launcher for Claude session migration GUI (ASCII-only to avoid codepage issues).
rem Tries "pyw -3", then pythonw, then python (all looked up from PATH). Each candidate must
rem run "import tkinter" first, which also skips the Microsoft Store "python" placeholder.
setlocal
set "HERE=%~dp0"

call :try pyw -3 && goto :eof
call :try pythonw && goto :eof
call :try python && goto :eof

echo Python 3 with Tkinter not found. Please install Python 3 from python.org (Tkinter included) and add it to PATH.
pause
goto :eof

:try
%* -c "import sys, tkinter; sys.exit(sys.version_info < (3, 8))" >nul 2>nul || exit /b 1
start "" %* "%HERE%gui.py"
exit /b 0

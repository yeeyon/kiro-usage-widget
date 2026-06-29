@echo off
rem ── Kiro Usage Widget — portable silent launcher (no console window) ──
rem Finds pythonw on this machine, then launches the widget detached.

setlocal
set "DIR=%~dp0"

rem Prefer the py launcher's windowed interpreter, else fall back to PATH.
where pythonw >nul 2>&1 && (
  start "" pythonw "%DIR%kiro_usage_widget.py"
  goto :eof
)

py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" >"%TEMP%\_kuw_pyw.txt" 2>nul
set /p PYW=<"%TEMP%\_kuw_pyw.txt"
del "%TEMP%\_kuw_pyw.txt" >nul 2>&1

if exist "%PYW%" (
  start "" "%PYW%" "%DIR%kiro_usage_widget.py"
) else (
  echo Could not find Python. Run setup.bat first.
  pause
)
endlocal

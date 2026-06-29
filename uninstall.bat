@echo off
rem ── Remove the Kiro Usage Widget ──
echo Stopping widget...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*kiro_usage_widget*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Removing autostart shortcut...
powershell -NoProfile -Command "Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'KiroUsageWidget.lnk') -ErrorAction SilentlyContinue"
echo Done. You can delete this folder now.
pause

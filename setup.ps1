# ============================================================
#  Kiro Usage Widget - setup / onboarding
#  - finds Python (or tells you how to get it)
#  - installs dependencies
#  - verifies it can read your Kiro usage
#  - registers autostart + promotes the tray icon to the taskbar
#  - launches the widget
# ============================================================
$ErrorActionPreference = 'Stop'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Info($m){ Write-Host "  $m" -ForegroundColor Gray }
function Ok($m)  { Write-Host "[ok] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "[!] $m" -ForegroundColor Yellow }
function Err($m) { Write-Host "[x] $m" -ForegroundColor Red }

Write-Host ""
Write-Host "  Kiro Usage Widget - setup" -ForegroundColor Cyan
Write-Host "  -------------------------" -ForegroundColor Cyan

# --- 1. locate Python -----------------------------------------------------
$py = $null; $pyw = $null
function Resolve-Python {
  foreach ($c in @('py -3','python','python3')) {
    $parts = $c.Split(' ')
    $exe = (Get-Command $parts[0] -ErrorAction SilentlyContinue)
    if ($exe) {
      try {
        $base = & $parts[0] $parts[1..($parts.Length-1)] -c "import sys,os;print(os.path.dirname(sys.executable))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $base) {
          $script:py  = $c
          $w = Join-Path $base 'pythonw.exe'
          $script:pyw = (Test-Path $w) ? $w : (Join-Path $base 'python.exe')
          return $true
        }
      } catch {}
    }
  }
  return $false
}

if (-not (Resolve-Python)) {
  Err "Python not found."
  Info "Install Python 3.9+ from https://www.python.org/downloads/ (check 'Add to PATH'),"
  Info "or run:  winget install Python.Python.3.12"
  Read-Host "`nPress Enter to exit"; exit 1
}
$pyver = & ($py.Split(' ')[0]) ($py.Split(' ')[1..9]) --version 2>&1
Ok "Found $pyver"
Info "interpreter (windowed): $pyw"

# --- 2. install dependencies ---------------------------------------------
Write-Host ""
Info "Installing dependencies (pystray, Pillow)..."
$pa = $py.Split(' ')
& $pa[0] $pa[1..($pa.Length-1)] -m pip install --quiet --user -r (Join-Path $Dir 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
  Warn "User install failed, retrying without --user..."
  & $pa[0] $pa[1..($pa.Length-1)] -m pip install --quiet -r (Join-Path $Dir 'requirements.txt')
}
if ($LASTEXITCODE -ne 0) { Err "Dependency install failed."; Read-Host "Press Enter"; exit 1 }
Ok "Dependencies ready"

# --- 3. verify it can read Kiro usage ------------------------------------
Write-Host ""
Info "Checking Kiro usage data..."
$check = & $pa[0] $pa[1..($pa.Length-1)] (Join-Path $Dir 'kiro_usage_widget.py') --selftest 2>&1
if ($LASTEXITCODE -eq 0) {
  Ok "Kiro usage detected -> $check"
} else {
  Warn "Couldn't read Kiro usage yet:"
  Info ($check | Out-String).Trim()
  Info "Make sure Kiro is installed and you've signed in. The widget will keep"
  Info "retrying once it's running."
}

# --- 4. autostart shortcut -----------------------------------------------
Write-Host ""
Info "Registering autostart (runs on login)..."
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'KiroUsageWidget.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath       = $pyw
$s.Arguments        = '"' + (Join-Path $Dir 'kiro_usage_widget.py') + '"'
$s.WorkingDirectory = $Dir
$s.WindowStyle      = 7
$s.Description       = 'Kiro Pro+ usage tray widget'
$s.Save()
Ok "Autostart shortcut created"

# --- 5. launch now --------------------------------------------------------
Write-Host ""
Info "Launching widget..."
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*kiro_usage_widget*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Process -FilePath $pyw -ArgumentList ('"' + (Join-Path $Dir 'kiro_usage_widget.py') + '"') -WorkingDirectory $Dir
Start-Sleep -Seconds 3

# --- 6. promote tray icon to the taskbar (always visible) ----------------
Info "Pinning tray icon to the taskbar..."
try {
  $base = 'HKCU:\Control Panel\NotifyIconSettings'
  $found = $false
  if (Test-Path $base) {
    Get-ChildItem $base | ForEach-Object {
      $p = Get-ItemProperty $_.PSPath
      if ($p.ExecutablePath -and ($p.ExecutablePath -like '*pythonw*') -and
          ($p.InitialTooltip -like '*Kiro*' -or $p.InitialTooltip -eq $null)) {
        Set-ItemProperty $_.PSPath -Name 'IsPromoted' -Value 1 -Type DWord
        $found = $true
      }
    }
  }
  if ($found) { Ok "Tray icon promoted (restart Explorer or re-login to apply)" }
  else { Warn "Couldn't auto-pin yet. Right-click taskbar > Taskbar settings >"
         Info "'Other system tray icons' and turn on the Kiro widget." }
} catch { Warn "Skipped taskbar pin: $_" }

Write-Host ""
Ok "All set. Look for the gauge icon in your taskbar's tray."
Info "Re-run this script anytime to repair the setup."
Write-Host ""
Read-Host "Press Enter to finish"

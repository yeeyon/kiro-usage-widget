#!/usr/bin/env bash
# ============================================================
#  Kiro Usage Widget - setup / onboarding (macOS + Linux)
#  - finds Python 3
#  - installs dependencies
#  - verifies it can read your Kiro usage
#  - registers autostart (LaunchAgent on macOS)
#  - launches the widget
# ============================================================
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "\033[32m[ok]\033[0m %s\n" "$1"; }
warn() { printf "\033[33m[!]\033[0m %s\n" "$1"; }
err()  { printf "\033[31m[x]\033[0m %s\n" "$1"; }

echo
bold "Kiro Usage Widget - setup"
echo "-------------------------"

# --- 1. find python -------------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; assert sys.version_info[:2] >= (3,9)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  err "Python 3.9+ not found."
  case "$(uname -s)" in
    Darwin) echo "  Install with:  brew install python   (or https://www.python.org/downloads/)";;
    *)      echo "  Install with your package manager, e.g.  sudo apt install python3 python3-pip python3-tk";;
  esac
  exit 1
fi
ok "Found $($PY --version 2>&1)"

# --- 2. dependencies ------------------------------------------------------
echo
echo "  Installing dependencies..."
"$PY" -m pip install --quiet --user -r "$DIR/requirements.txt" \
  || "$PY" -m pip install --quiet --break-system-packages -r "$DIR/requirements.txt"
ok "Dependencies ready"

# --- 3. verify usage read -------------------------------------------------
echo
echo "  Checking Kiro usage data..."
if OUT="$("$PY" "$DIR/kiro_usage_widget.py" --selftest 2>&1)"; then
  ok "Kiro usage detected -> $OUT"
else
  warn "Couldn't read Kiro usage yet: $OUT"
  echo "  Make sure Kiro is installed and signed in. The widget will keep retrying."
fi

PYW="$(command -v "$PY")"
WIDGET="$DIR/kiro_usage_widget.py"

# --- 4. autostart ---------------------------------------------------------
echo
echo "  Registering autostart..."
case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/dev.kiro.usagewidget.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.kiro.usagewidget</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYW</string>
    <string>$WIDGET</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>WorkingDirectory</key><string>$DIR</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST" 2>/dev/null || true
    ok "LaunchAgent installed (runs on login + now)"
    ;;
  *)
    # Linux: XDG autostart .desktop entry
    AUTO="$HOME/.config/autostart"
    mkdir -p "$AUTO"
    cat > "$AUTO/kiro-usage-widget.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Kiro Usage Widget
Exec=$PYW $WIDGET
X-GNOME-Autostart-enabled=true
EOF
    ok "Autostart entry installed"
    # start it now (background)
    nohup "$PYW" "$WIDGET" >/dev/null 2>&1 &
    ;;
esac

echo
ok "All set. Look for the gauge icon in your menu bar / tray."
echo "  Re-run this script anytime to repair the setup."
echo "  To remove:  bash uninstall.sh"


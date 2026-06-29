#!/usr/bin/env bash
# Remove the Kiro Usage Widget (macOS + Linux).
set -e
echo "Stopping widget..."
pkill -f kiro_usage_widget.py 2>/dev/null || true

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/dev.kiro.usagewidget.plist"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed LaunchAgent."
    ;;
  *)
    rm -f "$HOME/.config/autostart/kiro-usage-widget.desktop"
    echo "Removed autostart entry."
    ;;
esac
echo "Done. You can delete this folder now."

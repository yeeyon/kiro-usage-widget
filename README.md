# Kiro Usage Widget

A tiny Windows system-tray widget that shows your **Kiro Pro+ credit usage** as a
live gauge and pops a clean dialog when you cross the **50%** and **90%** marks —
so you never get surprised by overage charges.

- 🎯 Live gauge in the taskbar — color shifts green → amber → red as credits burn
- 🔔 One alert per threshold per billing cycle (no nagging)
- 🪟 Borderless dark popup, dismiss with OK / Esc / Enter
- 🔄 Auto-syncs from Kiro's own local data — no login, no API keys, no network calls
- 🚀 Runs in the background and starts on login

> Reads the same numbers shown in Kiro's status bar, straight from Kiro's local
> SQLite cache on your machine. Nothing is sent anywhere.

---

## Install (clone & run)

```bash
git clone https://github.com/yeeyon/kiro-usage-widget.git
cd kiro-usage-widget
```

Then **double-click `setup.bat`** (or run it from a terminal).

That's it. The setup will:
1. Find Python on your machine (or tell you how to install it)
2. Install dependencies (`pystray`, `Pillow`)
3. Verify it can read your Kiro usage
4. Register autostart so it runs on every login
5. Pin the tray icon to the taskbar and launch it

### Requirements
- Windows 10 / 11
- [Kiro](https://kiro.dev) installed and signed in (Pro / Pro+ / etc.)
- Python 3.9+ — get it from [python.org](https://www.python.org/downloads/)
  (tick *Add Python to PATH*) or `winget install Python.Python.3.12`

---

## Usage

- **Hover** the tray icon → exact credits + reset date
- **Right-click** → *Usage details* (full breakdown), *Check now*, *Quit*
- Alerts fire automatically at 50% and 90%

### Make the icon always visible on the taskbar
Windows hides new tray icons by default. Setup tries to pin it automatically;
if it's still hidden: **right-click taskbar → Taskbar settings → Other system
tray icons →** turn on the Kiro widget. (Or sign out / back in once.)

---

## Configure

Edit the top of `kiro_usage_widget.py`:

```python
THRESHOLDS   = [50, 90]   # percent marks that trigger an alert
POLL_SECONDS = 30         # how often it re-reads usage
```

Restart the widget after changes (right-click → Quit, then run `start_widget.bat`).

---

## Uninstall

Double-click **`uninstall.bat`** — stops the widget and removes the autostart
entry. Then delete the folder.

---

## How it works

Kiro stores your usage locally at:

```
%APPDATA%\Kiro\User\globalStorage\state.vscdb   (SQLite)
key: kiro.kiroAgent  →  kiro.resourceNotifications.usageState
```

The widget opens that file **read-only** every `POLL_SECONDS`, parses the
`currentUsage` / `usageLimit` fields, and renders the gauge. It never writes to
Kiro's data and never makes network requests.

> Numbers are as fresh as Kiro last wrote them. If Kiro is closed for a while,
> the figure can lag until Kiro next refreshes it.

---

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Kiro or AWS.

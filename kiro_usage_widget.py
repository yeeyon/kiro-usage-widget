"""
Kiro Usage Widget
-----------------
Cross-platform system-tray / menu-bar widget that watches Kiro Pro+ credit
usage, shows a live gauge, and alerts when usage crosses the 50% and 90% marks.

Data source (live, same numbers as the IDE status bar) — Kiro's local SQLite:
  Windows : %APPDATA%\\Kiro\\User\\globalStorage\\state.vscdb
  macOS   : ~/Library/Application Support/Kiro/User/globalStorage/state.vscdb
  Linux   : ~/.config/Kiro/User/globalStorage/state.vscdb
  key 'kiro.kiroAgent' -> kiro.resourceNotifications.usageState

Override the path for testing with the KIRO_DB_PATH environment variable.
"""

import os
import sys
import json
import math
import time
import shlex
import queue
import sqlite3
import threading
import subprocess

from PIL import Image, ImageDraw

# pystray is imported lazily (see _pystray) — importing it eagerly tries to
# open an X display, which fails on headless Linux/CI. The core (usage read +
# gauge render) must work without any display.
def _pystray():
    import pystray
    return pystray

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Tkinter is only used for the Windows dialog; import lazily so the module
# loads on headless/macOS CI where the custom Tk dialog isn't used.
if IS_WIN:
    import tkinter as tk
    from tkinter import font as tkfont
    from PIL import ImageTk

# ---------------------------------------------------------------- paths
def default_db_path():
    """Locate Kiro's usage SQLite DB for the current OS.
    KIRO_DB_PATH overrides everything (used by tests/CI)."""
    override = os.environ.get("KIRO_DB_PATH")
    if override:
        return override
    if IS_WIN:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:  # linux / other
        base = os.environ.get(
            "XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "Kiro", "User", "globalStorage", "state.vscdb")

DB_PATH = default_db_path()
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "alert_state.json")

THRESHOLDS = [50, 90]
POLL_SECONDS = 30

# palette
BG = "#16181d"
CARD = "#1e2129"
TXT = "#e7e9ee"
MUTED = "#9aa3b2"
EMERALD = "#10b981"
AMBER = "#f59e0b"
ROSE = "#f43f5e"

def zone_color(pct):
    return ROSE if pct >= 90 else AMBER if pct >= 50 else EMERALD

# ---------------------------------------------------------------- usage read
def read_usage(db_path=None):
    """Read live usage. Opens read-only (NOT immutable) so Kiro's ongoing
    writes / WAL are always reflected."""
    path = db_path or DB_PATH
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        con.execute("PRAGMA busy_timeout=3000")
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key='kiro.kiroAgent';"
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    state = json.loads(row[0]).get("kiro.resourceNotifications.usageState", {})
    bd = state.get("usageBreakdowns", [])
    if not bd:
        return None
    b = bd[0]
    used = float(b.get("currentUsage", 0))
    limit = float(b.get("usageLimit", 0)) or 1
    pct = b.get("percentageUsed")
    pct = float(pct) if pct is not None else used / limit * 100
    return {
        "used": used,
        "limit": limit,
        "pct": pct,
        "reset": (b.get("resetDate") or "")[:10],
        "overages": b.get("currentOverages", 0),
    }

# ---------------------------------------------------------------- alert state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cycle": "", "fired": []}

def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        pass

# ---------------------------------------------------------------- gauge image
def make_gauge(pct, S, width_frac=0.16, pad_frac=0.13, track="#ffffff", track_a=95):
    """Ring-gauge RGBA image at size S. Used both for the tray icon and dialog."""
    pct = max(0.0, min(100.0, float(pct)))
    SS = S * 4
    pad = int(SS * pad_frac)
    width = int(SS * width_frac)
    box = [pad, pad, SS - pad, SS - pad]
    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = tuple(int(zone_color(pct)[i:i + 2], 16) for i in (1, 3, 5))
    cx = cy = SS / 2.0
    r = (box[2] - box[0]) / 2.0
    cap = width / 2.0
    tr = tuple(int(track[i:i + 2], 16) for i in (1, 3, 5))
    d.arc(box, 0, 360, fill=tr + (track_a,), width=width)
    sweep = pct / 100.0 * 360.0
    if sweep > 0:
        d.arc(box, -90, -90 + sweep, fill=accent + (255,), width=width)
        for ang in (-90, -90 + sweep):
            a = math.radians(ang)
            px, py = cx + r * math.cos(a), cy + r * math.sin(a)
            d.ellipse([px - cap, py - cap, px + cap, py + cap], fill=accent + (255,))
    return img.resize((S, S), Image.LANCZOS)

def make_icon(pct):
    return make_gauge(pct, 64)

# ---------------------------------------------------------------- macOS alerts
def _osascript(script):
    """Run an AppleScript snippet, detached, so it never blocks polling."""
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def mac_notify(title, text):
    """Passive Notification Center banner."""
    q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    _osascript(f'display notification "{q(text)}" with title "{q(title)}"')

def mac_dialog(title, text):
    """Modal native dialog the user must dismiss (no auto-timeout)."""
    q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    _osascript(
        f'display dialog "{q(text)}" with title "{q(title)}" '
        f'buttons {{"OK"}} default button "OK" with icon note giving up after 0'
    )

def usage_detail_text(u):
    return (f"Used:       {u['used']:.2f}\n"
            f"Limit:      {u['limit']:.0f}\n"
            f"Remaining:  {u['limit'] - u['used']:.2f}\n"
            f"Percent:    {u['pct']:.1f}%\n"
            f"Overages:   {u['overages']}\n"
            f"Resets:     {u['reset']}")

# ---------------------------------------------------------------- Windows dialog
class Dialog(tk.Toplevel if IS_WIN else object):
    """Borderless dark card matching the gauge aesthetic (Windows only)."""
    def __init__(self, master, usage, heading, sub, accent):
        super().__init__(master)
        self.withdraw()
        self.configure(bg=BG)
        self.resizable(False, False)
        self.overrideredirect(True)          # no title bar / window chrome
        self.attributes("-topmost", True)

        f_head = tkfont.Font(family="Segoe UI Semibold", size=14)
        f_sub = tkfont.Font(family="Segoe UI", size=9)
        f_lbl = tkfont.Font(family="Segoe UI", size=10)
        f_val = tkfont.Font(family="Segoe UI Semibold", size=10)
        f_big = tkfont.Font(family="Segoe UI", size=24, weight="bold")

        # subtle 1px border so the borderless window has a clean edge
        border = tk.Frame(self, bg="#2c313c")
        border.pack(fill="both", expand=True)
        root = tk.Frame(border, bg=BG)
        root.pack(fill="both", expand=True, padx=1, pady=1)

        wrap = tk.Frame(root, bg=BG, padx=24, pady=22)
        wrap.pack(fill="both", expand=True)

        # left: slim gauge with % in the middle
        self._g = ImageTk.PhotoImage(
            make_gauge(usage["pct"], 116, width_frac=0.085, track_a=32))
        cv = tk.Canvas(wrap, width=116, height=116, bg=BG, highlightthickness=0)
        cv.grid(row=0, column=0, rowspan=2, padx=(0, 22), sticky="n")
        cv.create_image(58, 58, image=self._g)
        cv.create_text(58, 54, text=f"{usage['pct']:.0f}%", fill=TXT, font=f_big)
        cv.create_text(58, 78, text="used", fill=MUTED, font=f_sub)

        tk.Label(wrap, text=heading, bg=BG, fg=accent, font=f_head,
                 anchor="w", justify="left").grid(row=0, column=1, sticky="sw")
        tk.Label(wrap, text=sub, bg=BG, fg=MUTED, font=f_sub, anchor="w",
                 justify="left", wraplength=230).grid(row=1, column=1, sticky="nw",
                                                      pady=(2, 0))

        # stats card
        card = tk.Frame(root, bg=CARD)
        card.pack(fill="x", padx=24)
        rows = [
            ("Used", f"{usage['used']:.2f}"),
            ("Limit", f"{usage['limit']:.0f}"),
            ("Remaining", f"{usage['limit'] - usage['used']:.2f}"),
            ("Overages", str(usage["overages"])),
            ("Resets", usage["reset"]),
        ]
        for i, (k, v) in enumerate(rows):
            tk.Label(card, text=k, bg=CARD, fg=MUTED, font=f_lbl, anchor="w",
                     padx=16, pady=5).grid(row=i, column=0, sticky="w")
            tk.Label(card, text=v, bg=CARD, fg=TXT, font=f_val, anchor="e",
                     padx=16, pady=5).grid(row=i, column=1, sticky="e")
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=1)

        btnrow = tk.Frame(root, bg=BG, padx=24, pady=18)
        btnrow.pack(fill="x")
        btn = tk.Button(btnrow, text="OK", command=self._close, bg=accent,
                        fg="#0b0d10", activebackground=accent,
                        activeforeground="#0b0d10", relief="flat",
                        font=tkfont.Font(family="Segoe UI Semibold", size=10),
                        cursor="hand2", padx=30, pady=6, bd=0)
        btn.pack(side="right")

        # let the borderless card be dragged by its body
        for w in (wrap, root):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._drag)

        self.bind("<Return>", lambda e: self._close())
        self.bind("<Escape>", lambda e: self._close())

        self.update_idletasks()
        self._center()
        self.deiconify()
        self.lift()
        self.focus_force()
        btn.focus_set()

    def _press(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag(self, e):
        self.geometry(f"+{self.winfo_x() + e.x - self._dx}"
                      f"+{self.winfo_y() + e.y - self._dy}")

    def _center(self):
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw - w - 24}+{sh - h - 70}")

    def _close(self):
        self.destroy()

# ---------------------------------------------------------------- shared logic
def check_thresholds(u, on_fire):
    """Fire-once-per-cycle threshold logic, shared by both platforms.
    Calls on_fire(threshold) for each newly crossed mark."""
    st = load_state()
    if st.get("cycle") != u["reset"]:
        st = {"cycle": u["reset"], "fired": []}
    for t in THRESHOLDS:
        if u["pct"] >= t and t not in st["fired"]:
            st["fired"].append(t)
            save_state(st)
            on_fire(t)
    save_state(st)

def tooltip(u):
    if not u:
        return "Kiro usage: DB not found"
    return (f"Kiro Pro+  {u['pct']:.1f}%\n"
            f"{u['used']:.2f} / {u['limit']:.0f} credits\n"
            f"resets {u['reset']}")

# ---------------------------------------------------------------- Windows app
class WinApp:
    """Windows: pystray detached + Tk main thread for the custom gauge dialog."""
    def __init__(self):
        self.latest = None
        self.ui_q = queue.Queue()          # poll thread -> main(Tk) thread
        self.root = tk.Tk()
        self.root.withdraw()                # hidden root; only dialogs show
        pystray = _pystray()
        self.icon = pystray.Icon(
            "kiro_usage",
            icon=make_icon(0),
            title="Kiro usage: loading...",
            menu=pystray.Menu(
                pystray.MenuItem("Usage details", self._menu_details, default=True),
                pystray.MenuItem("Check now", self._menu_check),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._menu_quit),
            ),
        )

    def _menu_details(self, icon, item): self.ui_q.put(("details", None))
    def _menu_check(self, icon, item):
        self.poll(force=True); self.ui_q.put(("details", None))
    def _menu_quit(self, icon, item): self.ui_q.put(("quit", None))

    def poll(self, force=False):
        try:
            u = read_usage()
        except Exception:
            self.icon.title = "Kiro usage: read error"
            return
        self.latest = u
        if not u:
            self.icon.title = tooltip(u)
            return
        self.icon.icon = make_icon(u["pct"])
        self.icon.title = tooltip(u)
        check_thresholds(u, lambda t: self.ui_q.put(("alert", t)))

    def loop(self):
        time.sleep(1.5)
        while True:
            self.poll()
            time.sleep(POLL_SECONDS)

    def pump(self):
        try:
            while True:
                kind, arg = self.ui_q.get_nowait()
                if kind == "quit":
                    self.icon.stop(); self.root.quit(); return
                elif kind == "details":
                    self.show_details()
                elif kind == "alert":
                    self.show_alert(arg)
        except queue.Empty:
            pass
        self.root.after(200, self.pump)

    def show_details(self):
        u = self.latest
        if u:
            Dialog(self.root, u, "Kiro Pro+ usage",
                   "Live credit usage for this billing cycle.",
                   zone_color(u["pct"]))

    def show_alert(self, t):
        u = self.latest
        if not u:
            return
        danger = t >= 90
        Dialog(self.root, u,
               f"You've hit {t}%" + (" — almost out!" if danger else ""),
               (f"You've used {u['pct']:.1f}% of your Kiro Pro+ credits. "
                f"Credits reset on {u['reset']}."),
               ROSE if danger else AMBER)

    def run(self):
        if hasattr(self.icon, "run_detached"):
            self.icon.run_detached()
        else:
            threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self.loop, daemon=True).start()
        self.root.after(200, self.pump)
        self.root.mainloop()

# ---------------------------------------------------------------- macOS app
class MacApp:
    """macOS: pystray must own the MAIN thread (AppKit). Polling runs on a
    daemon thread; alerts/details use native osascript dialogs."""
    def __init__(self):
        self.latest = None
        pystray = _pystray()
        self.icon = pystray.Icon(
            "kiro_usage",
            icon=make_icon(0),
            title="Kiro usage: loading...",
            menu=pystray.Menu(
                pystray.MenuItem("Usage details", self._menu_details, default=True),
                pystray.MenuItem("Check now", self._menu_check),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._menu_quit),
            ),
        )

    def _menu_details(self, icon, item): self._show_details()
    def _menu_check(self, icon, item):
        self.poll(force=True); self._show_details()
    def _menu_quit(self, icon, item): self.icon.stop()

    def _show_details(self):
        u = self.latest
        if u:
            mac_dialog("Kiro Pro+ usage", usage_detail_text(u))

    def poll(self, force=False):
        try:
            u = read_usage()
        except Exception:
            self.icon.title = "Kiro usage: read error"
            return
        self.latest = u
        if not u:
            self.icon.title = tooltip(u)
            return
        self.icon.icon = make_icon(u["pct"])
        self.icon.title = tooltip(u)
        check_thresholds(u, self._alert)

    def _alert(self, t):
        u = self.latest
        danger = t >= 90
        head = f"Kiro usage at {t}%" + (" — almost out!" if danger else "")
        body = (f"You've used {u['pct']:.1f}% of your Kiro Pro+ credits. "
                f"Resets {u['reset']}.")
        mac_notify(head, body)   # passive banner
        mac_dialog(head, body)   # modal, must dismiss

    def loop(self):
        time.sleep(1.5)
        while True:
            self.poll()
            time.sleep(POLL_SECONDS)

    def run(self):
        threading.Thread(target=self.loop, daemon=True).start()
        self.icon.run()          # MUST be on the main thread on macOS

# ---------------------------------------------------------------- entrypoint
def run_selftest():
    if not os.path.exists(DB_PATH):
        print(f"Kiro DB not found at {DB_PATH}"); return 1
    try:
        u = read_usage()
    except Exception as e:
        print(f"read error: {e}"); return 1
    if not u:
        print("usage data not present yet (sign in to Kiro)"); return 1
    print(f"{u['used']:.2f}/{u['limit']:.0f} credits ({u['pct']:.1f}%)")
    return 0

def run_screenshot(outdir):
    """Render the tray icon + a dialog mock to PNGs (used by CI artifacts)."""
    os.makedirs(outdir, exist_ok=True)
    for p in (14, 65, 95):
        make_icon(p).save(os.path.join(outdir, f"icon_{p}.png"))
    # gauge-states strip
    strip = Image.new("RGBA", (460, 150), (24, 24, 28, 255))
    x = 80
    for p in (14, 65, 95):
        g = make_gauge(p, 72)
        strip.alpha_composite(g, (x - 36, 24)); x += 150
    strip.save(os.path.join(outdir, "states.png"))
    print(f"screenshots written to {outdir}")
    return 0

def main():
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    if "--screenshot" in sys.argv:
        i = sys.argv.index("--screenshot")
        outdir = sys.argv[i + 1] if len(sys.argv) > i + 1 else "docs"
        sys.exit(run_screenshot(outdir))

    if not os.path.exists(DB_PATH):
        msg = f"Kiro usage DB not found:\n{DB_PATH}"
        if IS_WIN:
            r = tk.Tk(); r.withdraw()
            from tkinter import messagebox
            messagebox.showerror("Kiro usage widget", msg)
        elif IS_MAC:
            mac_dialog("Kiro usage widget", msg)
        else:
            print(msg)
        sys.exit(1)

    if IS_MAC:
        MacApp().run()
    else:
        WinApp().run()


if __name__ == "__main__":
    main()

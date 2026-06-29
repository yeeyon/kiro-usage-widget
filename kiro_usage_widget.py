"""
Kiro Usage Widget
-----------------
System-tray widget that watches Kiro Pro+ credit usage, shows a live gauge in
the taskbar, and pops a custom dialog when usage crosses the 50% and 90% marks.

Data source (live, same numbers as the IDE status bar):
  %APPDATA%\\Kiro\\User\\globalStorage\\state.vscdb  (SQLite)
  key 'kiro.kiroAgent' -> kiro.resourceNotifications.usageState
"""

import os
import sys
import json
import math
import time
import queue
import sqlite3
import threading

import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk
import pystray

# ---------------------------------------------------------------- config
APPDATA = os.environ.get("APPDATA", "")
DB_PATH = os.path.join(APPDATA, "Kiro", "User", "globalStorage", "state.vscdb")
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
def read_usage():
    """Read live usage. Opens read-only (NOT immutable) so Kiro's ongoing
    writes / WAL are always reflected."""
    uri = f"file:{DB_PATH}?mode=ro"
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

# ---------------------------------------------------------------- custom dialog
class Dialog(tk.Toplevel):
    """Borderless dark card matching the gauge aesthetic (no OS title bar)."""
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

# ---------------------------------------------------------------- app
class Widget:
    def __init__(self):
        self.latest = None
        self.ui_q = queue.Queue()          # poll thread -> main(Tk) thread
        self.root = tk.Tk()
        self.root.withdraw()                # hidden root; only dialogs show

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

    # ---- tooltip
    def tooltip(self, u):
        if not u:
            return "Kiro usage: DB not found"
        return (f"Kiro Pro+  {u['pct']:.1f}%\n"
                f"{u['used']:.2f} / {u['limit']:.0f} credits\n"
                f"resets {u['reset']}")

    # ---- menu handlers (run in pystray thread -> marshal to Tk thread)
    def _menu_details(self, icon, item):
        self.ui_q.put(("details", None))

    def _menu_check(self, icon, item):
        self.poll(force=True)
        self.ui_q.put(("details", None))

    def _menu_quit(self, icon, item):
        self.ui_q.put(("quit", None))

    # ---- polling (background thread)
    def poll(self, force=False):
        try:
            u = read_usage()
        except Exception as e:
            self.icon.title = f"Kiro usage: read error"
            return
        self.latest = u
        if not u:
            self.icon.title = self.tooltip(u)
            return
        self.icon.icon = make_icon(u["pct"])
        self.icon.title = self.tooltip(u)

        st = load_state()
        if st.get("cycle") != u["reset"]:
            st = {"cycle": u["reset"], "fired": []}
        for t in THRESHOLDS:
            if u["pct"] >= t and t not in st["fired"]:
                st["fired"].append(t)
                save_state(st)
                self.ui_q.put(("alert", t))
        save_state(st)

    def loop(self):
        time.sleep(1.5)
        while True:
            self.poll()
            time.sleep(POLL_SECONDS)

    # ---- Tk-thread UI pump
    def pump(self):
        try:
            while True:
                kind, arg = self.ui_q.get_nowait()
                if kind == "quit":
                    self.icon.stop()
                    self.root.quit()
                    return
                elif kind == "details":
                    self.show_details()
                elif kind == "alert":
                    self.show_alert(arg)
        except queue.Empty:
            pass
        self.root.after(200, self.pump)

    def show_details(self):
        u = self.latest
        if not u:
            return
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


if __name__ == "__main__":
    # --selftest: used by setup to verify usage can be read, without launching
    # the tray. Prints a one-line summary and exits 0 on success, 1 on failure.
    if "--selftest" in sys.argv:
        if not os.path.exists(DB_PATH):
            print(f"Kiro DB not found at {DB_PATH}")
            sys.exit(1)
        try:
            u = read_usage()
        except Exception as e:
            print(f"read error: {e}")
            sys.exit(1)
        if not u:
            print("usage data not present yet (sign in to Kiro)")
            sys.exit(1)
        print(f"{u['used']:.2f}/{u['limit']:.0f} credits ({u['pct']:.1f}%)")
        sys.exit(0)

    if not os.path.exists(DB_PATH):
        r = tk.Tk(); r.withdraw()
        from tkinter import messagebox
        messagebox.showerror("Kiro usage widget",
                             f"Kiro usage DB not found:\n{DB_PATH}")
        sys.exit(1)
    Widget().run()

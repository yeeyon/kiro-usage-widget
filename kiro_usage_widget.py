"""
Kiro Usage Widget
-----------------
Cross-platform system-tray / menu-bar widget that watches Kiro Pro+ credit
usage, shows a live gauge, and alerts when usage crosses the 50% and 90% marks.

Data sources, in priority order:
  1. LIVE  — Kiro's backend GetUsageLimits API (the exact source the IDE status
     bar uses). Works even when the IDE isn't running, so the number never goes
     stale while you keep using Kiro. Uses the cached IdC/bearer token at
     ~/.aws/sso/cache/kiro-auth-token.json + the profileArn from
     <globalStorage>/kiro.kiroagent/profile.json.
  2. CACHED — Kiro's local SQLite (fallback when the API is unreachable / signed
     out). This only reflects what the IDE last wrote, so it can lag:
       Windows : %APPDATA%\\Kiro\\User\\globalStorage\\state.vscdb
       macOS   : ~/Library/Application Support/Kiro/User/globalStorage/state.vscdb
       Linux   : ~/.config/Kiro/User/globalStorage/state.vscdb
       key 'kiro.kiroAgent' -> kiro.resourceNotifications.usageState

Override the DB path for testing with KIRO_DB_PATH (this also forces the cached
path and skips the live call, keeping tests deterministic/offline). Set
KIRO_NO_LIVE=1 to disable the live API without touching the DB path.
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
import urllib.request
import urllib.error

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

def profile_json_path(db_path=None):
    """profile.json sits next to state.vscdb, under kiro.kiroagent/.
    Derived from the DB path so the KIRO_DB_PATH override keeps tests offline."""
    gs = os.path.dirname(db_path or DB_PATH)  # ...\\User\\globalStorage
    return os.path.join(gs, "kiro.kiroagent", "profile.json")

def auth_token_path():
    """Kiro's cached IdC/bearer token (AWS SSO cache, in the user home)."""
    override = os.environ.get("KIRO_TOKEN_PATH")
    if override:
        return override
    return os.path.expanduser(
        os.path.join("~", ".aws", "sso", "cache", "kiro-auth-token.json"))

DB_PATH = default_db_path()
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "alert_state.json")

THRESHOLDS = [50, 90]
POLL_SECONDS = 30
LIVE_TIMEOUT = 8          # seconds; keep small so a stall never freezes the UI

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
def _normalize(used, limit, reset, overages, source):
    limit = float(limit) or 1.0
    used = float(used)
    return {
        "used": used,
        "limit": limit,
        "pct": used / limit * 100.0,
        "reset": reset,
        "overages": overages,
        "source": source,            # "live" or "cached"
    }

def read_usage_cached(db_path=None):
    """Read Kiro's last-written usage from local SQLite. Opens read-only (NOT
    immutable) so any IDE writes / WAL are reflected. This can lag reality when
    the IDE isn't actively refreshing it — see read_usage_live."""
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
    return _normalize(
        used=b.get("currentUsage", 0),
        limit=b.get("usageLimit", 0),
        reset=(b.get("resetDate") or "")[:10],
        overages=b.get("currentOverages", 0),
        source="cached",
    )

def _read_profile_arn(db_path=None):
    """profileArn (encodes the account + region) from Kiro's profile.json."""
    try:
        with open(profile_json_path(db_path), encoding="utf-8") as f:
            return json.load(f).get("arn")
    except Exception:
        return None

def read_usage_live(db_path=None):
    """Read live usage straight from Kiro's backend — the same GetUsageLimits
    call the IDE status bar makes. Independent of the IDE process, so the number
    stays correct while you keep burning credits with the app closed.

    Returns a usage dict, or None if we can't (signed out, token expired, offline).
    """
    arn = _read_profile_arn(db_path)
    if not arn:
        return None
    try:
        region = arn.split(":")[3] or "us-east-1"
    except IndexError:
        return None

    # bearer token (re-read every call: any running Kiro process keeps it fresh)
    try:
        with open(auth_token_path(), encoding="utf-8") as f:
            tok = json.load(f)
        access = tok["accessToken"]
    except Exception:
        return None

    body = json.dumps({"profileArn": arn, "origin": "AI_EDITOR"}).encode()
    req = urllib.request.Request(
        f"https://q.{region}.amazonaws.com/", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + access)
    req.add_header("X-Amz-Target", "AmazonCodeWhispererService.GetUsageLimits")
    req.add_header("Content-Type", "application/x-amz-json-1.0")
    try:
        with urllib.request.urlopen(req, timeout=LIVE_TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None  # offline / 4xx / token issue -> caller falls back to cache

    bd = data.get("usageBreakdownList") or []
    if not bd:
        return None
    b = bd[0]
    epoch = b.get("nextDateReset") or data.get("nextDateReset")
    reset = (time.strftime("%Y-%m-%d", time.gmtime(epoch)) if epoch else "")
    return _normalize(
        used=b.get("currentUsageWithPrecision", b.get("currentUsage", 0)),
        limit=b.get("usageLimitWithPrecision", b.get("usageLimit", 0)),
        reset=reset,
        overages=b.get("currentOveragesWithPrecision", b.get("currentOverages", 0)),
        source="live",
    )

def read_usage(db_path=None):
    """Live first (the IDE-independent backend value), cached SQLite as fallback.

    The live call is skipped when a DB path is forced (KIRO_DB_PATH / explicit
    arg) or KIRO_NO_LIVE is set, so tests stay deterministic and offline."""
    forced_db = db_path is not None or bool(os.environ.get("KIRO_DB_PATH"))
    if not forced_db and not os.environ.get("KIRO_NO_LIVE"):
        u = read_usage_live(db_path)
        if u:
            return u
    return read_usage_cached(db_path)

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

def mac_dialog_buttons(title, text, buttons, default):
    """Blocking native dialog with custom buttons. Returns the clicked button
    label, or None if dismissed/errored. Runs osascript synchronously, so the
    caller must be off the AppKit main thread."""
    q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    btns = "{" + ", ".join(f'"{q(b)}"' for b in buttons) + "}"
    script = (
        f'display dialog "{q(text)}" with title "{q(title)}" '
        f'buttons {btns} default button "{q(default)}" with icon note'
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except Exception:
        return None
    if out.returncode != 0:        # user pressed Esc / closed the dialog
        return None
    # osascript prints e.g. "button returned:Refresh"
    line = out.stdout.strip()
    if "button returned:" in line:
        return line.split("button returned:", 1)[1].strip()
    return None

def usage_detail_text(u, updated=""):
    lines = [f"Used:       {u['used']:.2f}",
             f"Limit:      {u['limit']:.0f}",
             f"Remaining:  {u['limit'] - u['used']:.2f}",
             f"Percent:    {u['pct']:.1f}%",
             f"Overages:   {u['overages']}",
             f"Resets:     {u['reset']}"]
    if updated:
        lines.append(f"\nUpdated {updated}")
    return "\n".join(lines)

# ---------------------------------------------------------------- Windows dialog
class Dialog(tk.Toplevel if IS_WIN else object):
    """Borderless dark card matching the gauge aesthetic (Windows only)."""
    def __init__(self, master, usage, heading, sub, accent,
                 updated="", on_refresh=None):
        super().__init__(master)
        self.withdraw()
        self._on_refresh = on_refresh
        self.configure(bg=BG)
        self.resizable(False, False)
        self.overrideredirect(True)          # no title bar / window chrome
        self.attributes("-topmost", True)

        self._f_sub = tkfont.Font(family="Segoe UI", size=9)
        f_head = tkfont.Font(family="Segoe UI Semibold", size=14)
        f_lbl = tkfont.Font(family="Segoe UI", size=10)
        f_val = tkfont.Font(family="Segoe UI Semibold", size=10)
        f_big = tkfont.Font(family="Segoe UI", size=24, weight="bold")
        f_btn = tkfont.Font(family="Segoe UI Semibold", size=10)

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
        self._cv = tk.Canvas(wrap, width=116, height=116, bg=BG,
                             highlightthickness=0)
        self._cv.grid(row=0, column=0, rowspan=2, padx=(0, 22), sticky="n")
        self._img_id = self._cv.create_image(58, 58, image=self._g)
        self._pct_id = self._cv.create_text(58, 54, text=f"{usage['pct']:.0f}%",
                                            fill=TXT, font=f_big)
        self._cv.create_text(58, 78, text="used", fill=MUTED, font=self._f_sub)

        self._head = tk.Label(wrap, text=heading, bg=BG, fg=accent, font=f_head,
                              anchor="w", justify="left")
        self._head.grid(row=0, column=1, sticky="sw")
        tk.Label(wrap, text=sub, bg=BG, fg=MUTED, font=self._f_sub, anchor="w",
                 justify="left", wraplength=230).grid(row=1, column=1, sticky="nw",
                                                      pady=(2, 0))

        # stats card
        card = tk.Frame(root, bg=CARD)
        card.pack(fill="x", padx=24)
        self._labels = ("Used", "Limit", "Remaining", "Overages", "Resets")
        self._vals = {}
        for i, k in enumerate(self._labels):
            tk.Label(card, text=k, bg=CARD, fg=MUTED, font=f_lbl, anchor="w",
                     padx=16, pady=5).grid(row=i, column=0, sticky="w")
            v = tk.Label(card, bg=CARD, fg=TXT, font=f_val, anchor="e",
                         padx=16, pady=5)
            v.grid(row=i, column=1, sticky="e")
            self._vals[k] = v
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=1)

        btnrow = tk.Frame(root, bg=BG, padx=24, pady=18)
        btnrow.pack(fill="x")
        # left: last-updated stamp
        self._updated = tk.Label(btnrow, text="", bg=BG, fg=MUTED,
                                 font=self._f_sub, anchor="w")
        self._updated.pack(side="left")

        self._ok = tk.Button(btnrow, text="OK", command=self._close, bg=accent,
                            fg="#0b0d10", activebackground=accent,
                            activeforeground="#0b0d10", relief="flat",
                            font=f_btn, cursor="hand2", padx=30, pady=6, bd=0)
        self._ok.pack(side="right")
        if on_refresh is not None:
            self._refresh_btn = tk.Button(
                btnrow, text="Refresh", command=self._refresh, bg=CARD, fg=TXT,
                activebackground="#2c313c", activeforeground=TXT, relief="flat",
                font=f_btn, cursor="hand2", padx=18, pady=6, bd=0)
            self._refresh_btn.pack(side="right", padx=(0, 8))

        self._set_usage(usage, updated)

        # let the borderless card be dragged by its body
        for w in (wrap, root):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._drag)

        self.bind("<Return>", lambda e: self._close())
        self.bind("<Escape>", lambda e: self._close())
        self.bind("<F5>", lambda e: self._refresh())

        self.update_idletasks()
        self._center()
        self.deiconify()
        self.lift()
        self.focus_force()
        self._ok.focus_set()

    def _set_usage(self, usage, updated):
        """Populate gauge + stat values from a usage dict (used on open and
        on refresh, so the open dialog updates in place)."""
        accent = zone_color(usage["pct"])
        self._g = ImageTk.PhotoImage(
            make_gauge(usage["pct"], 116, width_frac=0.085, track_a=32))
        self._cv.itemconfigure(self._img_id, image=self._g)
        self._cv.itemconfigure(self._pct_id, text=f"{usage['pct']:.0f}%")
        self._head.configure(fg=accent)
        self._ok.configure(bg=accent, activebackground=accent)
        self._vals["Used"].configure(text=f"{usage['used']:.2f}")
        self._vals["Limit"].configure(text=f"{usage['limit']:.0f}")
        self._vals["Remaining"].configure(
            text=f"{usage['limit'] - usage['used']:.2f}")
        self._vals["Overages"].configure(text=str(usage["overages"]))
        self._vals["Resets"].configure(text=usage["reset"])
        self._updated.configure(text=f"Updated {updated}" if updated else "")

    def _refresh(self):
        if self._on_refresh is None:
            return
        self._updated.configure(text="Refreshing…")
        self.update_idletasks()
        usage, updated = self._on_refresh()
        if usage:
            self._set_usage(usage, updated)

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
    stale = "  (cached)" if u.get("source") == "cached" else ""
    return (f"Kiro Pro+  {u['pct']:.1f}%{stale}\n"
            f"{u['used']:.2f} / {u['limit']:.0f} credits\n"
            f"resets {u['reset']}")

# ---------------------------------------------------------------- Windows app
class WinApp:
    """Windows: pystray detached + Tk main thread for the custom gauge dialog."""
    def __init__(self):
        self.latest = None
        self.last_update = ""
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
        self.last_update = time.strftime("%H:%M:%S") + (
            " (cached)" if u.get("source") == "cached" else "")
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
                   zone_color(u["pct"]),
                   updated=self.last_update, on_refresh=self._refresh_now)

    def _refresh_now(self):
        """Called from the Refresh button (Tk main thread): re-read the DB
        and hand the fresh usage + stamp back to the open dialog."""
        self.poll(force=True)
        return self.latest, self.last_update

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
        self.last_update = ""
        self._details_open = False
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
        if self.latest is None or self._details_open:
            return
        # osascript blocks until dismissed; run it off the AppKit main thread
        # so the tray icon keeps polling. Refresh re-reads and reshows.
        self._details_open = True
        threading.Thread(target=self._details_loop, daemon=True).start()

    def _details_loop(self):
        try:
            while True:
                u = self.latest
                if not u:
                    return
                choice = mac_dialog_buttons(
                    "Kiro Pro+ usage",
                    usage_detail_text(u, self.last_update),
                    buttons=["Refresh", "OK"], default="OK")
                if choice == "Refresh":
                    self.poll(force=True)
                    continue
                return
        finally:
            self._details_open = False

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
        self.last_update = time.strftime("%H:%M:%S") + (
            " (cached)" if u.get("source") == "cached" else "")
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
    u = None
    try:
        u = read_usage()
    except Exception as e:
        print(f"read error: {e}")
    if not u:
        if not os.path.exists(DB_PATH):
            print(f"Kiro DB not found at {DB_PATH}")
        else:
            print("usage data not present yet (sign in to Kiro)")
        return 1
    src = u.get("source", "?")
    print(f"{u['used']:.2f}/{u['limit']:.0f} credits ({u['pct']:.1f}%)  [{src}]")
    if src == "cached":
        print("note: live API unavailable (signed out / offline) - showing last "
              "value the IDE wrote, which may be stale.")
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

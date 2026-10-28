#!/usr/bin/env python3
"""
Roblox Username Checker
-----------------------
- Check if usernames are available (Roblox's public username validation endpoint)
- Generate usernames (finite or INFINITE) and check them as they're made
- PRESETS: press [0] to create your own preset (same questions as Custom
  generate), then it shows up as [1], [2], ... and is saved to presets.json
- Custom generator: lowercase, UPPERCASE, numbers, underscore, pronounceable
- Generates a strong password for every available username
- ONE table that updates in place (never stacks up in your terminal)
- Press Ctrl+C to stop a run and go back to the menu
- Every available username is added to available_usernames.txt automatically
  (same folder as this script), so nothing is lost if you press Ctrl+C
- RECHECK option: re-checks every name in available_usernames.txt and removes
  the ones that are no longer available
- Score (0-100) for every name + summary
- ASCII art + loading bar on startup
- STATS: lifetime + per-day totals (generated, tried, available, taken...) saved
  to stats.json, viewable from the main menu
- CREATE ACCOUNT: pick a saved username with the arrow keys + Enter, then a
  browser opens and fills in the signup form (you solve the captcha yourself).
  A 2-3 minute cooldown applies after every attempt.

Install:  pip install requests rich
For account creation also:
          pip install undetected-chromedriver selenium setuptools certifi
Run:      python roblox_username_checker.py

Tip: run it in a real terminal (Command Prompt, PowerShell, Terminal, etc.).
Some IDE "run" panels can't redraw the screen or read arrow keys, so the tool
falls back to one plain line per username there and prints the table at the end.
"""

import json
import os
import random
import re
import secrets
import string
import sys
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime

import requests
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

console = Console()

API_URL = "https://auth.roblox.com/v1/usernames/validate"
MIN_LEN, MAX_LEN = 3, 20      # Roblox's real limits
MAX_ROWS_SHOWN = 18           # newest rows shown in the table
DELAY = 0.5                   # seconds between requests (be nice to the API)
SAVE_NAME = "available_usernames.txt"
PRESET_NAME = "presets.json"
# Save next to this script (not the terminal's current folder, which may be
# write-protected, e.g. C:\Windows\System32). Falls back to your home folder.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_FILE = os.path.join(SCRIPT_DIR, SAVE_NAME)
FALLBACK_FILE = os.path.join(os.path.expanduser("~"), SAVE_NAME)
PRESET_FILE = os.path.join(SCRIPT_DIR, PRESET_NAME)
STATS_NAME = "stats.json"
STATS_FILE = os.path.join(SCRIPT_DIR, STATS_NAME)
COOLDOWN_FILE = os.path.join(SCRIPT_DIR, "signup_cooldown.json")
COOLDOWN_RANGE = (120, 180)   # signup cooldown: 2 - 3 minutes (seconds)

# --------------------------------------------------------------------------- #
# ASCII art shown on startup. Paste yours between the triple quotes below.
# (It's a raw string, so backslashes are fine.)
# --------------------------------------------------------------------------- #
ASCII_ART = r"""
   ___        ___    ________  _______   ________      
 _|\  \__   _|\  \__|\   ____\|\  ___ \ |\   ___  \    
|\   ____\ |\   ____\ \  \___|\ \   __/|\ \  \\ \  \   
\ \  \___|_\ \  \___|\ \  \  __\ \  \_|/_\ \  \\ \  \  
 \ \_____  \\ \_____  \ \  \|\  \ \  \_|\ \ \  \\ \  \ 
  \|____|\  \\|____|\  \ \_______\ \_______\ \__\\ \__\
    ____\_\  \ ____\_\  \|_______|\|_______|\|__| \|__|
   |\___    __\\___    __\                             
   \|___|\__\_\|___|\__\_|                             
        \|__|      \|__|    
"""


# --------------------------------------------------------------------------- #
# Session state (kept small so infinite runs never eat memory)
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self):
        self.reset()

    def reset(self):
        self.total = 0
        self.counts = {"Available": 0, "Taken": 0, "Blocked": 0, "Invalid": 0, "Error": 0}
        self.recent = deque(maxlen=MAX_ROWS_SHOWN)
        self.seen = set()
        self.best_avail = None   # (score, name)
        self.best_taken = None
        self.saved = 0
        self.started = time.time()


S = Session()

# Generator settings for the custom generator (remembered between runs)
OPTS = {"lower": True, "upper": False, "digits": False, "underscore": False,
        "pronounceable": False}


# --------------------------------------------------------------------------- #
# Presets (your own, saved to presets.json)
# --------------------------------------------------------------------------- #
PRESETS = []   # each: {"name": str, "lo": int, "hi": int, "opts": {...}}


def describe_settings(lo, hi, opts):
    """Short human-readable summary of generator settings."""
    chars = []
    if opts["lower"]:
        chars.append("lowercase")
    if opts["upper"]:
        chars.append("CAPITALS")
    if opts["digits"]:
        chars.append("numbers")
    if opts["underscore"]:
        chars.append("underscore")
    style = "pronounceable" if opts["pronounceable"] else "random"
    length = f"{lo}" if lo == hi else f"{lo}-{hi}"
    return f"{style}, {', '.join(chars)}, {length} chars"


def load_presets():
    """Load presets.json into PRESETS. Returns how many were loaded."""
    PRESETS.clear()
    try:
        with open(PRESET_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for p in data:
            opts = {k: bool(p["opts"].get(k, False)) for k in OPTS}
            lo = max(MIN_LEN, min(MAX_LEN, int(p["lo"])))
            hi = max(lo, min(MAX_LEN, int(p["hi"])))
            PRESETS.append({"name": str(p["name"]), "lo": lo, "hi": hi, "opts": opts})
    except (OSError, ValueError, KeyError, TypeError):
        pass  # no file yet (or unreadable) -> start with no presets
    return len(PRESETS)


def save_presets():
    try:
        with open(PRESET_FILE, "w", encoding="utf-8") as f:
            json.dump(PRESETS, f, indent=2)
        return True
    except OSError as e:
        console.print(f"[bold red]Couldn't save presets to {PRESET_FILE}: {e}[/bold red]")
        return False


# --------------------------------------------------------------------------- #
# Lifetime stats (saved to stats.json, so they carry over between runs)
# --------------------------------------------------------------------------- #
STAT_KEYS = ("generated", "checked", "Available", "Taken", "Blocked", "Invalid", "Error")
STATS = {}


def _blank_counts():
    return {k: 0 for k in STAT_KEYS}


def _today():
    return time.strftime("%Y-%m-%d")


def _fresh_stats():
    return {"first_used": _today(), "sessions": 0, "total": _blank_counts(),
            "days": {}, "best_available": None}


def load_stats():
    """Load stats.json into STATS (starts fresh if it's missing or unreadable)."""
    STATS.clear()
    STATS.update(_fresh_stats())
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        STATS["first_used"] = str(data.get("first_used", STATS["first_used"]))
        STATS["sessions"] = int(data.get("sessions", 0))
        for k in STAT_KEYS:
            STATS["total"][k] = int(data.get("total", {}).get(k, 0))
        for day, counts in data.get("days", {}).items():
            STATS["days"][str(day)] = {k: int(counts.get(k, 0)) for k in STAT_KEYS}
        best = data.get("best_available")
        if isinstance(best, dict) and "name" in best and "score" in best:
            STATS["best_available"] = {"name": str(best["name"]), "score": int(best["score"])}
    except (OSError, ValueError, TypeError, AttributeError):
        pass


def save_stats():
    """Write stats safely (temp file first). Never interrupts a run if it fails."""
    tmp = STATS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATS, f, indent=2)
        os.replace(tmp, STATS_FILE)
    except OSError:
        pass


def bump_stats(*keys, save=True):
    """Add 1 to each key in both the all-time totals and today's row."""
    day = STATS["days"].setdefault(_today(), _blank_counts())
    for k in keys:
        STATS["total"][k] += 1
        day[k] += 1
    if save:
        save_stats()


# --------------------------------------------------------------------------- #
# Validation + checking
# --------------------------------------------------------------------------- #
def local_validate(name: str):
    """Roblox format rules, checked before bothering the API."""
    if not (MIN_LEN <= len(name) <= MAX_LEN):
        return f"Must be {MIN_LEN}-{MAX_LEN} characters"
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        return "Only letters, numbers and _ allowed"
    if name.count("_") > 1:
        return "Max one underscore"
    if name.startswith("_") or name.endswith("_"):
        return "Can't start/end with _"
    if name.isdigit():
        return "Can't be only numbers"
    return None


def check_username(name: str):
    """Returns (status, note). status: Available/Taken/Blocked/Invalid/Error."""
    problem = local_validate(name)
    if problem:
        return "Invalid", problem

    params = {
        "request.username": name,
        "request.birthday": "2000-01-01",
        "request.context": "Signup",
    }
    for attempt in range(4):
        try:
            r = requests.get(API_URL, params=params, timeout=10)
        except requests.RequestException as e:
            return "Error", type(e).__name__
        if r.status_code == 429:  # rate limited -> back off
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code != 200:
            return "Error", f"HTTP {r.status_code}"
        try:
            code = r.json().get("code")
        except ValueError:
            return "Error", "Bad response"
        if code == 0:
            return "Available", ""
        if code == 1:
            return "Taken", ""
        if code == 2:
            return "Blocked", "Not appropriate"
        if code == 3:
            return "Invalid", "Rejected by Roblox"
        return "Error", f"Code {code}"
    return "Error", "Rate limited"


# --------------------------------------------------------------------------- #
# Score
# --------------------------------------------------------------------------- #
def score_name(name: str) -> int:
    """Score 0-100. Shorter + cleaner names score higher."""
    n = len(name)
    base = {3: 100, 4: 96, 5: 88, 6: 78, 7: 66, 8: 56, 9: 46, 10: 38, 11: 31,
            12: 25, 13: 20, 14: 16, 15: 12, 16: 9, 17: 7, 18: 5, 19: 3, 20: 2}
    score = base.get(n, 0)
    if name.isalpha():
        score += 3                      # clean letters-only names are nicer
    score -= 3 * sum(c.isdigit() for c in name) // 2
    score -= 4 * name.count("_")
    if len(set(name.lower())) <= 2 and n <= 8:
        score += 5                      # repeating patterns like "xxxx"
    return max(0, min(100, score))


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #
CONS = "bcdfghjklmnprstvwxyz"
VOWS = "aeiou"


def _letters_pool(opts):
    pool = ""
    if opts["lower"]:
        pool += string.ascii_lowercase
    if opts["upper"]:
        pool += string.ascii_uppercase
    return pool or string.ascii_lowercase  # always at least some letters


def _pronounceable(length, opts):
    name = ""
    use_cons = random.random() < 0.7
    while len(name) < length:
        name += random.choice(CONS if use_cons else VOWS)
        use_cons = not use_cons
    if opts["upper"] and opts["lower"]:
        return name.capitalize()
    if opts["upper"]:
        return name.upper()
    return name


def generate_username(length: int, opts: dict):
    letters = _letters_pool(opts)
    if opts["pronounceable"]:
        name = _pronounceable(length, opts)
        if opts["digits"] and length >= 4 and random.random() < 0.5:
            k = random.randint(1, 2)  # tack numbers on the end, like "Zelo27"
            name = name[:-k] + "".join(random.choice(string.digits) for _ in range(k))
    else:
        pool = letters + (string.digits if opts["digits"] else "")
        name = random.choice(letters)  # always start with a letter
        name += "".join(random.choice(pool) for _ in range(length - 1))
    if opts["underscore"] and length >= 4 and random.random() < 0.5:
        i = random.randint(1, length - 2)
        name = name[:i] + "_" + name[i + 1:]
    return name


def generate_password(length: int = 16):
    """Cryptographically random password with all four character types."""
    symbols = "!@#$%^&*-_+="
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, symbols]
    pwd = [secrets.choice(p) for p in pools]
    allchars = "".join(pools)
    pwd += [secrets.choice(allchars) for _ in range(length - len(pwd))]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


# --------------------------------------------------------------------------- #
# Display (one table, updated in place)
# --------------------------------------------------------------------------- #
STATUS_STYLE = {
    "Available": "bold green",
    "Taken": "red",
    "Blocked": "yellow",
    "Invalid": "yellow",
    "Error": "bold red",
}


def clear_screen():
    """Clear the screen AND scrollback where the terminal supports it."""
    if not console.is_terminal:
        return
    os.system("cls" if os.name == "nt" else "clear")
    if os.name != "nt":
        console.file.write("\033[3J")
        console.file.flush()


def build_table():
    table = Table(box=box.ROUNDED, header_style="bold", expand=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Username", style="bold")
    table.add_column("Len", justify="right")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Password / Note")

    for r in S.recent:
        rated = r["status"] in ("Available", "Taken")
        extra = r["password"] if r["status"] == "Available" else r["note"]
        table.add_row(
            str(r["n"]), r["name"], str(len(r["name"])),
            f"[{STATUS_STYLE[r['status']]}]{r['status']}[/]",
            str(score_name(r["name"])) if rated else "-",
            Text(extra),  # Text() so symbols in passwords are never read as markup
        )
    if not S.recent:
        table.add_row("-", "No usernames checked yet", "", "", "", "")
    return table


def build_summary():
    c = S.counts
    other = S.total - c["Available"] - c["Taken"]
    lines = [
        f"Checked: [bold]{S.total}[/bold]   "
        f"Available: [green]{c['Available']}[/green]   "
        f"Taken: [red]{c['Taken']}[/red]   "
        f"Invalid/Blocked/Error: [yellow]{other}[/yellow]"
    ]
    if S.best_avail:
        score, name = S.best_avail
        lines.append(f"Best score available: [bold]{name}[/bold] (score {score})")
    if S.best_taken:
        score, name = S.best_taken
        lines.append(f"Best score taken: [bold]{name}[/bold] (score {score}) - already owned")
    if S.total:
        lines.append(f"Availability rate: {c['Available'] / S.total * 100:.1f}%")
    if S.saved:
        lines.append(f"[dim]{S.saved} available name(s) saved to {SAVE_FILE}[/dim]")
    return Panel("\n".join(lines), title="Summary", border_style="cyan", expand=False)


def build_view(message: str = ""):
    parts = [Text("Roblox Username Checker", style="bold cyan"), Text(""), build_table()]
    if S.total > len(S.recent):
        parts.append(Text(f"Showing latest {len(S.recent)} of {S.total} results "
                          f"(everything is still counted in the summary).", style="dim"))
    parts.append(build_summary())
    if message:
        parts.append(Text.from_markup(message))
    return Group(*parts)


# --------------------------------------------------------------------------- #
# Startup: ASCII art + loading bar (the steps really do run)
# --------------------------------------------------------------------------- #
def _step_modules():
    time.sleep(0.4)
    return "Modules loaded"


def _step_presets():
    n = load_presets()
    return f"{n} preset(s) loaded"


def _step_stats():
    load_stats()
    STATS["sessions"] += 1
    save_stats()
    return f"Stats loaded ({STATS['total']['checked']} tried all-time)"


def _step_save_location():
    global SAVE_FILE
    for path in (SAVE_FILE, FALLBACK_FILE):
        folder = os.path.dirname(path)
        if os.access(folder, os.W_OK):
            SAVE_FILE = path
            return "Save location ready"
    return "Save location not writable"


def _step_api():
    try:
        r = requests.get(API_URL, params={"request.username": "test",
                                          "request.birthday": "2000-01-01",
                                          "request.context": "Signup"}, timeout=5)
        if r.status_code in (200, 429):
            return "Roblox API online"
        return f"Roblox API replied HTTP {r.status_code}"
    except requests.RequestException:
        return "Roblox API unreachable"


def boot_sequence():
    clear_screen()
    art = ASCII_ART.strip("\n")
    if art:
        console.print(Text(art, style="bold cyan"))
        console.print()

    steps = [
        ("Loading modules", _step_modules),
        ("Loading presets", _step_presets),
        ("Loading stats", _step_stats),
        ("Checking save location", _step_save_location),
        ("Connecting to Roblox", _step_api),
    ]
    per_step = 100
    results = []
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description:<26}"),
        BarColumn(bar_width=40, complete_style="cyan", finished_style="green"),
        TaskProgressColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Starting up", total=per_step * len(steps))
        for label, fn in steps:
            progress.update(task, description=label)
            result = fn()                      # do the real work for this step
            results.append(result)
            for _ in range(10):                # then fill its slice of the bar smoothly
                progress.advance(task, per_step / 10)
                time.sleep(0.04)
        progress.update(task, description="Ready")

    console.print()
    for r in results:
        console.print(f"[dim]  - {escape(r)}[/dim]")
    time.sleep(0.8)


# --------------------------------------------------------------------------- #
# Core: check one name and record it
# --------------------------------------------------------------------------- #
def add_result(name: str):
    """Check a name and record it. Returns the row, or None if already checked."""
    key = name.lower()
    if key in S.seen:
        return None
    S.seen.add(key)

    status, note = check_username(name)
    password = generate_password() if status == "Available" else ""
    S.total += 1
    S.counts[status] += 1
    row = {"n": S.total, "name": name, "status": status, "note": note, "password": password}
    S.recent.append(row)
    bump_stats("checked", status, save=False)  # saved once at the end of this function

    if status in ("Available", "Taken"):
        score = score_name(name)
        entry = (score, name)
        if status == "Available":
            if S.best_avail is None or score > S.best_avail[0]:
                S.best_avail = entry
            best = STATS["best_available"]
            if best is None or score > best["score"]:
                STATS["best_available"] = {"name": name, "score": score}
            _save_available(name, password, score)
        elif S.best_taken is None or score > S.best_taken[0]:
            S.best_taken = entry
    save_stats()
    return row


_save_warned = False


def _save_available(name, password, score):
    """Append each available name to the .txt file the moment it's found."""
    global SAVE_FILE, _save_warned
    line = f"{name}:{password}  # score {score}\n"
    last_error = None
    for path in (SAVE_FILE, FALLBACK_FILE):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            SAVE_FILE = path  # remember where it actually went
            S.saved += 1
            return
        except OSError as e:
            last_error = e
    if not _save_warned:  # only warn once so it doesn't spam the table
        _save_warned = True
        console.print(f"[bold red]Can't write to {SAVE_NAME}: {last_error}[/bold red]")


def run_loop(names):
    """
    Check every name from the iterable `names` (can be infinite).
    One table, updated in place. Ctrl+C stops cleanly.
    """
    live_mode = console.is_terminal
    stopped = False
    ctx = Live(build_view(), console=console, refresh_per_second=10,
               transient=False) if live_mode else nullcontext()
    try:
        with ctx as live:
            for name in names:
                if live_mode:
                    live.update(build_view(f"[dim]Checking {name} ...   (Ctrl+C to stop)[/dim]"))
                row = add_result(name)
                if row is None:
                    continue
                if live_mode:
                    live.update(build_view("[dim](Ctrl+C to stop)[/dim]"))
                else:  # no in-place redraw available: one plain line per name
                    console.print(f"{row['n']:>5}. {row['name']:<22} {row['status']}"
                                  + (f"   pw: {row['password']}" if row['password'] else ""),
                                  markup=False, highlight=False)
                time.sleep(DELAY)
    except KeyboardInterrupt:
        stopped = True
    if not live_mode:
        console.print(build_view())
    return stopped


# --------------------------------------------------------------------------- #
# Recheck: remove names from the saved list that are no longer available
# --------------------------------------------------------------------------- #
ENTRY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)(?::|\s|$)")

# status -> (label, style, keep this line?)
RECHECK_RESULT = {
    "Available": ("Still available", "bold green", True),
    "Taken": ("Taken - removed", "red", False),
    "Blocked": ("Not allowed - removed", "yellow", False),
    "Invalid": ("Not allowed - removed", "yellow", False),
    "Error": ("Couldn't check - kept", "bold red", True),
}


def find_save_file():
    for path in (SAVE_FILE, FALLBACK_FILE):
        if os.path.isfile(path):
            return path
    return None


def write_lines(path, lines):
    """Rewrite the file safely (temp file first, so a crash can't wipe the list)."""
    data = "".join(line + "\n" for line in lines)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as f:  # may raise; caller handles it
            f.write(data)


def build_recheck_view(path, recent, stats, total, message=""):
    table = Table(box=box.ROUNDED, header_style="bold", expand=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Username", style="bold")
    table.add_column("Result")
    for n, name, status in recent:
        label, style, _ = RECHECK_RESULT[status]
        table.add_row(str(n), name, f"[{style}]{label}[/]")
    if not recent:
        table.add_row("-", "Nothing checked yet", "")

    done = sum(stats.values())
    summary = Panel(
        f"Checked: [bold]{done}[/bold] of {total}   "
        f"Still available: [green]{stats['Available']}[/green]   "
        f"Removed: [red]{stats['Taken'] + stats['Blocked'] + stats['Invalid']}[/red]   "
        f"Couldn't check: [yellow]{stats['Error']}[/yellow]\n"
        f"[dim]{path}[/dim]",
        title="Recheck", border_style="cyan", expand=False)
    parts = [Text("Recheck saved usernames", style="bold cyan"), Text(""), table, summary]
    if message:
        parts.append(Text.from_markup(message))
    return Group(*parts)


def action_recheck():
    path = find_save_file()
    if not path:
        console.print(f"[yellow]No {SAVE_NAME} found yet (looked in {SAVE_FILE} and "
                      f"{FALLBACK_FILE}).[/yellow]")
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        console.print(f"[bold red]Couldn't read {path}: {e}[/bold red]")
        return

    entries = []  # (line index, username)
    for i, line in enumerate(lines):
        m = ENTRY_RE.match(line)
        if m:
            entries.append((i, m.group(1)))
    if not entries:
        console.print(f"[yellow]{path} has no usernames in it.[/yellow]")
        return

    keep = [True] * len(lines)
    stats = {"Available": 0, "Taken": 0, "Blocked": 0, "Invalid": 0, "Error": 0}
    recent = deque(maxlen=MAX_ROWS_SHOWN)
    total = len(entries)

    clear_screen()
    live_mode = console.is_terminal
    ctx = Live(build_recheck_view(path, recent, stats, total), console=console,
               refresh_per_second=10, transient=False) if live_mode else nullcontext()
    stopped = False
    try:
        with ctx as live:
            for n, (i, name) in enumerate(entries, 1):
                if live_mode:
                    live.update(build_recheck_view(
                        path, recent, stats, total,
                        f"[dim]Checking {name} ...   (Ctrl+C to stop)[/dim]"))
                status, _note = check_username(name)
                stats[status] += 1
                recent.append((n, name, status))
                if not RECHECK_RESULT[status][2]:
                    keep[i] = False
                if live_mode:
                    live.update(build_recheck_view(
                        path, recent, stats, total, "[dim](Ctrl+C to stop)[/dim]"))
                else:
                    console.print(f"{n:>5}. {name:<22} {RECHECK_RESULT[status][0]}",
                                  markup=False, highlight=False)
                time.sleep(DELAY)
    except KeyboardInterrupt:
        stopped = True
    if not live_mode:
        console.print(build_recheck_view(path, recent, stats, total))

    removed = sum(1 for k in keep if not k)
    if stopped:
        console.print("[yellow]Stopped early - names not checked yet were left in the file.[/yellow]")
    if removed:
        try:
            write_lines(path, [line for i, line in enumerate(lines) if keep[i]])
        except OSError as e:
            console.print(f"[bold red]Couldn't update {path}: {e} (nothing was removed)[/bold red]")
            return
    left = sum(1 for _i, _n in entries) - removed
    console.print(f"[green]Removed {removed} name(s). {left} still in {path}.[/green]")
    if stats["Error"]:
        console.print(f"[yellow]{stats['Error']} name(s) couldn't be checked (network or "
                      f"rate limit) and were kept - run Recheck again later.[/yellow]")


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def ask_int(prompt, lo, hi, default):
    raw = console.input(f"{prompt} [{lo}-{hi}, default {default}]: ").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def ask_yes_no(prompt, default):
    d = "Y/n" if default else "y/N"
    raw = console.input(f"{prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def ask_count():
    raw = console.input("How many names? Enter a number, or 0 for INFINITE "
                        "(runs until you press Ctrl+C) [default 0]: ").strip().lower()
    if raw in ("", "0", "inf", "infinite", "infinity"):
        return 0
    try:
        return max(1, int(raw))
    except ValueError:
        return 0


def ask_generator_settings(defaults, title):
    """
    The 'how do you want it generated?' questions, shared by Custom generate
    (main menu option 2) and Create a preset. Returns (lo, hi, opts).
    """
    clear_screen()
    console.print(f"[bold cyan]{title}[/bold cyan] (press Enter to keep the default)\n")
    lo = ask_int("Minimum length", MIN_LEN, MAX_LEN, 5)
    hi = ask_int("Maximum length", lo, MAX_LEN, max(lo, 8))
    console.print()
    opts = dict(defaults)
    opts["lower"] = ask_yes_no("Allow lowercase letters (abc)", opts["lower"])
    opts["upper"] = ask_yes_no("Allow CAPITAL letters (ABC)", opts["upper"])
    if not opts["lower"] and not opts["upper"]:
        console.print("[yellow]Need at least some letters - using lowercase.[/yellow]")
        opts["lower"] = True
    opts["digits"] = ask_yes_no("Allow numbers (123)", opts["digits"])
    opts["underscore"] = ask_yes_no('Allow an underscore "_" (max one, never at the ends)',
                                    opts["underscore"])
    opts["pronounceable"] = ask_yes_no("Make names pronounceable (like Zelomi) instead of random",
                                       opts["pronounceable"])
    return lo, hi, opts


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def show_static(message=""):
    clear_screen()
    console.print(build_view(message))


def action_check():
    raw = console.input("Usernames to check (comma or space separated): ").strip()
    names = [n for n in re.split(r"[,\s]+", raw) if n]
    if not names:
        return
    clear_screen()
    stopped = run_loop(names)
    if stopped:
        console.print("[yellow]Stopped.[/yellow]")


def run_generator(lo, hi, opts, count):
    """Generate names with the given settings and check them (count 0 = infinite)."""
    def names():
        produced = misses = 0
        while count == 0 or produced < count:
            name = generate_username(random.randint(lo, hi), opts)
            if name.lower() in S.seen:
                misses += 1
                if misses >= 500:  # every name in this setup has been tried
                    console.print("[yellow]Ran out of new names for these settings.[/yellow]")
                    return
                continue
            misses = 0
            produced += 1
            bump_stats("generated", save=False)  # saved when the name gets checked
            yield name

    clear_screen()
    stopped = run_loop(names())
    if stopped:
        console.print("[yellow]Stopped.[/yellow]")


def action_generate():
    lo, hi, opts = ask_generator_settings(OPTS, "Custom generator settings")
    OPTS.update(opts)  # remember for next time
    console.print()
    count = ask_count()
    run_generator(lo, hi, opts, count)


def create_preset():
    """[0] on the presets menu: same questions as Custom generate, then save it."""
    lo, hi, opts = ask_generator_settings(OPTS, "Create a preset")
    console.print()
    default_name = f"Preset {len(PRESETS) + 1}"
    name = console.input(f"Name this preset [default {default_name}]: ").strip() or default_name
    PRESETS.append({"name": name, "lo": lo, "hi": hi, "opts": opts})
    if save_presets():
        console.print(f"\n[green]Saved '{escape(name)}' as preset [{len(PRESETS)}].[/green]")
    else:
        console.print(f"\n[yellow]'{escape(name)}' works this session but wasn't saved to disk.[/yellow]")
    time.sleep(1.2)


def action_presets():
    while True:
        clear_screen()
        console.print("[bold cyan]Presets[/bold cyan] - pick a number and it generates + checks names for you\n")
        console.print("[bold][0][/bold] Create a preset")
        for i, p in enumerate(PRESETS, 1):
            example = generate_username((p["lo"] + p["hi"]) // 2, p["opts"])
            console.print(f"[bold][{i}][/bold] {escape(p['name']):<24} "
                          f"[dim]{describe_settings(p['lo'], p['hi'], p['opts'])}  e.g. {example}[/dim]",
                          highlight=False)
        if not PRESETS:
            console.print("\n[dim]No presets yet - press 0 to make your first one.[/dim]")
        console.print()
        raw = console.input("Pick a number (Enter to go back): ").strip()
        if not raw:
            return
        if raw == "0":
            create_preset()
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(PRESETS):
            p = PRESETS[int(raw) - 1]
            console.print(f"\nUsing [bold]{escape(p['name'])}[/bold]: "
                          f"{describe_settings(p['lo'], p['hi'], p['opts'])}\n")
            count = ask_count()
            run_generator(p["lo"], p["hi"], p["opts"], count)
            return
        console.print("[yellow]Not a valid number.[/yellow]")
        time.sleep(0.8)


def action_stats():
    """Main menu: lifetime totals plus a per-day history."""
    while True:
        clear_screen()
        t = STATS["total"]
        tried = t["checked"]
        rate = f"{t['Available'] / tried * 100:.1f}%" if tried else "-"
        lines = [
            f"Generated: [bold]{t['generated']}[/bold]   Tried (checked): [bold]{tried}[/bold]",
            f"Available: [green]{t['Available']}[/green]   Taken: [red]{t['Taken']}[/red]   "
            f"Blocked/Invalid: [yellow]{t['Blocked'] + t['Invalid']}[/yellow]   "
            f"Errors: [yellow]{t['Error']}[/yellow]",
            f"Availability rate: {rate}",
        ]
        best = STATS["best_available"]
        if best:
            lines.append(f"Best available found: [bold]{escape(best['name'])}[/bold] "
                         f"(score {best['score']})")
        lines.append(f"This session: [bold]{S.total}[/bold] tried, "
                     f"[green]{S.counts['Available']}[/green] available")
        lines.append(f"[dim]Using since {STATS['first_used']} - {STATS['sessions']} session(s)[/dim]")

        table = Table(box=box.ROUNDED, header_style="bold", expand=False)
        table.add_column("Date")
        table.add_column("Generated", justify="right")
        table.add_column("Tried", justify="right")
        table.add_column("Available", justify="right", style="green")
        table.add_column("Taken", justify="right", style="red")
        table.add_column("Rate", justify="right")
        table.add_column("Activity")

        days = sorted(STATS["days"].items(), reverse=True)[:14]
        most = max((c["checked"] for _d, c in days), default=0)
        today = _today()
        for day, c in days:
            bar = "█" * max(1, round(20 * c["checked"] / most)) if c["checked"] and most else ""
            drate = f"{c['Available'] / c['checked'] * 100:.1f}%" if c["checked"] else "-"
            table.add_row(day + (" (today)" if day == today else ""),
                          str(c["generated"]), str(c["checked"]), str(c["Available"]),
                          str(c["Taken"]), drate, Text(bar, style="cyan"))
        if not days:
            table.add_row("-", "", "", "", "", "", "No activity yet")

        console.print(Text("Stats", style="bold cyan"))
        console.print()
        console.print(Panel("\n".join(lines), title="All time", border_style="cyan", expand=False))
        console.print(Text("Last 14 days with activity", style="dim"))
        console.print(table)
        console.print()
        raw = console.input("Press Enter to go back, or type RESET to wipe all stats: ").strip()
        if raw.upper() != "RESET":
            return
        if ask_yes_no("This permanently deletes your stats history. Really wipe it?", False):
            STATS.clear()
            STATS.update(_fresh_stats())
            STATS["sessions"] = 1
            save_stats()


# --------------------------------------------------------------------------- #
# Arrow-key menu
# --------------------------------------------------------------------------- #
def read_key():
    """Read one key press. Returns 'up', 'down', 'enter', 'esc' or the character."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):                  # arrow keys come as 2 chars
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch.lower()

    # macOS / Linux (these modules don't exist on Windows, so import them here)
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            if select.select([sys.stdin], [], [], 0.05)[0]:
                return {"[A": "up", "[B": "down"}.get(sys.stdin.read(2), "")
            return "esc"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def pick_username(entries, page=14):
    """
    Arrow-key picker. entries: list of dicts with name/password/score/line.
    Up/Down to move, Enter to choose, Esc or Q to cancel. Returns the entry or None.
    """
    pos = 0
    while True:
        clear_screen()
        console.print("[bold cyan]Pick a username[/bold cyan]  "
                      "[dim](Up/Down = move, Enter = select, Esc = cancel)[/dim]\n")
        start = max(0, min(pos - page // 2, len(entries) - page))
        for i in range(start, min(start + page, len(entries))):
            e = entries[i]
            label = f"{e['name']:<22} score {e['score']}"
            if i == pos:
                console.print(f"[bold black on cyan] > {escape(label)} [/]")
            else:
                console.print(f"   {escape(label)}")
        if len(entries) > page:
            console.print(f"\n[dim]{pos + 1} of {len(entries)}[/dim]")

        key = read_key()
        if key == "up":
            pos = (pos - 1) % len(entries)
        elif key == "down":
            pos = (pos + 1) % len(entries)
        elif key == "enter":
            return entries[pos]
        elif key in ("esc", "q"):
            return None


# --------------------------------------------------------------------------- #
# Signup cooldown (saved to disk so restarting the script doesn't skip it)
# --------------------------------------------------------------------------- #
def cooldown_remaining():
    try:
        with open(COOLDOWN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return max(0, int(data["last"] + data["wait"] - time.time()))
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def start_cooldown():
    try:
        with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump({"last": time.time(), "wait": random.randint(*COOLDOWN_RANGE)}, f)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Browser signup helper (fills the form; YOU solve the captcha)
# --------------------------------------------------------------------------- #
SIGNUP_URL = "https://www.roblox.com/signup"
SIGNUP_TIMEOUT = 20


def _human_pause(lo=0.3, hi=1.1):
    time.sleep(random.uniform(lo, hi))


def _human_type(element, text):
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.04, 0.16))
        if random.random() < 0.05:
            time.sleep(random.uniform(0.2, 0.5))


def _text_xpath(*labels):
    lower = "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    conds = " or ".join(f"{lower}='{label.lower()}'" for label in labels)
    return f"//button[{conds}] | //a[{conds}]"


def _safe_click(driver, element):
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    _human_pause()


def run_signup_browser(username, password, bday):
    """Open Chrome, fill the signup form, wait while you solve the captcha."""
    try:
        import undetected_chromedriver as uc
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import Select, WebDriverWait
    except ImportError:
        console.print("[bold red]Missing packages.[/bold red] Run:  "
                      "pip install undetected-chromedriver selenium setuptools certifi")
        return False

    clear_mod = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL

    def set_text(el, text):
        el.click()
        el.send_keys(clear_mod, "a")
        el.send_keys(Keys.DELETE)
        _human_type(el, text)

    def select_option(driver, element_id, *candidates):
        el = WebDriverWait(driver, SIGNUP_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, element_id)))
        sel = Select(el)
        for text in candidates:
            for action in (sel.select_by_visible_text, sel.select_by_value):
                try:
                    action(text)
                    _human_pause(0.2, 0.6)
                    return
                except Exception:
                    pass
        raise RuntimeError(f"Couldn't set #{element_id}. Options: {[o.text for o in sel.options]}")

    driver = uc.Chrome(options=uc.ChromeOptions())
    wait = WebDriverWait(driver, SIGNUP_TIMEOUT)
    success = False
    try:
        driver.get(SIGNUP_URL)
        _human_pause(1.0, 2.0)

        # Cookie banner (if any)
        end = time.time() + 8
        while time.time() < end:
            found = [e for e in driver.find_elements(By.ID, "onetrust-accept-btn-handler")
                     if e.is_displayed()]
            if found:
                _safe_click(driver, found[0])
                break
            time.sleep(0.5)

        # Open the form if it isn't showing yet
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "MonthDropdown")))
        except TimeoutException:
            _safe_click(driver, wait.until(
                EC.element_to_be_clickable((By.XPATH, _text_xpath("Sign Up")))))

        select_option(driver, "MonthDropdown", bday.strftime("%b"), bday.strftime("%B"), str(bday.month))
        select_option(driver, "DayDropdown", str(bday.day))
        select_option(driver, "YearDropdown", str(bday.year))

        set_text(wait.until(EC.element_to_be_clickable((By.ID, "signup-username"))), username)
        set_text(wait.until(EC.element_to_be_clickable((By.ID, "signup-password"))), password)

        try:
            btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "signup-button")))
        except TimeoutException:
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, _text_xpath("Sign Up"))))
        _safe_click(driver, btn)

        console.print("\n[bold]Clicked Sign Up.[/bold] If a captcha appeared in the browser, "
                      "solve it now. Waiting up to 3 minutes...")
        try:
            WebDriverWait(driver, 180).until(lambda d: "/signup" not in d.current_url)
            console.print(f"[green]Signup appears to have succeeded.[/green] Now at: {driver.current_url}")
            success = True
        except TimeoutException:
            console.print("[yellow]Still on the signup page after 3 minutes. Check the browser "
                          "for an error (username taken, captcha expired...).[/yellow]")
        console.input("Press Enter to close the browser...")
    except (TimeoutException, RuntimeError, WebDriverException) as e:
        console.print(f"\n[bold red]Failed:[/bold red] {escape(str(e))}")
        console.print("[dim]Roblox changes its markup often; if an element wasn't found, "
                      "the IDs in run_signup_browser may need updating.[/dim]")
        console.input("Press Enter to close the browser...")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return success


# --------------------------------------------------------------------------- #
# Menu action: pick a saved username, then make the account
# --------------------------------------------------------------------------- #
SAVED_RE = re.compile(r"^\s*([A-Za-z0-9_]+):(\S+)(?:\s+#\s*score\s*(\d+))?")


def action_signup():
    if not (console.is_terminal and sys.stdin.isatty()):
        console.print("[yellow]The arrow-key picker needs a real terminal.[/yellow]")
        return

    wait_left = cooldown_remaining()
    if wait_left:
        console.print(f"[yellow]Cooldown active - try again in {wait_left // 60}m "
                      f"{wait_left % 60:02d}s.[/yellow]")
        return

    path = find_save_file()
    if not path:
        console.print(f"[yellow]No {SAVE_NAME} yet - generate some available names first.[/yellow]")
        return
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    entries = []
    for i, line in enumerate(lines):
        m = SAVED_RE.match(line)
        if m:
            entries.append({"name": m.group(1), "password": m.group(2),
                            "score": int(m.group(3) or score_name(m.group(1))), "line": i})
    if not entries:
        console.print("[yellow]No saved usernames with passwords found.[/yellow]")
        return
    entries.sort(key=lambda e: -e["score"])

    choice = pick_username(entries)
    clear_screen()
    if not choice:
        console.print("[yellow]Cancelled.[/yellow]")
        return

    name = choice["name"]
    console.print(f"Checking that [bold]{escape(name)}[/bold] is still available...")
    status, _note = check_username(name)
    if status != "Available":
        console.print(f"[red]{escape(name)} is {status.lower()} now - pick another one.[/red]")
        return

    while True:
        raw = console.input("Birthday for the account (YYYY-MM-DD, Enter to cancel): ").strip()
        if not raw:
            return
        try:
            bday = datetime.strptime(raw, "%Y-%m-%d")
            break
        except ValueError:
            console.print("[yellow]Use the format YYYY-MM-DD, e.g. 2000-01-31.[/yellow]")

    console.print(f"\nUsername: [bold]{escape(name)}[/bold]\nPassword: saved from your list "
                  f"(also in {escape(os.path.basename(path))})")
    if not ask_yes_no("Open the browser and start signup", True):
        return

    start_cooldown()   # starts on every attempt, so failed attempts count too
    ok = run_signup_browser(name, choice["password"], bday)
    if ok:
        keep = [l for i, l in enumerate(lines) if i != choice["line"]]
        try:
            write_lines(path, keep)
            console.print(f"[green]Removed {escape(name)} from your available list.[/green]")
        except OSError:
            pass
    left = cooldown_remaining()
    console.print(f"[dim]Next signup unlocks in about {left // 60}m {left % 60:02d}s.[/dim]")


# --------------------------------------------------------------------------- #
# Main menu
# --------------------------------------------------------------------------- #
def main():
    boot_sequence()
    show_static()
    while True:
        console.print("\n[bold][1][/bold] Check usernames   [bold][2][/bold] Custom generate   "
                      "[bold][3][/bold] Presets   [bold][4][/bold] Recheck saved list   "
                      "[bold][5][/bold] Stats   [bold][6][/bold] Clear table   "
                      "[bold][7][/bold] Create account   [bold][8][/bold] Quit")
        try:
            choice = console.input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        try:
            if choice == "1":
                action_check()
            elif choice == "2":
                action_generate()
            elif choice == "3":
                action_presets()
            elif choice == "4":
                action_recheck()
            elif choice == "5":
                action_stats()
            elif choice == "6":
                S.reset()
                show_static()
            elif choice == "7":
                action_signup()
            elif choice == "8":
                break
            else:
                console.print("[yellow]Pick 1-8.[/yellow]")
        except KeyboardInterrupt:  # Ctrl+C during a prompt -> back to menu
            console.print("\n[yellow]Cancelled.[/yellow]")


if __name__ == "__main__":
    main()
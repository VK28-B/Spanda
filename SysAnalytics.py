"""
Spanda v3.0 - Advanced System Monitor
A polished, responsive, animated system dashboard for Windows and Linux.

Requirements:
    pip install dearpygui psutil duckdb
    pip install gputil        # optional - NVIDIA GPU monitoring

The UI is built around responsive tables, incremental widget updates and a
lightweight per-frame animation layer so it stays smooth while collecting
metrics once per second.
"""

from __future__ import annotations

import csv
import datetime
import math
import os
import platform
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager

import dearpygui.dearpygui as dpg
import duckdb
import psutil

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
DEFAULT_SYS_DRIVE = "C:\\" if IS_WINDOWS else "/"

try:
    import winreg
except Exception:
    winreg = None

try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    Image = None
    PIL_AVAILABLE = False

WIN_ICON_SUPPORT = False
if IS_WINDOWS:
    try:
        import win32gui
        import win32ui
        WIN_ICON_SUPPORT = PIL_AVAILABLE
    except Exception:
        win32gui = win32ui = None
else:
    win32gui = win32ui = None

# Linux process-icon support uses .desktop entries and icon themes through PIL.
ICON_SUPPORT = PIL_AVAILABLE

try:
    import win32api
except Exception:
    win32api = None

try:
    import GPUtil
    GPU_LIB = True
except ImportError:
    GPU_LIB = False


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
APP_NAME          = "Spanda"
APP_VERSION       = "3.0"
MAX_DATA_POINTS   = 300          # 5 minutes at 1 Hz
WINDOW_SECONDS    = 300
SAMPLE_INTERVAL_S = 1.0
DB_FLUSH_INTERVAL = 5.0
DB_RETENTION_DAYS = 7
DB_STATS_INTERVAL = 30.0
MAX_PROC_ROWS     = 400          # the table is clipped, so this is "all"
MAX_CORES_DISPLAY = 64
MAX_QUERY_ROWS    = 500
ICON_SIZE         = 20           # process icon size, in pixels
ICON_EXTRACT_BUDGET = 12         # max new icons extracted per refresh tick
EXE_CACHE_TTL     = 60.0         # seconds before re-resolving a PID's exe path
PROC_RESORT_INTERVAL = 3.0       # seconds between live row reshuffles
PROC_CPU_SMOOTH   = 0.35         # EMA weight for sort-only CPU smoothing

HEADER_H      = 72
FOOTER_H      = 32
TABBAR_CHROME = 44
MAIN_PAD      = 20
GAP           = 8

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resource_path(*parts):
    """Resolve bundled assets in source and PyInstaller builds."""
    base = getattr(sys, "_MEIPASS", None) or BASE_DIR
    return os.path.join(base, *parts)


def _env(name, default=""):
    """Read SPANDA_* first, with legacy SYSANALYTICS_* kept for smoke tests."""
    return os.environ.get("SPANDA_" + name,
                          os.environ.get("SYSANALYTICS_" + name, default))


def _data_home():
    override = os.environ.get("SPANDA_DATA_HOME")
    if override:
        return override
    if IS_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "Spanda")
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(xdg, "Spanda")


def _export_dir():
    if IS_WINDOWS:
        return BASE_DIR
    docs = os.environ.get("XDG_DOCUMENTS_HOME") or os.path.join(
        os.path.expanduser("~"), "Documents")
    try:
        os.makedirs(docs, exist_ok=True)
        return docs
    except OSError:
        return os.path.expanduser("~")


def _resolve_db_path() -> str:
    """Use Spanda's data directory, migrating legacy SysAnalytics data."""
    legacy_local = os.path.join(
        os.environ.get("LOCALAPPDATA", BASE_DIR), "SysAnalytics",
        "sysanalytics.duckdb")
    legacy_root = os.path.join(BASE_DIR, "sysanalytics.duckdb")
    data_dir = _data_home()
    try:
        os.makedirs(data_dir, exist_ok=True)
        target = os.path.join(data_dir, "spanda.duckdb")
    except OSError:
        return legacy_local if os.path.exists(legacy_local) else legacy_root
    if not os.path.exists(target):
        for old in (legacy_local, legacy_root):
            if os.path.exists(old):
                try:
                    shutil.copy2(old, target)
                except OSError:
                    continue
                break
    return target


DB_PATH = _resolve_db_path()


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────────────────────
C_BG      = (9, 11, 17)
C_PANEL   = (17, 20, 29)
C_PANEL2  = (21, 25, 36)
C_PANEL3  = (26, 31, 45)
C_TRACK   = (31, 36, 51)
C_BORDER  = (37, 43, 60)
C_GRID    = (34, 40, 56)
C_TEXT    = (222, 227, 240)
C_DIM     = (152, 163, 188)
C_MUTED   = (110, 122, 150)
C_WHITE   = (255, 255, 255)
C_ACCENT  = (56, 140, 255)

C_CPU     = (56, 170, 255)
C_MEM     = (34, 211, 165)
C_DISK    = (255, 176, 64)
C_NET     = (255, 105, 97)
C_NET2    = (255, 214, 88)
C_GPU     = (178, 102, 255)
C_SWAP    = (139, 124, 255)
C_GREEN   = (52, 211, 153)
C_AMBER   = (251, 191, 36)
C_RED     = (248, 113, 113)


def _l(c):
    """Tuple -> list (DearPyGui colour)."""
    return [c[0], c[1], c[2]]


def _a(c, alpha):
    return [c[0], c[1], c[2], int(max(0, min(255, alpha)))]


def _mix(c1, c2, t):
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _level_color(v, warn=60.0, crit=85.0):
    if v >= crit:
        return C_RED
    if v >= warn:
        return C_AMBER
    return C_GREEN


def _fmt_bytes(n):
    n = float(n or 0)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n:.0f} B"


def _fmt_speed(kbps):
    kbps = max(0.0, float(kbps or 0))
    if kbps >= 1024 ** 2:
        return f"{kbps / 1024 ** 2:.2f} GB/s"
    if kbps >= 1024:
        return f"{kbps / 1024:.2f} MB/s"
    return f"{kbps:.1f} KB/s"


def _fmt_gb(gb):
    gb = float(gb or 0)
    if gb >= 1024:
        return f"{gb / 1024:.2f} TB"
    return f"{gb:.1f} GB"


def _short(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: max(0, n - 3)] + "..."


_STATUS_LABELS = {
    "running": "Running",
    "sleeping": "Sleeping",
    "waiting": "Waiting",
    "stopped": "Suspended",
    "zombie": "Zombie",
    "unknown": "Unknown",
}

# Friendly app names for the grouped Processes view (Windows 11 style).
_FRIENDLY_NAMES = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "code.exe": "Visual Studio Code",
    "explorer.exe": "File Explorer",
    "opencode-cli.exe": "Opencode CLI",
    "opencode.exe": "Opencode",
    "python.exe": "Python",
    "pythonw.exe": "Python",
    "powershell.exe": "Windows PowerShell",
    "pwsh.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "windows terminal.exe": "Windows Terminal",
    "devenv.exe": "Visual Studio",
    "idea64.exe": "IntelliJ IDEA",
    "notepad.exe": "Notepad",
    "msmpeng.exe": "Microsoft Defender",
    "windefend.exe": "Microsoft Defender",
    "svchost.exe": "Service Host",
    "memory compression": "Memory Compression",
    "brave.exe": "Brave",
    "systemsettings.exe": "System Settings",
    "shellexperiencehost.exe": "Shell Experience Host",
    "applicationframehost.exe": "Application Frame Host",
    "searchhost.exe": "Windows Search",
    "searchindexer.exe": "Windows Search Indexer",
    "startmenuexperiencehost.exe": "Start Menu",
    "textinputhost.exe": "Text Input Host",
    "lockapp.exe": "LockApp",
    "runtimebroker.exe": "Runtime Broker",
    "audiodg.exe": "Windows Audio Device Graph",
    "spoolsv.exe": "Print Spooler",
    "fontdrvhost.exe": "Font Driver Host",
    "dwm.exe": "Desktop Window Manager",
    "lsass.exe": "Local Security Authority",
    "winlogon.exe": "Windows Logon",
    "wininit.exe": "Windows Initialization",
    "smss.exe": "Session Manager",
    "csrss.exe": "Client Server Runtime",
    "services.exe": "Service Control Manager",
    # Linux / KDE / common desktop processes
    "systemd": "Systemd",
    "init": "System Init",
    "xorg": "X Server",
    "xwayland": "XWayland",
    "wayland": "Wayland Compositor",
    "kwin_x11": "KWin",
    "kwin_wayland": "KWin",
    "plasmashell": "Plasma Desktop",
    "plasma-session": "Plasma Session",
    "ksmserver": "KDE Session Manager",
    "kworker": "Kernel Worker",
    "ksoftirqd": "Kernel Softirq",
    "dbus-daemon": "D-Bus",
    "pipewire": "PipeWire",
    "pulseaudio": "PulseAudio",
    "wireplumber": "WirePlumber",
    "chrome": "Google Chrome",
    "google-chrome": "Google Chrome",
    "google-chrome-stable": "Google Chrome",
    "chromium": "Chromium",
    "firefox": "Mozilla Firefox",
    "code": "Visual Studio Code",
    "python": "Python",
    "python3": "Python",
}


def _friendly_name(raw):
    """Short, readable app name for a grouped process row."""
    name = str(raw or "?").strip()
    key = name.lower()
    if key in _FRIENDLY_NAMES:
        return _FRIENDLY_NAMES[key]
    base = os.path.splitext(name)[0].strip()
    if not base:
        return name
    return base[0].upper() + base[1:]


# ── Portable helpers ────────────────────────────────────────────────────────
def _cmd_text(args, timeout=4):
    """Run a read-only command and return stdout, or '' when unavailable."""
    try:
        p = subprocess.run(
            list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=timeout, check=False,
        )
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _linux_os_line():
    vals = {}
    for line in _read_text("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals.get("PRETTY_NAME") or vals.get("NAME") or \
        f"{platform.system()} {platform.release()}"


def _cpu_model_name():
    txt = _cmd_text(["lscpu"])
    for line in txt.splitlines():
        if line.lower().startswith("model name:"):
            return line.split(":", 1)[1].strip()
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


_LINUX_DESKTOP_DIRS = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/snapd/desktop/applications",
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
)
_LINUX_DESKTOP_CACHE = None


def _linux_desktop_entries():
    """Parse installed .desktop files once for display names and icons."""
    global _LINUX_DESKTOP_CACHE
    if _LINUX_DESKTOP_CACHE is not None:
        return _LINUX_DESKTOP_CACHE
    entries = {}
    for directory in _LINUX_DESKTOP_DIRS:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".desktop"):
                continue
            path = os.path.join(directory, filename)
            data = {}
            in_main = False
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("["):
                            in_main = line == "[Desktop Entry]"
                            continue
                        if in_main and "=" in line:
                            key, value = line.split("=", 1)
                            data[key.strip()] = value.strip()
            except OSError:
                continue
            if data.get("Hidden", "").lower() == "true":
                continue
            entry = {
                "name": data.get("Name", ""),
                "icon": data.get("Icon", ""),
            }
            if not entry["name"] and not entry["icon"]:
                continue
            stem = os.path.splitext(filename)[0].lower()
            entries.setdefault(stem, entry)
            exec_line = data.get("Exec", "")
            try:
                parts = shlex.split(exec_line)
            except ValueError:
                parts = exec_line.split()
            for part in parts:
                if not part or part.startswith("%"):
                    continue
                if "=" in part and not part.startswith(("/", ".")):
                    continue
                exe = os.path.basename(part).lower()
                if exe:
                    entries.setdefault(exe, entry)
                    break
    _LINUX_DESKTOP_CACHE = entries
    return entries


def _linux_desktop_for(name):
    raw = os.path.basename(str(name or "")).strip().lower()
    if not raw:
        return None
    entries = _linux_desktop_entries()
    variants = (
        raw,
        os.path.splitext(raw)[0],
        raw.split("-")[0],
        raw.split(".")[0],
    )
    for key in variants:
        if key in entries:
            return entries[key]
    for directory in _LINUX_DESKTOP_DIRS:
        for key in variants:
            candidate = os.path.join(directory, key + ".desktop")
            if os.path.isfile(candidate):
                entry = entries.get(key)
                if entry:
                    return entry
    return None


def _linux_display_name(name):
    entry = _linux_desktop_for(name)
    if entry and entry.get("name"):
        return entry["name"]
    return _friendly_name(name)


_LINUX_ICON_DIRS = (
    "/usr/share/icons/hicolor",
    "/usr/share/icons/Adwaita",
    "/usr/share/icons",
    "/usr/local/share/icons",
    "/usr/share/pixmaps",
    os.path.expanduser("~/.local/share/icons"),
)
_LINUX_ICON_EXTS = (".png", ".xpm", ".webp", ".jpg", ".jpeg", ".bmp")
_LINUX_ICON_INDEX = None


def _icon_rank(path):
    low = path.replace("\\", "/").lower()
    ext = os.path.splitext(low)[1]
    ext_rank = {".png": 0, ".xpm": 1, ".webp": 2, ".jpg": 3,
                ".jpeg": 3, ".bmp": 4}.get(ext, 9)
    size_rank = 9
    for size, rank in ((48, 0), (64, 1), (32, 2), (128, 3),
                       (256, 4), (24, 5), (16, 6)):
        if f"/{size}x{size}/" in low:
            size_rank = rank
            break
    return (size_rank, ext_rank, len(path))


def _linux_icon_index():
    global _LINUX_ICON_INDEX
    if _LINUX_ICON_INDEX is not None:
        return _LINUX_ICON_INDEX
    index = {}
    for root in _LINUX_ICON_DIRS:
        if not os.path.isdir(root):
            continue
        for folder, _dirs, files in os.walk(root):
            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext not in _LINUX_ICON_EXTS:
                    continue
                path = os.path.join(folder, filename)
                rank = _icon_rank(path)
                for key in (filename.lower(), os.path.splitext(filename)[0].lower()):
                    old = index.get(key)
                    if old is None or rank < _icon_rank(old):
                        index[key] = path
    _LINUX_ICON_INDEX = index
    return index


def _resolve_linux_icon_path(name, path=""):
    if path and os.path.isfile(path) and \
            os.path.splitext(path)[1].lower() in _LINUX_ICON_EXTS:
        return path
    candidates = []
    entry = _linux_desktop_for(name)
    if entry and entry.get("icon"):
        candidates.append(entry["icon"])
    base = os.path.splitext(os.path.basename(str(name or path or "")))[0]
    candidates.extend((base, os.path.basename(str(name or ""))))
    index = _linux_icon_index()
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.path.isfile(candidate):
            return candidate
        hit = index.get(candidate.lower()) or index.get(
            os.path.splitext(candidate)[0].lower())
        if hit:
            return hit
    return None


def _rgba_from_image_file(path, size=ICON_SIZE):
    if not PIL_AVAILABLE or not path:
        return None
    try:
        im = Image.open(path).convert("RGBA")
        im.thumbnail((size, size), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.alpha_composite(im, ((size - im.width) // 2,
                                    (size - im.height) // 2))
        return [v / 255.0 for v in canvas.tobytes()]
    except Exception:
        return None


def _linux_memory_modules():
    txt = _cmd_text(["dmidecode", "-t", "memory"], timeout=5)
    modules = []
    block = []
    if txt:
        for raw in txt.splitlines() + [""]:
            if raw.strip() == "Memory Device":
                block = []
                continue
            if not raw.strip():
                if block:
                    fields = {}
                    for line in block:
                        if ":" in line:
                            k, v = line.split(":", 1)
                            fields[k.strip()] = v.strip()
                    size = fields.get("Size", "")
                    if size and "No Module" not in size and "Unknown" not in size:
                        line = size
                        man = fields.get("Manufacturer", "")
                        part = fields.get("Part Number", "")
                        speed = fields.get("Speed", "")
                        if man and man.lower() not in ("unknown", "none"):
                            line += f"  {man}"
                        if part and part.lower() not in ("unknown", "none"):
                            line += f"  {part}"
                        if speed and speed.lower() not in ("unknown", "none"):
                            line += f"  @ {speed}"
                        modules.append(line)
                    block = []
                continue
            if block or raw.strip() == "Memory Device":
                block.append(raw)
    return modules


def _linux_gpus(s):
    gpus = []
    nvidia = _cmd_text([
        "nvidia-smi", "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ])
    for line in nvidia.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        text = parts[0]
        try:
            vram = float(parts[1]) / 1024.0
            if vram >= 0.9:
                text += f"  •  {vram:.1f} GB VRAM"
        except (IndexError, ValueError):
            pass
        if len(parts) > 2 and parts[2]:
            text += f"  •  driver {parts[2]}"
        gpus.append(text)
    lspci = _cmd_text(["lspci"])
    for line in lspci.splitlines():
        low = line.lower()
        if any(k in low for k in ("vga compatible", "3d controller",
                                  "display controller")):
            name = line.split(":", 1)[1].strip() if ":" in line else line.strip()
            if name and name not in gpus:
                gpus.append(name)
    if not gpus and s.get("has_gpu"):
        gpus.append(s["gpu"])
    return gpus


def _linux_displays():
    lines = []
    xrandr = _cmd_text(["xrandr", "--current"])
    for raw in xrandr.splitlines():
        if " connected" not in raw:
            continue
        parts = raw.split()
        name = parts[0]
        mode = ""
        refresh = ""
        for part in parts[1:]:
            if "x" in part and part.split("x")[0].isdigit():
                mode = part
                continue
            if mode and any(ch.isdigit() for ch in part):
                refresh = part
                break
        detail = mode or "Connected"
        if refresh:
            detail += f"  •  {refresh}"
        lines.append((name, detail))
    if lines:
        return lines
    try:
        for entry in sorted(os.listdir("/sys/class/drm")):
            if not entry.startswith("card") or "-" not in entry:
                continue
            status = _read_text(f"/sys/class/drm/{entry}/status")
            if status == "connected":
                lines.append((entry.split("-", 1)[1], "Connected"))
    except OSError:
        pass
    return lines


def _collect_linux_specs(s):
    """Seven-section Specs inventory for Kubuntu/Plasma and other Linux DEs."""
    os_line = _linux_os_line()
    lscpu = {}
    for line in _cmd_text(["lscpu"]).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            lscpu[k.strip()] = v.strip()

    cpu_name = lscpu.get("Model name") or _cpu_model_name() or \
        s.get("cpu_name") or "Processor"
    vendor = lscpu.get("Vendor ID") or _read_text(
        "/sys/devices/virtual/dmi/id/sys_vendor") or ""
    mhz = lscpu.get("CPU max MHz") or lscpu.get("CPU MHz") or ""
    cpu_speed = ""
    if mhz:
        try:
            cpu_speed = f"{float(mhz) / 1000:.2f} GHz"
        except ValueError:
            cpu_speed = mhz

    maker = _read_text("/sys/class/dmi/id/sys_vendor") or \
        _read_text("/sys/devices/virtual/dmi/id/sys_vendor")
    model = _read_text("/sys/class/dmi/id/product_name") or \
        _read_text("/sys/devices/virtual/dmi/id/product_name")
    bios_v = _read_text("/sys/class/dmi/id/bios_version")
    bios_d = _read_text("/sys/class/dmi/id/bios_date")

    vm = psutil.virtual_memory()
    modules = _linux_memory_modules()
    if not modules:
        modules = [f"{vm.total / 1024 ** 3:.2f} GB  •  /proc/meminfo"]

    storage = []
    try:
        for part in psutil.disk_partitions(all=False):
            if "loop" in part.device or part.fstype in ("squashfs", "tmpfs"):
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            total = u.total / 1024 ** 3
            used = u.used / 1024 ** 3
            free = u.free / 1024 ** 3
            storage.append((
                part.mountpoint,
                f"{used:,.1f} / {total:,.1f} GB used  "
                f"•  {free:,.1f} GB free  •  {part.fstype or '?'}",
            ))
    except Exception:
        pass
    physical = _cmd_text(["lsblk", "-d", "-o", "NAME,MODEL,SIZE,ROTA"])
    if physical:
        lines = [line for line in physical.splitlines() if line.strip()]
        if len(lines) > 1:
            storage.append(("Physical disks", "  |  ".join(lines[1:])))

    network = []
    try:
        for name, addrs in psutil.net_if_addrs().items():
            if name.lower().startswith("lo"):
                continue
            mac = next((a.address for a in addrs
                        if a.family == psutil.AF_LINK), "")
            ip = next((a.address for a in addrs
                       if a.family == psutil.AF_INET
                       and not a.address.startswith("127.")), "")
            if ip or mac:
                network.append((name, f"{ip or 'No IPv4'}  •  {mac or 'No MAC'}"))
    except Exception:
        pass

    try:
        boot = datetime.datetime.fromtimestamp(psutil.boot_time())
        boot_line = boot.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        boot_line = "—"
    battery = psutil.sensors_battery()
    if battery:
        battery_line = (f"{battery.percent:.0f}%  "
                        f"({'charging' if battery.power_plugged else 'on battery'})")
    else:
        battery_line = "Not present"

    displays = _linux_displays()
    primary = displays[0][1] if displays else ""
    resolution = primary.split("  •  ")[0] if primary else "—"

    return [
        ("System", [
            ("Device", s["node"]),
            ("Operating system", os_line),
            ("Architecture", s["arch"]),
            ("Manufacturer", maker or "—"),
            ("Model", model or "—"),
            ("BIOS", (bios_v or "—") + (f"  •  {bios_d}" if bios_d else "")),
            ("Booted", boot_line),
            ("Battery", battery_line),
        ]),
        ("Processor", [
            ("CPU", cpu_name),
            ("Vendor", vendor or "—"),
            ("Cores / threads", f"{s['cpu_phys']} physical  •  {s['cpu_log']} logical"),
            ("Rated speed", cpu_speed or s["cpu_max_freq"]),
        ]),
        ("Memory", [
            ("Installed RAM", f"{vm.total / 1024 ** 3:.2f} GB"),
            ("Available", f"{vm.available / 1024 ** 3:.2f} GB"),
            ("Modules", "\n".join(modules)),
        ]),
        ("Storage", storage or [(s["sys_drive"], f"{s['disk_t']:,.1f} GB")]),
        ("Graphics", [(f"GPU {i + 1}", g) for i, g in enumerate(_linux_gpus(s))]
                     or [("GPU", s["gpu"])]),
        ("Network", network or [("Adapters", "Not detected")]),
        ("Display", [
            ("Primary resolution", resolution),
            ("DPI awareness", "Per-monitor aware (X11/XWayland)"),
        ]),
    ]


def _status_label(value):
    """Turn raw NT status strings into short, Task Manager-style labels."""
    v = str(value or "").strip().lower()
    if not v:
        return "—"
    if v in _STATUS_LABELS:
        return _STATUS_LABELS[v]
    return _short(v.capitalize(), 12)


def _fallback_icon_rgba(size=ICON_SIZE):
    """A neutral placeholder icon, used until a real one is extracted."""
    out = []
    r = size * 0.42
    cx = cy = (size - 1) / 2.0
    for y in range(size):
        for x in range(size):
            inside = (x - cx) ** 2 + (y - cy) ** 2 <= r * r
            out.extend((0.46, 0.52, 0.62, 1.0) if inside else (0.0, 0.0, 0.0, 0.0))
    return out


def _icon_rgba_from_path(path, size=ICON_SIZE):
    """Extract a small icon from an executable/DLL and return flat RGBA floats.

    On Windows this uses the same single-HICON API as Explorer.  On Linux it
    resolves the process desktop entry / icon theme and decodes that image.
    """
    if not ICON_SUPPORT or not path:
        return None
    if IS_LINUX:
        name = os.path.basename(path)
        icon_path = _resolve_linux_icon_path(name, path)
        return _rgba_from_image_file(icon_path, size) if icon_path else None
    large, small = [], []
    hbm_mask = hbm_color = 0
    try:
        try:
            large, small = win32gui.ExtractIconEx(path, 0)
        except Exception:
            return None
        # Prefer the 32px icon and downscale; upscaling the 16px one looks soft.
        icons = list(large or small)
        if not icons:
            return None
        try:
            info = win32gui.GetIconInfo(icons[0])
            if not info:
                return None
            _is_icon, _xhot, _yhot, hbm_mask, hbm_color = info
            src = hbm_color or hbm_mask
            if not src:
                return None
            bm = win32ui.CreateBitmapFromHandle(src)
            w = int(bm.GetInfo()["bmWidth"])
            h = int(bm.GetInfo()["bmHeight"])
            bits = bm.GetBitmapBits(True)
        except Exception:
            return None
        if not bits or w <= 0 or h <= 0 or len(bits) < w * h * 4:
            return None
        # GetBitmapBits(True) already gives top-down BGRA for icon bitmaps.
        out = bytearray(w * h * 4)
        for y in range(h):
            sy = y
            row = bits[sy * w * 4:(sy + 1) * w * 4]
            dst = y * w * 4
            for x in range(w):
                b, g, r, a = row[x * 4:x * 4 + 4]
                out[dst + x * 4] = r
                out[dst + x * 4 + 1] = g
                out[dst + x * 4 + 2] = b
                out[dst + x * 4 + 3] = a
        try:
            img = Image.frombytes("RGBA", (w, h), bytes(out))
            if (w, h) != (size, size):
                img = img.resize((size, size), Image.LANCZOS)
            return [v / 255.0 for px in img.getdata() for v in px]
        except Exception:
            return None
    finally:
        for h in list(large) + list(small):
            try:
                win32gui.DestroyIcon(h)
            except Exception:
                pass
        for h in (hbm_mask, hbm_color):
            if h:
                try:
                    win32gui.DeleteObject(h)
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Fast process enumeration
#
# psutil walks every process with individual syscalls, which costs seconds on
# machines with many processes.  Task Manager instead reads one kernel
# snapshot, so we do the same: a single NtQuerySystemInformation call returns
# name, PID/PPID, thread and handle counts, working-set/private bytes, CPU
# times and I/O counters for every process in a few milliseconds.
# ─────────────────────────────────────────────────────────────────────────────
_WIN_SNAPSHOT_OK = False

try:  # Windows only - the psutil path below is the portable fallback
    import ctypes
    from ctypes import wintypes as _wt

    _ntdll = ctypes.WinDLL("ntdll")
    _NT_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    _SPI_CLASS = 5  # SystemProcessInformation

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", _wt.USHORT),
            ("MaximumLength", _wt.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    class _SystemProcessInformation(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", _wt.ULONG),
            ("NumberOfThreads", _wt.ULONG),
            ("WorkingSetPrivateSize", ctypes.c_longlong),
            ("HardFaultCount", _wt.ULONG),
            ("NumberOfThreadsHighWatermark", _wt.ULONG),
            ("CycleTime", ctypes.c_ulonglong),
            ("CreateTime", ctypes.c_longlong),
            ("UserTime", ctypes.c_longlong),
            ("KernelTime", ctypes.c_longlong),
            ("ImageName", _UnicodeString),
            ("BasePriority", ctypes.c_long),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
            ("HandleCount", _wt.ULONG),
            ("SessionId", _wt.ULONG),
            ("PeakVirtualSize", ctypes.c_size_t),
            ("VirtualSize", ctypes.c_size_t),
            ("PageFaultCount", _wt.ULONG),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivatePageCount", ctypes.c_size_t),
            ("ReadOperationCount", ctypes.c_longlong),
            ("WriteOperationCount", ctypes.c_longlong),
            ("OtherOperationCount", ctypes.c_longlong),
            ("ReadTransferCount", ctypes.c_longlong),
            ("WriteTransferCount", ctypes.c_longlong),
            ("OtherTransferCount", ctypes.c_longlong),
        ]

    class _SystemThreadInformation(ctypes.Structure):
        _fields_ = [
            ("KernelTime", ctypes.c_longlong),
            ("UserTime", ctypes.c_longlong),
            ("CreateTime", ctypes.c_longlong),
            ("WaitTime", _wt.ULONG),
            ("StartAddress", ctypes.c_void_p),
            ("UniqueProcess", ctypes.c_void_p),
            ("UniqueThread", ctypes.c_void_p),
            ("Priority", ctypes.c_long),
            ("BasePriority", ctypes.c_long),
            ("ContextSwitches", _wt.ULONG),
            ("ThreadState", _wt.ULONG),
            ("WaitReason", _wt.ULONG),
        ]

    _SPI_SIZE = ctypes.sizeof(_SystemProcessInformation)
    _STI_SIZE = ctypes.sizeof(_SystemThreadInformation)
    _snap_buf = None
    _snap_size = 0

    def _win_process_snapshot():
        """Return one dict per process, or None if the snapshot failed."""
        global _snap_buf, _snap_size
        if _snap_buf is None:
            _snap_size = 256 * 1024
            _snap_buf = ctypes.create_string_buffer(_snap_size)
        for _ in range(6):
            ret_len = _wt.ULONG(0)
            status = _ntdll.NtQuerySystemInformation(
                _SPI_CLASS, _snap_buf, _snap_size, ctypes.byref(ret_len),
            )
            if status == _NT_STATUS_INFO_LENGTH_MISMATCH or ret_len.value > _snap_size:
                _snap_size = max(_snap_size * 2, ret_len.value + 65536)
                _snap_buf = ctypes.create_string_buffer(_snap_size)
                continue
            if status != 0:
                return None
            break
        else:
            return None

        base = ctypes.addressof(_snap_buf)
        offset = 0
        out = []
        buf_len = len(_snap_buf)
        while offset + _SPI_SIZE <= buf_len:
            spi = _SystemProcessInformation.from_address(base + offset)
            pid = spi.UniqueProcessId or 0
            name = ""
            if spi.ImageName.Buffer and spi.ImageName.Length:
                name = ctypes.wstring_at(
                    spi.ImageName.Buffer, spi.ImageName.Length // 2
                )
            # A process whose threads all wait for the "suspended" reason is
            # the Windows equivalent of psutil's "stopped" state.
            status = "running"
            nthreads = spi.NumberOfThreads
            if nthreads:
                suspended = 0
                taddr = base + offset + _SPI_SIZE
                # Never read past this entry (or the buffer for the last one)
                span = (spi.NextEntryOffset or (buf_len - offset)) - _SPI_SIZE
                ncheck = min(nthreads, max(0, span // _STI_SIZE))
                for i in range(ncheck):
                    sti = _SystemThreadInformation.from_address(taddr + i * _STI_SIZE)
                    if sti.ThreadState == 5 and sti.WaitReason == 5:
                        suspended += 1
                if ncheck == nthreads and suspended == nthreads:
                    status = "stopped"
            elif nthreads == 0 and pid != 4:  # not System
                # Kernel processes with 0 threads (Secure System, MemCompression)
                # are typically not runnable; label them stopped.
                status = "stopped"
            out.append({
                "pid": pid,
                "ppid": spi.InheritedFromUniqueProcessId or 0,
                "name": name,
                "status": status,
                "threads": nthreads,
                "handles": spi.HandleCount,
                # Task Manager's "Memory" column is the *private* working set;
                # total working set double-counts shared DLL pages and reads
                # much higher than the value users see in Task Manager.
                "ws": (spi.WorkingSetPrivateSize if spi.WorkingSetPrivateSize > 0
                       else spi.WorkingSetSize),
                "priv": spi.PrivatePageCount,
                "cpu_time": (spi.UserTime + spi.KernelTime) / 10_000_000.0,
                "read_bytes": spi.ReadTransferCount,
                "write_bytes": spi.WriteTransferCount,
                "create_time": (
                    spi.CreateTime / 10_000_000.0 - 11_644_473_600.0
                    if spi.CreateTime > 0 else 0.0
                ),
            })
            if spi.NextEntryOffset == 0:
                break
            offset += spi.NextEntryOffset
        return out

    # Validate the struct layout once: a sane snapshot must contain the
    # current process and a plausible total.
    _probe = _win_process_snapshot()
    if _probe and len(_probe) > 8 and any(
        pr["pid"] == os.getpid() and pr["name"] for pr in _probe
    ):
        _WIN_SNAPSHOT_OK = True
    del _probe
except Exception:  # pragma: no cover - non-Windows / hardened systems
    _WIN_SNAPSHOT_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# Metric packet (written by the background thread, read by the UI thread)
# ─────────────────────────────────────────────────────────────────────────────
class MetricPacket:
    def __init__(self):
        self.ts = time.time()
        # CPU
        self.cpu_pct = 0.0
        self.cpu_freq = 0.0
        self.cpu_freq_max = 0.0
        self.cpu_cores = []
        self.cpu_temp = 0.0
        self.cpu_proc_count = 0
        self.cpu_thread_count = 0
        self.cpu_ctx_sw = 0
        self.cpu_interrupts = 0
        # Memory
        self.mem_pct = 0.0
        self.mem_used_gb = 0.0
        self.mem_total_gb = 0.0
        self.mem_avail_gb = 0.0
        self.mem_free_gb = 0.0
        self.mem_cached_gb = 0.0
        self.swap_pct = 0.0
        self.swap_used_gb = 0.0
        self.swap_total_gb = 0.0
        self.swap_free_gb = 0.0
        # Disk
        self.disk_pct = 0.0
        self.disk_used_gb = 0.0
        self.disk_free_gb = 0.0
        self.disk_total_gb = 0.0
        self.disk_read_mbps = 0.0
        self.disk_write_mbps = 0.0
        self.disks = []
        # Network
        self.net_sent_kbps = 0.0
        self.net_recv_kbps = 0.0
        self.net_total_sent_mb = 0.0
        self.net_total_recv_mb = 0.0
        self.nics = []
        # GPU
        self.gpu_pct = 0.0
        self.gpu_mem_pct = 0.0
        self.gpu_temp = 0.0
        self.gpu_mem_used_mb = 0.0
        self.gpu_mem_total_mb = 0.0
        self.gpu_fan_pct = 0.0
        # Processes
        self.processes = []
        # System
        self.num_logical_cpus = 1
        self.uptime_s = 0.0
        self.has_battery = False
        self.battery_pct = 0.0
        self.battery_plugged = False
        # XY series
        self.cpu_xy = ([], [])
        self.mem_xy = ([], [])
        self.swap_xy = ([], [])
        self.disk_xy = ([], [])
        self.disk_read_xy = ([], [])
        self.disk_write_xy = ([], [])
        self.net_sent_xy = ([], [])
        self.net_recv_xy = ([], [])
        self.gpu_xy = ([], [])
        self.cpu_cores_xy = []


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────
class SystemMonitorApp:

    # ── Init ─────────────────────────────────────────────────────────────────
    def __init__(self):
        self._num_cpus = psutil.cpu_count(logical=True) or 1
        self._display_cores = min(self._num_cpus, MAX_CORES_DISPLAY)
        self._boot_time = 0.0
        try:
            self._boot_time = psutil.boot_time()
        except Exception:
            pass

        # Plot X values are *relative* seconds since this base.  ImPlot stores
        # axis bounds in float32; raw epoch seconds (~1.8e9) exhaust its
        # precision and collapse a 32 s window into a single value, which makes
        # every graph render blank.  Relative seconds stay tiny and exact.
        self._plot_t0 = time.time()

        # Rolling history
        self._cpu_dq = deque(maxlen=MAX_DATA_POINTS)
        self._mem_dq = deque(maxlen=MAX_DATA_POINTS)
        self._swap_dq = deque(maxlen=MAX_DATA_POINTS)
        self._disk_dq = deque(maxlen=MAX_DATA_POINTS)
        self._disk_rd_dq = deque(maxlen=MAX_DATA_POINTS)
        self._disk_wr_dq = deque(maxlen=MAX_DATA_POINTS)
        self._net_sent_dq = deque(maxlen=MAX_DATA_POINTS)
        self._net_recv_dq = deque(maxlen=MAX_DATA_POINTS)
        self._gpu_dq = deque(maxlen=MAX_DATA_POINTS)
        self._core_dqs = [deque(maxlen=MAX_DATA_POINTS) for _ in range(self._num_cpus)]

        # Thread state
        self._lock = threading.Lock()
        self._latest = MetricPacket()
        self._new_data = False
        self._bg_thread = None
        self._running = False
        self._stop_event = threading.Event()

        # Delta tracking
        self._prev_net = None
        self._prev_net_t = None
        self._prev_disk_io = None
        self._prev_disk_t = None

        # Process table
        self._proc_sort_col = "cpu_pct_norm"
        self._proc_sort_asc = False
        # Column index -> field name.  Index 0 is the icon column and is not
        # sortable; DearPyGui reports the clicked column in the table callback.
        self._proc_sort_keys = {
            0: None, 1: "name", 2: "pid", 3: "status", 4: "cpu_pct_norm",
            5: "memory_percent", 6: "mem_mb", 7: "num_threads", 8: "username",
        }
        self._proc_sort_labels = {
            "name": "Name", "pid": "PID", "status": "Status",
            "cpu_pct_norm": "CPU", "memory_percent": "Memory %",
            "mem_mb": "Memory", "num_threads": "Threads", "username": "User",
        }
        self._proc_sort_from_label = {
            v: k for k, v in self._proc_sort_labels.items()
        }
        self._proc_filter = ""
        self._all_procs = []
        self._proc_rows = []
        self._row_pids = []
        self._updating_rows = False
        self._username_cache = {}
        self._cache_clean_t = time.monotonic()
        self._prev_proc_cpu = {}
        self._prev_proc_t = 0.0
        self._mem_total_bytes = 0

        # Stable live ordering.  Cell values update every sample, but the row
        # order only changes every few seconds so the top apps stop jumping.
        self._proc_display_order = []
        self._proc_last_sort_t = 0.0
        self._proc_force_sort = True
        self._proc_cpu_smooth = {}
        self._smooth_clean_t = time.monotonic()

        # App icons (lazy, cached by executable path).
        self._exe_cache = {}
        self._desc_cache = {}
        self._icon_cache = {}
        self._icon_pending = []
        self._icon_fallback = None
        self._icon_registry = 0

        # Grouped (Task Manager style) process view.
        self._expanded_groups = set()
        self._selected_pids = set()
        self._selected_group = None
        self._pending_end_pids = []
        self._row_entries = []
        self._display_keys = []
        self._all_group_keys = set()
        self._last_row_click_t = 0.0
        self._last_row_click_idx = -1

        # Disk / NIC row pools
        self._disk_rows = {}
        self._nic_rows = {}

        # Animation state
        self._anim = {}
        self._last_text = {}
        self._last_color = {}
        self._axis_cur = {}
        self._axis_target = {}
        self._axis_expected = {}    # axis tag -> last limits we programmatically set
        self._user_zoom = {}        # axis tag -> True once the user zooms it
        self._chart_axes = {}       # chart tag -> (x_tag, y_tag)
        self._auto_y_axes = set()   # Y axis tags that auto-scale (net/disk)
        self._bar_classes = {}

        # DB
        self._conn = None
        self._db_lock = threading.Lock()
        self._db_buffer = []
        self._last_flush = time.monotonic()
        self._last_prune = 0.0
        self._db_count = 0
        self._db_size = 0
        self._db_stats_t = 0.0

        # GPU / battery caches
        self._gpu_tick = 0
        self._gpu_cache = None
        self._batt_cache = None
        self._batt_tick = 0
        self._has_gpu = False

        # UI misc
        self._fonts = {}
        self._fps = 0.0
        self._fps_frames = 0
        self._fps_t = time.monotonic()
        self._sample_count = 0
        self._toast_until = 0.0
        self._last_size = None
        self._chart_tags = []
        self._window_span = 30.0

        # Themes (created after dpg.create_context)
        self._card_theme = None
        self._chart_theme = None
        self._strip_theme = None
        self._plot_theme = None
        self._pb_themes = []
        self._series_themes = {}
        self._btn_primary = None
        self._btn_danger = None
        self._btn_ghost = None
        self._btn_active = None
        self._btn_pause = None
        self._btn_resume = None
        self._sort_btns = {}
        self._text_themes = {}

        self._sys = self._one_time_sysinfo()
        self._specs = self._collect_system_specs()
        self._has_gpu = bool(self._sys.get("has_gpu"))
        self._init_db()
        self._build_ui()

    # ── One-time system info ─────────────────────────────────────────────────
    @staticmethod
    def _one_time_sysinfo():
        u = platform.uname()
        log = psutil.cpu_count(logical=True) or 1
        phy = psutil.cpu_count(logical=False) or log
        cpu_name = (_cpu_model_name() if IS_LINUX else platform.processor()) \
            or f"{phy}-core Processor"
        cpu_name = _short(cpu_name, 56)
        try:
            ram = round(psutil.virtual_memory().total / 1024 ** 3, 1)
        except Exception:
            ram = 0.0
        sys_drive = DEFAULT_SYS_DRIVE if IS_LINUX else \
            (os.environ.get("SystemDrive") or "C:") + "\\"
        try:
            disk_t = round(psutil.disk_usage(sys_drive).total / 1024 ** 3, 1)
        except Exception:
            disk_t = 0.0
        gpu_name = "No GPU detected"
        has_gpu = False
        if GPU_LIB:
            try:
                gs = GPUtil.getGPUs()
                if gs:
                    has_gpu = True
                    gpu_name = gs[0].name
                    if len(gs) > 1:
                        gpu_name += f" (+{len(gs) - 1})"
            except Exception:
                pass
        try:
            freq = psutil.cpu_freq()
            max_ghz = f"{freq.max / 1000:.2f} GHz" if freq and freq.max else "N/A"
        except Exception:
            max_ghz = "N/A"
        return dict(
            os=f"{u.system} {u.release}",
            node=u.node,
            arch=platform.machine(),
            cpu_name=cpu_name,
            cpu_log=log,
            cpu_phys=phy,
            cpu_max_freq=max_ghz,
            ram=ram,
            disk_t=disk_t,
            sys_drive=sys_drive,
            gpu=gpu_name,
            has_gpu=has_gpu,
        )

    def _collect_system_specs(self):
        """Detailed hardware/system inventory for the Specs tab."""
        s = self._sys
        if IS_LINUX:
            return _collect_linux_specs(s)

        def reg(root, path, name):
            try:
                with winreg.OpenKey(root, path) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    if isinstance(value, (list, tuple)):
                        return ", ".join(str(x) for x in value)
                    return str(value).strip()
            except Exception:
                return ""

        hklm = winreg.HKEY_LOCAL_MACHINE
        cv = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        product = reg(hklm, cv, "ProductName") or s["os"]
        display = reg(hklm, cv, "DisplayVersion")
        build = reg(hklm, cv, "CurrentBuild")
        ubr = reg(hklm, cv, "UBR")
        os_line = product
        if display:
            os_line += f" {display}"
        if build:
            os_line += f" (build {build}" + (f".{ubr}" if ubr else "") + ")"

        cp = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        cpu_name = reg(hklm, cp, "ProcessorNameString") or s["cpu_name"]
        vendor = reg(hklm, cp, "VendorIdentifier")
        mhz = reg(hklm, cp, "~MHz")
        cpu_speed = ""
        if mhz:
            try:
                cpu_speed = f"{int(mhz) / 1000:.2f} GHz"
            except ValueError:
                cpu_speed = mhz
        bios = r"HARDWARE\DESCRIPTION\System\BIOS"
        maker = reg(hklm, bios, "SystemManufacturer")
        model = reg(hklm, bios, "SystemProductName")
        bios_v = reg(hklm, bios, "BIOSVersion")
        bios_d = reg(hklm, bios, "BIOSReleaseDate")

        vm = psutil.virtual_memory()
        modules = []
        try:
            import win32com.client
            wmi = win32com.client.GetObject(
                "winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2")
            for m in wmi.InstancesOf("Win32_PhysicalMemory"):
                cap = int(getattr(m, "Capacity", 0) or 0) / 1024 ** 3
                speed = getattr(m, "Speed", 0) or 0
                man = str(getattr(m, "Manufacturer", "") or "").strip()
                part = str(getattr(m, "PartNumber", "") or "").strip()
                line = f"{cap:.0f} GB"
                if man and man.lower() not in ("unknown", "none"):
                    line += f"  {man}"
                if part and part.lower() not in ("unknown", "none"):
                    line += f"  {part}"
                if speed:
                    line += f"  @ {speed} MT/s"
                modules.append(line)
        except Exception:
            pass

        gpus = []
        try:
            import win32com.client
            wmi = win32com.client.GetObject(
                "winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2")
            for g in wmi.InstancesOf("Win32_VideoController"):
                name = str(getattr(g, "Name", "") or "").strip()
                if not name:
                    continue
                line = name
                vram = int(getattr(g, "AdapterRAM", 0) or 0) / 1024 ** 3
                if vram >= 0.9:
                    line += f"  •  {vram:.1f} GB VRAM"
                drv = str(getattr(g, "DriverVersion", "") or "").strip()
                if drv:
                    line += f"  •  driver {drv}"
                gpus.append(line)
        except Exception:
            pass
        if not gpus and s.get("has_gpu"):
            gpus.append(s["gpu"])

        storage = []
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(part.mountpoint)
                except Exception:
                    continue
                total = u.total / 1024 ** 3
                used = u.used / 1024 ** 3
                free = u.free / 1024 ** 3
                storage.append((
                    part.mountpoint,
                    f"{used:,.1f} / {total:,.1f} GB used  "
                    f"•  {free:,.1f} GB free  •  {part.fstype or '?'}",
                ))
        except Exception:
            pass

        network = []
        try:
            for name, addrs in psutil.net_if_addrs().items():
                if name.lower().startswith("loopback"):
                    continue
                mac = next((a.address for a in addrs
                            if a.family == psutil.AF_LINK), "")
                ip = next((a.address for a in addrs
                           if a.family == psutil.AF_INET
                           and not a.address.startswith("127.")), "")
                if ip or mac:
                    network.append((name, f"{ip or 'No IPv4'}  •  {mac or 'No MAC'}"))
        except Exception:
            pass

        try:
            boot = datetime.datetime.fromtimestamp(psutil.boot_time())
            boot_line = boot.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            boot_line = "—"
        battery = psutil.sensors_battery()
        if battery:
            battery_line = (f"{battery.percent:.0f}%  "
                            f"({'charging' if battery.power_plugged else 'on battery'})")
        else:
            battery_line = "Not present"

        try:
            w = win32api.GetSystemMetrics(0) if win32api else 0
            h = win32api.GetSystemMetrics(1) if win32api else 0
        except Exception:
            w = h = 0
        if not w or not h:
            try:
                w, h = dpg.get_viewport_width(), dpg.get_viewport_height()
            except Exception:
                w = h = 0

        return [
            ("System", [
                ("Device", s["node"]),
                ("Operating system", os_line),
                ("Architecture", s["arch"]),
                ("Manufacturer", maker or "—"),
                ("Model", model or "—"),
                ("BIOS", (bios_v or "—") + (f"  •  {bios_d}" if bios_d else "")),
                ("Booted", boot_line),
                ("Battery", battery_line),
            ]),
            ("Processor", [
                ("CPU", cpu_name),
                ("Vendor", vendor or "—"),
                ("Cores / threads", f"{s['cpu_phys']} physical  •  {s['cpu_log']} logical"),
                ("Rated speed", cpu_speed or s["cpu_max_freq"]),
            ]),
            ("Memory", [
                ("Installed RAM", f"{vm.total / 1024 ** 3:.2f} GB"),
                ("Available", f"{vm.available / 1024 ** 3:.2f} GB"),
                ("Modules", "\n".join(modules) if modules else "Details unavailable"),
            ]),
            ("Storage", storage or [(s["sys_drive"], f"{s['disk_t']:,.1f} GB")]),
            ("Graphics", [(f"GPU {i + 1}", g) for i, g in enumerate(gpus)]
                         or [("GPU", s["gpu"])]),
            ("Network", network or [("Adapters", "Not detected")]),
            ("Display", [
                ("Primary resolution", f"{w} × {h}" if w and h else "—"),
                ("DPI awareness", "Per-monitor aware"),
            ]),
        ]

    # ── Database ─────────────────────────────────────────────────────────────
    def _init_db(self):
        self._conn = None
        try:
            self._conn = duckdb.connect(DB_PATH)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS system_metrics "
                "(ts TIMESTAMP, metric_name VARCHAR, value DOUBLE)"
            )
            try:
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sm_ts ON system_metrics(ts)"
                )
            except Exception:
                pass
            self._prune_db(force=True)
        except Exception as e:
            print(f"[DB] init: {e}")
            self._conn = None

    def _prune_db(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_prune < 600:
            return
        self._last_prune = now
        cutoff = datetime.datetime.now() - datetime.timedelta(days=DB_RETENTION_DAYS)
        with self._db_lock:
            if not self._conn:
                return
            try:
                self._conn.execute(
                    "DELETE FROM system_metrics WHERE ts < ?", [cutoff]
                )
            except Exception:
                pass

    def _flush_db(self):
        with self._db_lock:
            if not self._conn or not self._db_buffer:
                self._last_flush = time.monotonic()
                return
            rows, self._db_buffer = self._db_buffer, []
            try:
                self._conn.executemany(
                    "INSERT INTO system_metrics VALUES (?,?,?)", rows
                )
            except Exception as e:
                print(f"[DB] write: {e}")
            self._last_flush = time.monotonic()

    def _db_query(self, sql):
        with self._db_lock:
            if not self._conn:
                raise RuntimeError("Database is unavailable")
            cur = self._conn.execute(sql)
            rows = cur.fetchall()
            desc = cur.description or []
        return rows, desc

    # ── Metric collection ────────────────────────────────────────────────────
    def _collect(self) -> MetricPacket:
        now_dt = datetime.datetime.now()
        ts = now_dt.timestamp()
        p = MetricPacket()
        p.ts = ts
        p.num_logical_cpus = self._num_cpus

        # CPU
        try:
            p.cpu_pct = psutil.cpu_percent(interval=None)
            cores = list(psutil.cpu_percent(percpu=True, interval=None))
            if len(cores) < self._num_cpus:
                cores += [0.0] * (self._num_cpus - len(cores))
            p.cpu_cores = cores[: self._num_cpus]
            f = psutil.cpu_freq()
            if f:
                p.cpu_freq = f.current or 0.0
                p.cpu_freq_max = f.max or 0.0
        except Exception:
            p.cpu_cores = [0.0] * self._num_cpus
        try:
            st = psutil.cpu_stats()
            p.cpu_ctx_sw = st.ctx_switches
            p.cpu_interrupts = st.interrupts
        except Exception:
            pass
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "CPU"):
                    if key in temps and temps[key]:
                        p.cpu_temp = temps[key][0].current or 0.0
                        break
        except Exception:
            pass

        # Memory
        try:
            m = psutil.virtual_memory()
            p.mem_pct = m.percent
            p.mem_used_gb = m.used / 1024 ** 3
            p.mem_total_gb = m.total / 1024 ** 3
            self._mem_total_bytes = m.total
            p.mem_avail_gb = m.available / 1024 ** 3
            p.mem_free_gb = max(0.0, p.mem_total_gb - p.mem_used_gb)
            p.mem_cached_gb = getattr(m, "cached", 0) / 1024 ** 3
        except Exception:
            pass
        try:
            sw = psutil.swap_memory()
            p.swap_pct = sw.percent
            p.swap_used_gb = sw.used / 1024 ** 3
            p.swap_total_gb = sw.total / 1024 ** 3
            p.swap_free_gb = max(0.0, p.swap_total_gb - p.swap_used_gb)
        except Exception:
            pass

        # Disk partitions
        try:
            parts = []
            for pt in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(pt.mountpoint)
                    parts.append(dict(
                        mount=pt.mountpoint,
                        device=pt.device,
                        fstype=pt.fstype or "?",
                        total_gb=u.total / 1024 ** 3,
                        used_gb=u.used / 1024 ** 3,
                        free_gb=u.free / 1024 ** 3,
                        pct=u.percent,
                    ))
                except (PermissionError, OSError):
                    pass
            p.disks = parts
            sys_drive = self._sys.get("sys_drive", DEFAULT_SYS_DRIVE)
            chosen = next(
                (d for d in parts if d["mount"].lower().rstrip("\\") ==
                 sys_drive.lower().rstrip("\\")),
                parts[0] if parts else None,
            )
            if chosen:
                p.disk_pct = chosen["pct"]
                p.disk_used_gb = chosen["used_gb"]
                p.disk_free_gb = chosen["free_gb"]
                p.disk_total_gb = chosen["total_gb"]
        except Exception:
            pass

        # Disk I/O
        try:
            now_t = time.monotonic()
            dio = psutil.disk_io_counters()
            if self._prev_disk_io and dio:
                dt = now_t - self._prev_disk_t
                if dt > 0:
                    p.disk_read_mbps = max(
                        0.0, (dio.read_bytes - self._prev_disk_io.read_bytes) / dt / 1024 ** 2
                    )
                    p.disk_write_mbps = max(
                        0.0, (dio.write_bytes - self._prev_disk_io.write_bytes) / dt / 1024 ** 2
                    )
            self._prev_disk_io = dio
            self._prev_disk_t = now_t
        except Exception:
            pass

        # Network
        try:
            n = psutil.net_io_counters()
            now_t = time.monotonic()
            if self._prev_net and n:
                dt = now_t - self._prev_net_t
                if dt > 0:
                    p.net_sent_kbps = max(
                        0.0, (n.bytes_sent - self._prev_net.bytes_sent) / dt / 1024
                    )
                    p.net_recv_kbps = max(
                        0.0, (n.bytes_recv - self._prev_net.bytes_recv) / dt / 1024
                    )
            self._prev_net, self._prev_net_t = n, now_t
            if n:
                p.net_total_sent_mb = n.bytes_sent / 1024 ** 2
                p.net_total_recv_mb = n.bytes_recv / 1024 ** 2
            nics_stats = psutil.net_if_stats()
            nics = []
            for name, io in psutil.net_io_counters(pernic=True).items():
                st = nics_stats.get(name)
                nics.append(dict(
                    name=name,
                    is_up=bool(st.isup) if st else False,
                    speed=st.speed if st else 0,
                    sent_mb=io.bytes_sent / 1024 ** 2,
                    recv_mb=io.bytes_recv / 1024 ** 2,
                    pkts=io.packets_sent + io.packets_recv,
                    errors=io.errin + io.errout + io.dropin + io.dropout,
                ))
            nics.sort(key=lambda x: (not x["is_up"], x["name"].lower()))
            p.nics = nics
        except Exception:
            pass

        # GPU (probe every 5 seconds at most)
        self._gpu_tick += 1
        if GPU_LIB and self._has_gpu and (self._gpu_cache is None or self._gpu_tick % 5 == 0):
            try:
                gs = GPUtil.getGPUs()
                if gs:
                    g = gs[0]
                    self._gpu_cache = dict(
                        pct=(g.load or 0) * 100,
                        mem_pct=(g.memoryUtil or 0) * 100,
                        temp=g.temperature or 0,
                        mem_used=g.memoryUsed or 0,
                        mem_total=g.memoryTotal or 0,
                        fan=getattr(g, "fan", 0) or 0,
                    )
            except Exception:
                pass
        if self._gpu_cache:
            p.gpu_pct = self._gpu_cache["pct"]
            p.gpu_mem_pct = self._gpu_cache["mem_pct"]
            p.gpu_temp = self._gpu_cache["temp"]
            p.gpu_mem_used_mb = self._gpu_cache["mem_used"]
            p.gpu_mem_total_mb = self._gpu_cache["mem_total"]
            p.gpu_fan_pct = self._gpu_cache["fan"]

        # Processes (one kernel snapshot - milliseconds for the whole system)
        procs = []
        tcount = 0
        try:
            if _WIN_SNAPSHOT_OK:
                procs, tcount = self._collect_procs_snapshot()
            else:
                procs, tcount = self._collect_procs_psutil()
        except Exception:
            procs, tcount = [], 0
        p.processes = procs
        p.cpu_proc_count = len(procs)
        p.cpu_thread_count = tcount

        # System / battery
        try:
            p.uptime_s = max(0.0, time.time() - self._boot_time)
        except Exception:
            pass
        self._batt_tick += 1
        if self._batt_cache is None or self._batt_tick % 30 == 0:
            try:
                self._batt_cache = psutil.sensors_battery()
            except Exception:
                self._batt_cache = False
        b = self._batt_cache
        if b and b is not False:
            p.has_battery = True
            p.battery_pct = b.percent
            p.battery_plugged = bool(b.power_plugged)

        # History deques - use relative seconds (float32-safe for ImPlot)
        rt = ts - self._plot_t0
        self._cpu_dq.append((rt, p.cpu_pct))
        self._mem_dq.append((rt, p.mem_pct))
        self._swap_dq.append((rt, p.swap_pct))
        self._disk_dq.append((rt, p.disk_pct))
        self._disk_rd_dq.append((rt, p.disk_read_mbps))
        self._disk_wr_dq.append((rt, p.disk_write_mbps))
        self._net_sent_dq.append((rt, p.net_sent_kbps))
        self._net_recv_dq.append((rt, p.net_recv_kbps))
        self._gpu_dq.append((rt, p.gpu_pct))
        for i, v in enumerate(p.cpu_cores[: self._num_cpus]):
            self._core_dqs[i].append((rt, v))

        p.cpu_xy = self._xy(self._cpu_dq)
        p.mem_xy = self._xy(self._mem_dq)
        p.swap_xy = self._xy(self._swap_dq)
        p.disk_xy = self._xy(self._disk_dq)
        p.disk_read_xy = self._xy(self._disk_rd_dq)
        p.disk_write_xy = self._xy(self._disk_wr_dq)
        p.net_sent_xy = self._xy(self._net_sent_dq)
        p.net_recv_xy = self._xy(self._net_recv_dq)
        p.gpu_xy = self._xy(self._gpu_dq)
        p.cpu_cores_xy = [self._xy(dq) for dq in self._core_dqs]

        # Buffer DB rows; the background loop flushes them in batches
        if self._conn:
            rows = [
                (now_dt, "cpu_percent", p.cpu_pct),
                (now_dt, "cpu_freq_mhz", p.cpu_freq),
                (now_dt, "cpu_temp_c", p.cpu_temp),
                (now_dt, "mem_percent", p.mem_pct),
                (now_dt, "mem_used_gb", p.mem_used_gb),
                (now_dt, "swap_percent", p.swap_pct),
                (now_dt, "disk_percent", p.disk_pct),
                (now_dt, "disk_read_mbps", p.disk_read_mbps),
                (now_dt, "disk_write_mbps", p.disk_write_mbps),
                (now_dt, "net_sent_kbps", p.net_sent_kbps),
                (now_dt, "net_recv_kbps", p.net_recv_kbps),
                (now_dt, "gpu_percent", p.gpu_pct),
                (now_dt, "gpu_temp_c", p.gpu_temp),
                (now_dt, "gpu_mem_pct", p.gpu_mem_pct),
            ]
            with self._db_lock:
                self._db_buffer.extend(rows)

        # Occasionally trim the username cache
        if time.monotonic() - self._cache_clean_t > 300:
            self._cache_clean_t = time.monotonic()
            live = {pr["pid"] for pr in procs}
            self._username_cache = {
                k: v for k, v in self._username_cache.items() if k in live
            }

        return p

    # ── Process collection ───────────────────────────────────────────────────
    def _collect_procs_snapshot(self):
        """Build the process list from a single kernel snapshot."""
        snap = _win_process_snapshot()
        if not snap:
            return self._collect_procs_psutil()

        now_t = time.monotonic()
        dt = now_t - self._prev_proc_t
        if not (0.05 <= dt <= 10.0):
            dt = SAMPLE_INTERVAL_S
        self._prev_proc_t = now_t

        prev = self._prev_proc_cpu
        cur = {}
        total_mem = float(self._mem_total_bytes or 0) or 1.0
        ncpu = float(self._num_cpus or 1)
        uname_cache = self._username_cache
        procs = []
        tcount = 0
        # Resolving owner names costs a handle per process; spread it over a
        # few cycles instead of stalling the first sample.
        uname_budget = 80

        for sp in snap:
            pid = sp["pid"]
            name = sp["name"] or ""
            if pid == 0 or name.lower() == "system idle process":
                continue
            ct = sp["cpu_time"]
            base = prev.get(pid)
            cur[pid] = ct
            # Percent of one core, then normalised to the whole machine so it
            # matches Task Manager's "CPU %" column.
            one_core = ((ct - base) / dt * 100.0) if base is not None else 0.0
            one_core = max(0.0, min(one_core, ncpu * 100.0))
            ws = sp["ws"]
            uname = uname_cache.get(pid)
            if uname is None:
                if uname_budget > 0:
                    uname = self._lookup_username(pid)
                    uname_cache[pid] = uname
                    uname_budget -= 1
                else:
                    uname = ""
            procs.append(dict(
                pid=pid,
                ppid=sp["ppid"],
                name=name or "?",
                status=sp["status"],
                cpu_pct=one_core,
                cpu_pct_norm=one_core / ncpu,
                memory_percent=ws / total_mem * 100.0,
                mem_mb=ws / 1048576.0,
                num_threads=sp["threads"],
                handles=sp["handles"],
                priv_mb=sp["priv"] / 1048576.0,
                io_mb=(sp["read_bytes"] + sp["write_bytes"]) / 1048576.0,
                started=sp["create_time"],
                username=uname,
            ))
            tcount += sp["threads"] or 1

        self._prev_proc_cpu = cur

        # Fallback: for kernel processes with empty names in the snapshot,
        # use psutil's names (cached once).
        if not hasattr(self, "_ps_name_cache"):
            self._ps_name_cache = {}
        for p in procs:
            if not p.get("name"):
                pid = p["pid"]
                if pid in self._ps_name_cache:
                    p["name"] = self._ps_name_cache[pid]
                else:
                    try:
                        self._ps_name_cache[pid] = psutil.Process(pid).name()
                        p["name"] = self._ps_name_cache[pid]
                    except Exception:
                        p["name"] = ""
        return procs, tcount

    def _collect_procs_psutil(self):
        """Portable fallback (also used if the snapshot layout is unexpected)."""
        attrs = ["pid", "name", "status", "cpu_percent", "memory_percent",
                 "memory_info", "num_threads"]
        procs = []
        tcount = 0
        uname_cache = self._username_cache
        for proc in psutil.process_iter(attrs):
            try:
                info = proc.info
                pid = info.get("pid", 0)
                name = info.get("name") or ""
                if pid == 0 or name.lower() == "system idle process":
                    continue
                mi = info.get("memory_info")
                uname = uname_cache.get(pid)
                if uname is None:
                    try:
                        uname = proc.username() or ""
                    except Exception:
                        uname = ""
                    uname_cache[pid] = uname
                threads = info.get("num_threads") or 0
                cpu = max(0.0, info.get("cpu_percent") or 0.0)
                procs.append(dict(
                    pid=pid,
                    ppid=0,
                    name=name or "?",
                    status=info.get("status") or "?",
                    cpu_pct=cpu,
                    cpu_pct_norm=cpu / self._num_cpus,
                    memory_percent=info.get("memory_percent") or 0.0,
                    mem_mb=(mi.rss if mi else 0) / 1024 ** 2,
                    num_threads=threads,
                    handles=0,
                    priv_mb=(mi.private if mi else 0) / 1024 ** 2,
                    io_mb=0.0,
                    started=0.0,
                    username=uname,
                ))
                tcount += threads or 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return procs, tcount

    @staticmethod
    def _lookup_username(pid):
        try:
            return psutil.Process(pid).username() or ""
        except Exception:
            return ""

    @staticmethod
    def _xy(dq):
        if not dq:
            return [], []
        data = list(dq)
        return [d[0] for d in data], [d[1] for d in data]

    # ── Background thread ────────────────────────────────────────────────────
    def _bg_loop(self):
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(percpu=True, interval=None)
            # Seed the per-process CPU-time baseline one cycle early. Without
            # this the first packet has no previous counters to diff against,
            # so every CPU cell reads 0.0% and sorting looks meaningless.
            try:
                self._collect_procs_snapshot()
            except Exception:
                pass
            self._stop_event.wait(0.9)
            while not self._stop_event.is_set():
                t0 = time.monotonic()
                try:
                    packet = self._collect()
                    if _env("PROFILE"):
                        print(f"[collect] {time.monotonic() - t0:.3f}s")
                    with self._lock:
                        self._latest = packet
                        self._new_data = True
                except Exception as e:
                    print(f"[BG] {e}")
                if time.monotonic() - self._last_flush >= DB_FLUSH_INTERVAL:
                    self._flush_db()
                self._prune_db()
                remaining = SAMPLE_INTERVAL_S - (time.monotonic() - t0)
                if remaining > 0:
                    self._stop_event.wait(remaining)
        finally:
            self._flush_db()

    # ─────────────────────────────────────────────────────────────────────────
    # Apply packet -> UI (always on the main thread)
    # ─────────────────────────────────────────────────────────────────────────
    def _apply_packet(self, p: MetricPacket):
        self._all_procs = p.processes
        self._sample_count += 1
        # Start with a tight window so the first points are immediately visible,
        # then widen toward WINDOW_SECONDS as history accumulates.
        n = len(self._cpu_dq)
        self._window_span = min(float(WINDOW_SECONDS), max(8.0, n * 1.5))

        # ── Overview cards ────────────────────────────────────────────────────
        self._anim_bar("ov_cpu_bar", p.cpu_pct)
        self._anim_bar("ov_mem_bar", p.mem_pct)
        self._anim_bar("ov_disk_bar", p.disk_pct)
        self._anim_bar("ov_gpu_bar", p.gpu_pct)
        self._anim_text("ov_cpu_val", p.cpu_pct, lambda v: f"{v:.1f}%")
        self._anim_text("ov_mem_val", p.mem_pct, lambda v: f"{v:.1f}%")
        self._anim_text("ov_disk_val", p.disk_pct, lambda v: f"{v:.1f}%")
        self._anim_text("ov_gpu_val", p.gpu_pct, lambda v: f"{v:.1f}%")
        self._text("ov_cpu_sub",
                   f"{p.cpu_freq:.0f} MHz  •  {p.cpu_proc_count} processes")
        self._text("ov_mem_sub",
                   f"{p.mem_used_gb:.1f} / {p.mem_total_gb:.1f} GB used")
        self._text("ov_disk_sub",
                   f"R {p.disk_read_mbps:.1f}  •  W {p.disk_write_mbps:.1f} MB/s")
        if self._has_gpu:
            self._text("ov_gpu_sub",
                       f"{p.gpu_temp:.0f} °C  •  VRAM {p.gpu_mem_pct:.0f}%")
        else:
            self._text("ov_gpu_sub", "No compatible GPU detected")
        self._text("ov_net_up", f"↑ {_fmt_speed(p.net_sent_kbps)}")
        self._text("ov_net_down", f"↓ {_fmt_speed(p.net_recv_kbps)}")
        self._text("ov_proc_lbl",
                   f"{p.cpu_proc_count} processes  •  {p.cpu_thread_count} threads")
        if p.has_battery:
            plug = "charging" if p.battery_plugged else "on battery"
            self._text("ov_batt_lbl", f"Battery {p.battery_pct:.0f}% ({plug})")
        else:
            self._text("ov_batt_lbl", "")

        # ── Overview charts ───────────────────────────────────────────────────
        self._set_series("ov_cpu_s", p.cpu_xy)
        self._set_series("ov_mem_s", p.mem_xy)
        self._set_series("ov_disk_s", p.disk_xy)
        self._set_series("ov_net_s1", p.net_sent_xy)
        self._set_series("ov_net_s2", p.net_recv_xy)
        self._set_series("ov_gpu_s", p.gpu_xy)
        for x_tag in ("ov_xax_cpu", "ov_xax_mem", "ov_xax_disk",
                      "ov_xax_net", "ov_xax_gpu"):
            self._scroll_axis(x_tag, p.ts)
        self._auto_axis("ov_yax_net", p.net_sent_xy[1] + p.net_recv_xy[1])
        self._text("ov_chart_cpu_val", f"{p.cpu_pct:.1f}%")
        self._text("ov_chart_mem_val", f"{p.mem_pct:.1f}%")
        self._text("ov_chart_disk_val",
                   f"R {p.disk_read_mbps:.1f} • W {p.disk_write_mbps:.1f} MB/s")
        self._text("ov_chart_net_val",
                   f"↑ {_fmt_speed(p.net_sent_kbps)}  ↓ {_fmt_speed(p.net_recv_kbps)}")
        self._text("ov_chart_gpu_val", f"{p.gpu_pct:.1f}%")

        # ── CPU tab ───────────────────────────────────────────────────────────
        self._anim_bar("cpu_overall_bar", p.cpu_pct)
        self._anim_text("cpu_overall_val", p.cpu_pct, lambda v: f"{v:.1f}%")
        self._text("cpu_freq_val", f"{p.cpu_freq:.0f} MHz")
        self._text("cpu_temp_val", f"{p.cpu_temp:.1f} °C" if p.cpu_temp else "N/A")
        self._text("cpu_procs_val", f"{p.cpu_proc_count:,}")
        self._text("cpu_threads_val", f"{p.cpu_thread_count:,}")
        self._text("cpu_ctx_val", f"{p.cpu_ctx_sw:,}")
        self._text("cpu_int_val", f"{p.cpu_interrupts:,}")
        shown = 0
        busy = 0
        total_core = 0.0
        max_core = 0.0
        for i, val in enumerate(p.cpu_cores[: self._display_cores]):
            if dpg.does_item_exist(f"cpu_core_{i}_bar"):
                self._anim_bar(f"cpu_core_{i}_bar", val, rate=16)
                self._text(f"cpu_core_{i}_lbl", f"{val:.0f}%")
                shown += 1
                total_core += val
                max_core = max(max_core, val)
                if val >= 50:
                    busy += 1
        if shown:
            self._text("cpu_core_summary",
                       f"Busy {busy}/{shown}  •  Avg {total_core / shown:.0f}%  "
                       f"•  Peak {max_core:.0f}%")
        self._set_series("cpu_series", p.cpu_xy)
        self._scroll_axis("cpu_xax", p.ts)
        self._text("cpu_chart_val", f"{p.cpu_pct:.1f}%")

        # ── Memory tab ────────────────────────────────────────────────────────
        self._anim_bar("mem_phy_bar", p.mem_pct)
        self._anim_text("mem_phy_val", p.mem_pct, lambda v: f"{v:.1f}%")
        self._text("mem_used_val", f"{p.mem_used_gb:.2f} GB")
        self._text("mem_avail_val", f"{p.mem_avail_gb:.2f} GB")
        self._text("mem_free_val", f"{p.mem_free_gb:.2f} GB")
        self._text("mem_total_val", f"{p.mem_total_gb:.2f} GB")
        self._anim_bar("mem_swap_bar", p.swap_pct)
        self._anim_text("mem_swap_val", p.swap_pct, lambda v: f"{v:.1f}%")
        self._text("mem_swap_used", f"{p.swap_used_gb:.2f} GB")
        self._text("mem_swap_free", f"{p.swap_free_gb:.2f} GB")
        self._text("mem_swap_total", f"{p.swap_total_gb:.2f} GB")
        self._set_series("mem_series", p.mem_xy)
        self._set_series("swap_series", p.swap_xy)
        self._scroll_axis("mem_xax", p.ts)
        self._scroll_axis("swap_xax", p.ts)
        self._text("mem_chart_val", f"{p.mem_pct:.1f}%")
        self._text("swap_chart_val", f"{p.swap_pct:.1f}%")

        # ── Disk tab ──────────────────────────────────────────────────────────
        self._anim_text("disk_read_val", p.disk_read_mbps,
                        lambda v: f"{v:.2f} MB/s")
        self._anim_text("disk_write_val", p.disk_write_mbps,
                        lambda v: f"{v:.2f} MB/s")
        self._text("disk_vol_count", f"{len(p.disks)} volume(s)")
        self._update_disk_table(p)
        self._set_series("disk_rd_s", p.disk_read_xy)
        self._set_series("disk_wr_s", p.disk_write_xy)
        self._scroll_axis("disk_xax", p.ts)
        self._auto_axis("disk_yax", p.disk_read_xy[1] + p.disk_write_xy[1])
        self._text("disk_chart_val",
                   f"R {p.disk_read_mbps:.1f}  •  W {p.disk_write_mbps:.1f} MB/s")

        # ── Network tab ───────────────────────────────────────────────────────
        self._anim_text("net_sent_val", p.net_sent_kbps,
                        lambda v: _fmt_speed(v))
        self._anim_text("net_recv_val", p.net_recv_kbps,
                        lambda v: _fmt_speed(v))
        self._text("net_tsent_val", f"{p.net_total_sent_mb:,.1f} MB")
        self._text("net_trecv_val", f"{p.net_total_recv_mb:,.1f} MB")
        self._update_nic_table(p)
        self._set_series("net_sent_s", p.net_sent_xy)
        self._set_series("net_recv_s", p.net_recv_xy)
        self._scroll_axis("net_xax", p.ts)
        self._auto_axis("net_yax", p.net_sent_xy[1] + p.net_recv_xy[1])
        self._text("net_chart_val",
                   f"↑ {_fmt_speed(p.net_sent_kbps)}  ↓ {_fmt_speed(p.net_recv_kbps)}")

        # ── GPU tab ───────────────────────────────────────────────────────────
        if self._has_gpu:
            self._anim_bar("gpu_load_bar", p.gpu_pct)
            self._anim_bar("gpu_mem_bar", p.gpu_mem_pct)
            self._anim_text("gpu_load_val", p.gpu_pct, lambda v: f"{v:.1f}%")
            self._anim_text("gpu_mem_val", p.gpu_mem_pct, lambda v: f"{v:.1f}%")
            self._text("gpu_temp_val", f"{p.gpu_temp:.0f} °C")
            self._text("gpu_fan_val", f"{p.gpu_fan_pct:.0f}%")
            self._text("gpu_mem_detail",
                       f"{p.gpu_mem_used_mb:.0f} / {p.gpu_mem_total_mb:.0f} MB")
            self._set_series("gpu_series", p.gpu_xy)
            self._scroll_axis("gpu_xax", p.ts)
            self._text("gpu_chart_val", f"{p.gpu_pct:.1f}%")

        # ── Header / footer ───────────────────────────────────────────────────
        self._update_header(p)
        self._text("ftr_samples", f"{self._sample_count:,} samples")
        self._update_selected_proc(p)

        # ── Processes ─────────────────────────────────────────────────────────
        self._refresh_proc_table()

    def _update_header(self, p):
        up = int(p.uptime_s)
        d, rem = divmod(up, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d:
            self._text("hdr_uptime", f"Up {d}d {h}h")
        elif h:
            self._text("hdr_uptime", f"Up {h}h {m}m")
        else:
            self._text("hdr_uptime", f"Up {m}m")
        if p.has_battery:
            icon = "AC" if p.battery_plugged else "BAT"
            self._text("hdr_batt", f"{icon} {p.battery_pct:.0f}%")
        else:
            self._text("hdr_batt", "")

    # ─────────────────────────────────────────────────────────────────────────
    # Process table (incremental: rows are reused, never rebuilt every tick)
    # ─────────────────────────────────────────────────────────────────────────
    def _init_icon_textures(self):
        """Create the shared texture registry and the fallback icon once."""
        try:
            with dpg.texture_registry(tag="proc_icon_registry") as reg:
                self._icon_registry = reg or "proc_icon_registry"
                self._icon_fallback = dpg.add_static_texture(
                    ICON_SIZE, ICON_SIZE, _fallback_icon_rgba(),
                    parent=reg,
                )
        except Exception:
            self._icon_fallback = None
            self._icon_registry = 0

    def _exe_path_for(self, pr):
        """Resolve (and cache) the executable path for a process row."""
        pid = int(pr.get("pid") or 0)
        now = time.monotonic()
        cached = self._exe_cache.get(pid)
        if cached and now - cached[1] < EXE_CACHE_TTL:
            return cached[0]
        path = ""
        try:
            path = psutil.Process(pid).exe() or ""
        except Exception:
            pass
        if not path:
            name = pr.get("name") or ""
            if IS_LINUX:
                for folder in ("/usr/bin", "/usr/local/bin", "/bin",
                               "/snap/bin", "/usr/libexec"):
                    candidate = os.path.join(folder, name)
                    if name and os.path.isfile(candidate):
                        path = candidate
                        break
            else:
                win = os.environ.get("WINDIR", r"C:\Windows")
                for folder in ("System32", "SysWOW64"):
                    candidate = os.path.join(win, folder, name)
                    if name and os.path.isfile(candidate):
                        path = candidate
                        break
        self._exe_cache[pid] = (path, now)
        return path

    def _file_description(self, path):
        """Read the executable's real display name."""
        if not path:
            return ""
        key = path.lower()
        if key in self._desc_cache:
            return self._desc_cache[key]
        if IS_LINUX:
            desc = _linux_display_name(os.path.basename(path))
            self._desc_cache[key] = desc
            return desc
        desc = ""
        if win32api is not None:
            try:
                translations = win32api.GetFileVersionInfo(
                    path, "\\VarFileInfo\\Translation")
                candidates = []
                if translations:
                    lang, codepage = translations[0]
                    candidates.append(
                        f"\\StringFileInfo\\{lang:04X}{codepage:04X}\\FileDescription")
                candidates += [
                    "\\StringFileInfo\\040904B0\\FileDescription",
                    "\\StringFileInfo\\040904E4\\FileDescription",
                ]
                for sub in candidates:
                    try:
                        desc = win32api.GetFileVersionInfo(path, sub) or ""
                    except Exception:
                        desc = ""
                    if desc:
                        break
            except Exception:
                desc = ""
        desc = str(desc or "").strip()
        self._desc_cache[key] = desc
        return desc

    def _display_name_for(self, pr):
        """Accurate display name for a process: version info, then fallback."""
        raw = str(pr.get("name") or "?").strip()
        if IS_LINUX:
            return _linux_display_name(raw)
        try:
            path = self._exe_path_for(pr)
        except Exception:
            path = ""
        desc = self._file_description(path) if path else ""
        return desc or _friendly_name(raw)

    def _icon_for_proc(self, pr):
        """Return a texture tag now; queue extraction for a later tick."""
        fallback = self._icon_fallback or 0
        if not fallback:
            return 0
        path = self._exe_path_for(pr)
        if not path:
            return fallback
        key = path.lower()
        tex = self._icon_cache.get(key, 0)
        if tex:
            return tex
        if key not in self._icon_cache:
            self._icon_cache[key] = 0           # mark as attempted
            self._icon_pending.append((key, path))
        return fallback

    def _process_icon_queue(self):
        """Extract at most a handful of icons per tick to keep frames smooth."""
        if not self._icon_pending or not self._icon_registry:
            return
        budget = ICON_EXTRACT_BUDGET
        while self._icon_pending and budget > 0:
            key, path = self._icon_pending.pop(0)
            budget -= 1
            flat = _icon_rgba_from_path(path)
            tex = 0
            if flat:
                try:
                    tex = dpg.add_static_texture(
                        ICON_SIZE, ICON_SIZE, flat, parent=self._icon_registry
                    )
                except Exception:
                    tex = 0
            self._icon_cache[key] = tex or (self._icon_fallback or 0)

    def _text_theme(self, color):
        """Cached text-colour theme for a table selectable cell."""
        key = tuple(color)
        th = self._text_themes.get(key)
        if th is None:
            with dpg.theme() as th:
                with dpg.theme_component(dpg.mvSelectable):
                    dpg.add_theme_color(dpg.mvThemeCol_Text, _l(color))
            self._text_themes[key] = th
        return th

    def _ensure_proc_rows(self, n):
        if not dpg.does_item_exist("proc_table"):
            return
        fallback = self._icon_fallback or 0
        while len(self._proc_rows) < n:
            i = len(self._proc_rows)
            with dpg.table_row(parent="proc_table") as row_tag:
                # IMPORTANT: a table row assigns one child per column, in
                # order.  A span_columns selectable consumes a column of its
                # own, which shifted the icon into "Name" and the name into
                # "PID".  Instead every value cell IS a selectable and column
                # 0 is kept free for the icon + dropdown button.
                with dpg.group(horizontal=True):
                    icon = dpg.add_image(fallback, width=ICON_SIZE,
                                         height=ICON_SIZE)
                    toggle = dpg.add_button(
                        label="▼", callback=self._on_group_toggle, user_data=i,
                        width=26, height=22)
                    dpg.bind_item_theme(toggle, self._btn_ghost)
                    f = self._fonts.get("bold")
                    if f:
                        dpg.bind_item_font(toggle, f)

                sels = []

                def cell(color):
                    s = dpg.add_selectable(
                        label="", callback=self._on_proc_click, user_data=i)
                    dpg.bind_item_theme(s, self._text_theme(color))
                    sels.append(s)
                    return s

                # Name keeps the 20px spacer for child-instance indentation.
                with dpg.group(horizontal=True):
                    child_space = dpg.add_spacer(width=20)
                    name_txt = cell(C_TEXT)
                pid = cell(C_TEXT)
                status = cell(C_DIM)
                cpu = cell(C_GREEN)
                memp = cell(C_TEXT)
                memmb = cell(C_TEXT)
                thr = cell(C_TEXT)
                user = cell(C_DIM)
            self._proc_rows.append(dict(
                row=row_tag, icon=icon, icon_tex=fallback or -1,
                sel=name_txt, sels=sels,
                toggle=toggle, child_space=child_space, name=name_txt,
                pid=pid, status=status, cpu=cpu, memp=memp, memmb=memmb,
                thr=thr, user=user,
                data=None, sel_state=None, cpu_col=None, status_col=None,
            ))
        while len(self._proc_rows) > n:
            r = self._proc_rows.pop()
            try:
                dpg.delete_item(r["row"])
            except Exception:
                pass

    def _group_aggregate(self, key, members, smooth):
        """Combine same-name processes into one Task Manager-style app row."""
        def total(field, default=0.0):
            try:
                return sum(float(m.get(field) or default) for m in members)
            except (TypeError, ValueError):
                return default

        raw_name = members[0].get("name") or key
        display_name = self._display_name_for(members[0])
        statuses = {str(m.get("status") or "").lower() for m in members}
        if any(s.startswith("running") for s in statuses):
            status = "running"
        elif any(s.startswith(("stopped", "zombie", "suspend")) for s in statuses):
            status = "stopped"
        else:
            status = members[0].get("status") or ""
        users = [str(m.get("username") or "") for m in members]
        user = next((u for u in users if u), "")
        starts = [m.get("started") or 0 for m in members]
        return dict(
            key=key,
            name=display_name,
            _sort_name=display_name.lower(),
            count=len(members),
            members=members,
            pid=None,
            # Smoothed sum drives sorting; raw sum drives the displayed value.
            cpu_pct_norm=sum(
                smooth.get(m.get("pid"), float(m.get("cpu_pct_norm") or 0))
                for m in members
            ),
            cpu_pct_raw=total("cpu_pct_norm"),
            memory_percent=total("memory_percent"),
            mem_mb=total("mem_mb"),
            num_threads=int(total("num_threads")),
            status=status,
            username=user,
            started=min(starts) if starts else 0,
        )

    def _refresh_proc_table(self):
        if not dpg.does_item_exist("proc_table"):
            return
        flt = self._proc_filter.strip().lower()
        pool = self._all_procs
        if flt:
            pool = [
                pr for pr in pool
                if flt in (pr.get("name") or "").lower()
                or flt in _friendly_name(pr.get("name") or "").lower()
                or flt in str(pr.get("pid", ""))
                or flt in (pr.get("username") or "").lower()
                or flt in _status_label(pr.get("status")).lower()
            ]

        numeric = {"pid", "cpu_pct_norm", "memory_percent", "mem_mb", "num_threads"}
        col = self._proc_sort_col
        now = time.monotonic()

        # CPU readings are noisy.  Smooth the *sort key* only, so row order
        # settles down while the displayed percentage stays instantaneous.
        smooth = self._proc_cpu_smooth
        for pr in pool:
            pid = pr.get("pid")
            raw = float(pr.get("cpu_pct_norm") or 0.0)
            prev = smooth.get(pid, raw)
            smooth[pid] = PROC_CPU_SMOOTH * raw + (1.0 - PROC_CPU_SMOOTH) * prev
        if now - self._smooth_clean_t > 60.0:
            live = {pr.get("pid") for pr in self._all_procs}
            self._proc_cpu_smooth = {k: v for k, v in smooth.items() if k in live}
            self._exe_cache = {k: v for k, v in self._exe_cache.items()
                               if k in live}
            smooth = self._proc_cpu_smooth
            self._smooth_clean_t = now

        def key(pr):
            v = pr.get(col)
            if col == "cpu_pct_norm":
                if pr.get("members"):
                    primary = float(v or 0.0)   # already summed from smooth
                else:
                    primary = smooth.get(pr.get("pid"), float(v or 0.0))
            elif col in numeric:
                try:
                    primary = float(v or 0.0)
                except (TypeError, ValueError):
                    primary = 0.0
            elif col == "name":
                primary = str(pr.get("_sort_name") or pr.get("name") or "").lower()
            else:
                primary = str(v or "").lower()
            try:
                mem = float(pr.get("mem_mb") or 0.0)
            except (TypeError, ValueError):
                mem = 0.0
            return (primary, mem, str(pr.get("_sort_name") or
                                      pr.get("name") or "").lower())

        # Build groups first, then sort groups (and their children).
        grouped = {}
        for pr in pool:
            gkey = (pr.get("name") or "?").strip().lower()
            grouped.setdefault(gkey, []).append(pr)
        self._all_group_keys = set(grouped)

        groups = [self._group_aggregate(k, m, smooth)
                  for k, m in grouped.items()]
        groups.sort(key=key, reverse=(not self._proc_sort_asc))

        entries = []
        for g in groups:
            gkey = g["key"]
            expanded = gkey in self._expanded_groups
            entries.append(dict(
                key="g|" + gkey, kind="group", group=gkey,
                data=g, pids=[m["pid"] for m in g["members"]],
                primary=g["members"][0]["pid"], expanded=expanded,
            ))
            if expanded:
                for pr in sorted(g["members"], key=key,
                                 reverse=(not self._proc_sort_asc)):
                    entries.append(dict(
                        key="p|" + str(pr["pid"]), kind="proc", group=gkey,
                        data=pr, pids=[pr["pid"]], primary=pr["pid"],
                        expanded=False,
                    ))

        by_key = {e["key"]: e for e in entries}
        n = min(len(entries), MAX_PROC_ROWS)
        if n > len(self._proc_rows) + 120:
            n = len(self._proc_rows) + 120

        # Keep row positions stable between periodic reshuffles.
        due = (self._proc_force_sort
               or now - self._proc_last_sort_t >= PROC_RESORT_INTERVAL)
        if due:
            self._display_keys = [e["key"] for e in entries[:n]]
            self._proc_last_sort_t = now
            self._proc_force_sort = False
        else:
            keep, seen = [], set()
            for k in self._display_keys[:n]:
                if k in by_key and k not in seen:
                    keep.append(k)
                    seen.add(k)
            for e in entries:
                if e["key"] not in seen:
                    keep.append(e["key"])
                    seen.add(e["key"])
                    if len(keep) >= n:
                        break
            self._display_keys = keep[:n]

        self._row_entries = [by_key[k] for k in self._display_keys if k in by_key]
        n = len(self._row_entries)
        self._ensure_proc_rows(n)
        self._row_pids = [e["primary"] for e in self._row_entries]

        # A group has no PID of its own; borrow the first member for its icon.
        def icon_source(e):
            data = e["data"]
            return data["members"][0] if e["kind"] == "group" else data

        # Resolve icons for the visible rows, then drip-feed new extractions.
        for e in self._row_entries[:40]:
            self._icon_for_proc(icon_source(e))
        self._process_icon_queue()

        self._updating_rows = True
        try:
            for i, e in enumerate(self._row_entries):
                row = self._proc_rows[i]
                pr = e["data"]
                tex = self._icon_for_proc(icon_source(e))
                if row["icon_tex"] != tex:
                    if tex and dpg.does_item_exist(row["icon"]):
                        dpg.configure_item(row["icon"], texture_tag=tex)
                    row["icon_tex"] = tex

                is_group = e["kind"] == "group"
                raw_name = str(pr.get("_sort_name") or pr.get("name") or "?")
                if is_group:
                    display = str(pr.get("name") or _friendly_name(raw_name))
                    name = f"{_short(display, 38)} ({pr['count']})"
                    pid_disp = "—"
                else:
                    display = self._display_name_for(pr)
                    name = "   " + _short(display, 40)
                    pid_disp = str(e["primary"])
                raw_status = str(pr.get("status") or "")
                status = _status_label(raw_status)
                cpu_n = float(pr.get("cpu_pct_raw",
                                     pr.get("cpu_pct_norm")) or 0.0)
                mem_p = float(pr.get("memory_percent") or 0.0)
                mem_mb = float(pr.get("mem_mb") or 0.0)
                thr = int(pr.get("num_threads") or 0)
                user = (pr.get("username") or "").split("\\")[-1]
                user = _short(user, 18) if user else "—"
                mem_disp = f"{mem_mb:,.0f} MB"

                data = (e["kind"], name, pr.get("count", 0), pid_disp, status,
                        round(cpu_n, 1), round(mem_p, 2), mem_disp, thr, user,
                        e["expanded"])
                if row["data"] != data:
                    dpg.set_item_label(row["name"], name)
                    dpg.set_item_label(row["pid"], pid_disp)
                    dpg.set_item_label(row["status"], status)
                    dpg.set_item_label(row["cpu"], f"{cpu_n:.1f}%")
                    dpg.set_item_label(row["memp"], f"{mem_p:.2f}%")
                    dpg.set_item_label(row["memmb"], mem_disp)
                    dpg.set_item_label(row["thr"], str(thr))
                    dpg.set_item_label(row["user"], user)
                    dpg.configure_item(row["toggle"], show=is_group,
                                       label=("▲" if e["expanded"] else "▼"))
                    dpg.configure_item(row["child_space"], show=not is_group)
                    font = self._fonts.get("bold" if is_group else "body")
                    if font:
                        dpg.bind_item_font(row["name"], font)
                    row["data"] = data

                cpu_col = _level_color(cpu_n, 15, 40)
                if row["cpu_col"] != cpu_col:
                    dpg.bind_item_theme(row["cpu"], self._text_theme(cpu_col))
                    row["cpu_col"] = cpu_col
                st = raw_status.lower()
                if st.startswith("running"):
                    status_col = C_GREEN
                elif st.startswith("sleeping") or st.startswith("waiting"):
                    status_col = C_DIM
                elif (st.startswith("stopped") or st.startswith("zombie")
                      or st.startswith("suspend")):
                    status_col = C_AMBER
                else:
                    status_col = C_TEXT
                if row["status_col"] != status_col:
                    dpg.bind_item_theme(row["status"],
                                        self._text_theme(status_col))
                    row["status_col"] = status_col

                pids = set(e["pids"])
                sel_state = bool(pids & self._selected_pids)
                if row["sel_state"] != sel_state:
                    for s in row["sels"]:
                        dpg.set_value(s, sel_state)
                    row["sel_state"] = sel_state
        finally:
            self._updating_rows = False

        self._text("proc_count_lbl", f"{len(self._all_procs):,}")
        self._text("proc_shown_lbl", f"{len(pool):,} / {len(self._all_procs):,}")
        app_count = len(grouped)
        self._text("proc_groups_lbl",
                   f"{app_count:,} app{'' if app_count == 1 else 's'}")
        all_expanded = (bool(self._all_group_keys)
                        and self._all_group_keys.issubset(self._expanded_groups))
        if dpg.does_item_exist("proc_expand_btn"):
            dpg.set_item_label(
                "proc_expand_btn",
                "Collapse all" if all_expanded else "Expand all",
            )
        total_threads = sum(int(pr.get("num_threads") or 0)
                            for pr in self._all_procs)
        self._text("proc_threads_lbl", f"{total_threads:,}")
        self._update_selection_ui()

    def _on_group_toggle(self, sender, app_data, user_data):
        try:
            idx = int(user_data)
        except (TypeError, ValueError):
            return
        if idx < 0 or idx >= len(self._row_entries):
            return
        e = self._row_entries[idx]
        if e["kind"] != "group":
            return
        key = e["group"]
        if key in self._expanded_groups:
            self._expanded_groups.discard(key)
        else:
            self._expanded_groups.add(key)
        self._proc_force_sort = True
        self._refresh_proc_table()

    def _expand_all_groups(self, expand=True):
        if expand:
            self._expanded_groups.update(self._all_group_keys)
        else:
            self._expanded_groups.clear()
        self._proc_force_sort = True
        self._refresh_proc_table()

    def _on_expand_all_click(self):
        all_expanded = (bool(self._all_group_keys)
                        and self._all_group_keys.issubset(self._expanded_groups))
        self._expand_all_groups(expand=not all_expanded)

    def _on_proc_click(self, sender, app_data, user_data):
        if self._updating_rows:
            return
        try:
            idx = int(user_data)
        except (TypeError, ValueError):
            return
        if idx < 0 or idx >= len(self._row_entries):
            return

        # Safety net: if the flat row selectable still receives a click that
        # physically landed on the arrow, treat it as a dropdown toggle.  This
        # keeps the arrow working even under unusual DPI/hit-test behaviour.
        try:
            if self._row_entries[idx]["kind"] == "group":
                mx, my = dpg.get_mouse_pos()
                lo = dpg.get_item_rect_min(self._proc_rows[idx]["toggle"])
                hi = dpg.get_item_rect_max(self._proc_rows[idx]["toggle"])
                if (lo[0] <= mx <= hi[0]) and (lo[1] <= my <= hi[1]):
                    self._on_group_toggle(None, None, idx)
                    return
        except Exception:
            pass

        # A fresh row click invalidates any confirmation that was open before.
        self._pending_end_pids = []
        e = self._row_entries[idx]
        pids = set(e["pids"])
        if app_data:
            self._selected_pids = pids
            self._selected_group = e["group"] if e["kind"] == "group" else None
            self._load_proc_details(e)
        elif pids & self._selected_pids:
            self._selected_pids.clear()
            self._selected_group = None
            self._clear_proc_details()
        self._update_selection_ui()

        # Double-clicking a group row also expands/collapses it.  Useful if a
        # DPI scaling quirk makes the small arrow hard to hit.
        now = time.monotonic()
        double = (app_data and e["kind"] == "group"
                  and idx == self._last_row_click_idx
                  and now - self._last_row_click_t < 0.45)
        self._last_row_click_t = now
        self._last_row_click_idx = idx
        if double:
            self._last_row_click_idx = -1
            self._on_group_toggle(None, None, idx)

    def _update_selection_ui(self):
        enabled = bool(self._selected_pids)
        for tag in ("end_proc_btn", "det_end_btn", "det_copy_btn",
                    "det_open_btn"):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=enabled)
        if dpg.does_item_exist("det_placeholder"):
            dpg.configure_item("det_placeholder", show=not enabled)
        if dpg.does_item_exist("det_body"):
            dpg.configure_item("det_body", show=enabled)

    def _clear_proc_details(self):
        if dpg.does_item_exist("det_proc_icon") and self._icon_fallback:
            dpg.configure_item("det_proc_icon", texture_tag=self._icon_fallback)
        for tag in ("det_name", "det_pid", "det_user", "det_status",
                    "det_cpu", "det_mem", "det_threads", "det_started",
                    "det_exe", "det_cmd"):
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, "—")
        self._text("det_status", "—")

    def _load_proc_details(self, entry):
        pr = entry["data"]
        is_group = entry["kind"] == "group"
        src = pr["members"][0] if is_group else pr
        pid = entry["primary"]
        tex = self._icon_for_proc(src)
        if tex and dpg.does_item_exist("det_proc_icon"):
            dpg.configure_item("det_proc_icon", texture_tag=tex)

        if is_group:
            count = pr["count"]
            self._text("det_name", f"{pr['name']} ({count})")
            self._text("det_pid", f"{count} instances")
            user = (pr.get("username") or "").split("\\")[-1]
            self._text("det_user", _short(user or "—", 28))
            self._text("det_status", _status_label(pr.get("status")))
        else:
            display = self._display_name_for(pr)
            self._text("det_name", _short(display or f"Process {pid}", 40))
            self._text("det_pid", str(pid))
            user = (pr.get("username") or "").split("\\")[-1]
            self._text("det_user", _short(user or "—", 28))
            self._text("det_status", _status_label(pr.get("status")))

        started = "—"
        if pr.get("started"):
            try:
                started = datetime.datetime.fromtimestamp(
                    float(pr["started"])).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        self._text("det_started", started)
        exe = self._exe_path_for(src) or "—"
        self._text("det_exe", _short(exe, 160))
        if is_group:
            self._text("det_cmd", f"{pr['count']} instances — "
                                  f"select a child row to inspect one PID")
        else:
            try:
                cmd = " ".join(psutil.Process(pid).cmdline()) or "—"
            except Exception:
                cmd = "—"
            self._text("det_cmd", _short(cmd, 200))

        # Fill live values immediately; switching selection should not leave
        # the previous app's numbers visible until the next sample arrives.
        pids = set(entry["pids"])
        members = [m for m in self._all_procs if m.get("pid") in pids]
        cpu = sum(float(m.get("cpu_pct_norm") or 0) for m in members)
        mem = sum(float(m.get("mem_mb") or 0) for m in members)
        memp = sum(float(m.get("memory_percent") or 0) for m in members)
        thr = sum(int(m.get("num_threads") or 0) for m in members)
        self._text("det_cpu", f"{cpu:.1f}%")
        self._text("det_mem", f"{mem:,.1f} MB  ({memp:.2f}%)")
        self._text("det_threads", str(thr))

    def _update_selected_proc(self, p):
        if not self._selected_pids:
            return
        live = {x["pid"]: x for x in p.processes}
        members = [live[pid] for pid in self._selected_pids if pid in live]
        if not members:
            self._text("det_status", "Exited")
            return
        cpu = sum(float(m.get("cpu_pct_norm") or 0) for m in members)
        mem = sum(float(m.get("mem_mb") or 0) for m in members)
        memp = sum(float(m.get("memory_percent") or 0) for m in members)
        thr = sum(int(m.get("num_threads") or 0) for m in members)
        if self._selected_group:
            statuses = {str(m.get("status") or "").lower() for m in members}
            status = ("running" if any(s.startswith("running") for s in statuses)
                      else next(iter(statuses), ""))
        else:
            status = members[0].get("status")
        self._text("det_status", _status_label(status))
        self._text("det_cpu", f"{cpu:.1f}%")
        self._text("det_mem", f"{mem:,.1f} MB  ({memp:.2f}%)")
        self._text("det_threads", str(thr))

    def _on_proc_filter(self, sender, app_data):
        self._proc_filter = app_data or ""
        self._proc_force_sort = True
        self._refresh_proc_table()

    def _clear_filter(self):
        self._proc_filter = ""
        if dpg.does_item_exist("proc_filter"):
            dpg.set_value("proc_filter", "")
        self._proc_force_sort = True
        self._refresh_proc_table()

    def _sort_by(self, col, asc=None):
        """Sort by a field and immediately refresh the visible rows."""
        if asc is None:
            if self._proc_sort_col == col:
                self._proc_sort_asc = not self._proc_sort_asc
            else:
                self._proc_sort_col = col
                # Numbers read best largest-first (CPU, memory, threads);
                # text and PID read best smallest-first.
                self._proc_sort_asc = col in (
                    "name", "status", "username", "pid"
                )
        else:
            self._proc_sort_col = col
            self._proc_sort_asc = bool(asc)
        self._proc_force_sort = True
        self._update_sort_buttons()
        self._refresh_proc_table()

    def _on_proc_sort(self, sender, sort_specs):
        """Native header-click handler: [[column_index, direction]], where
        direction is 1 for ascending and -1 for descending."""
        if not sort_specs:
            return
        try:
            idx, direction = sort_specs[0]
            col = self._proc_sort_keys.get(int(idx))
        except (TypeError, ValueError, IndexError):
            return
        if not col:
            return
        asc = direction > 0
        if self._proc_sort_col == col and self._proc_sort_asc == asc:
            return
        self._sort_by(col, asc)

    def _on_proc_sort_combo(self, sender, label):
        """The visible Sort-by combo — fully wired, unlike the old chip."""
        col = self._proc_sort_from_label.get(label)
        if not col:
            return
        self._sort_by(col, asc=(col in ("name", "status", "username", "pid")))

    def _toggle_proc_sort_dir(self):
        """Direction button next to the combo."""
        self._sort_by(self._proc_sort_col, asc=not self._proc_sort_asc)

    def _update_sort_buttons(self):
        label = self._proc_sort_labels.get(self._proc_sort_col,
                                           self._proc_sort_col)
        arrow = "▲" if self._proc_sort_asc else "▼"
        if (dpg.does_item_exist("proc_sort_combo")
                and dpg.get_value("proc_sort_combo") != label):
            dpg.set_value("proc_sort_combo", label)
        if dpg.does_item_exist("proc_sort_dir_btn"):
            dpg.set_item_label("proc_sort_dir_btn", arrow)

    def _ask_end_process(self, pids=None):
        if pids is None:
            pids = list(self._selected_pids)
        elif isinstance(pids, int):
            pids = [pids]
        else:
            pids = list(pids)
        if not pids:
            return
        self._pending_end_pids = pids
        by_pid = {pr["pid"]: pr for pr in self._all_procs}
        names = [str(by_pid.get(pid, {}).get("name") or "?") for pid in pids]
        display_names = [
            self._display_name_for(by_pid[pid]) if pid in by_pid else names[i]
            for i, pid in enumerate(pids)
        ]
        critical_names = {
            "system", "csrss.exe", "wininit.exe", "winlogon.exe",
            "services.exe", "lsass.exe", "smss.exe", "svchost.exe",
            "explorer.exe",
        }
        critical = any(n.lower() in critical_names for n in names)
        if len(pids) == 1:
            head = f"End  [{pids[0]}]  {display_names[0]}?"
        else:
            label = next(
                (e["data"]["name"] for e in self._row_entries
                 if e["kind"] == "group" and e["group"] == self._selected_group),
                display_names[0],
            )
            head = f"End all {len(pids)} instances of {label}?"
        if dpg.does_item_exist("confirm_end_text"):
            dpg.set_value(
                "confirm_end_text",
                f"{head}\n\n"
                "Any unsaved work in these processes will be lost.",
            )
        if dpg.does_item_exist("confirm_end_warn"):
            dpg.set_value(
                "confirm_end_warn",
                "Warning: this includes a critical Windows process. Ending it "
                "may destabilise your system." if critical else "",
            )
        self._show_modal("confirm_end", 460, 230)

    def _cancel_end_process(self):
        self._pending_end_pids = []
        if dpg.does_item_exist("confirm_end"):
            dpg.configure_item("confirm_end", show=False)

    def _do_end_process(self):
        pids = list(self._pending_end_pids or self._selected_pids)
        if dpg.does_item_exist("confirm_end"):
            dpg.configure_item("confirm_end", show=False)
        if not pids:
            return
        done, failed = 0, 0
        for pid in pids:
            try:
                psutil.Process(pid).terminate()
                done += 1
            except Exception:
                failed += 1
        label = (f"{done} process(es)" if len(pids) > 1
                 else f"[{pids[0]}]")
        if done:
            self._toast(f"Terminated {label}", C_GREEN)
        if failed:
            self._toast(f"Could not end {failed} process(es)", C_AMBER)
        self._pending_end_pids = []
        self._selected_pids.clear()
        self._selected_group = None
        self._clear_proc_details()
        self._update_selection_ui()
        self._refresh_proc_table()

    def _copy_pid(self):
        pids = sorted(self._selected_pids)
        if not pids:
            return
        try:
            text = ", ".join(str(p) for p in pids)
            dpg.set_clipboard_text(text)
            self._toast(f"Copied PID{'' if len(pids) == 1 else 's'}: {text}", C_GREEN)
        except Exception:
            pass

    def _open_location(self):
        if not self._selected_pids:
            return
        pid = next(iter(self._selected_pids))
        try:
            exe = psutil.Process(pid).exe()
            folder = os.path.dirname(exe)
            if folder and os.path.isdir(folder):
                if IS_WINDOWS:
                    os.startfile(folder)
                else:
                    subprocess.Popen(
                        ["xdg-open", folder],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            else:
                self._toast("Executable location unavailable", C_AMBER)
        except Exception as e:
            self._toast(f"Cannot open location: {e}", C_RED)

    # ─────────────────────────────────────────────────────────────────────────
    # Disk / NIC tables
    # ─────────────────────────────────────────────────────────────────────────
    def _update_disk_table(self, p):
        if not dpg.does_item_exist("disk_table"):
            return
        mounts = [d["mount"] for d in p.disks]
        if set(mounts) != set(self._disk_rows.keys()):
            for ch in list(dpg.get_item_children("disk_table", slot=1)):
                try:
                    dpg.delete_item(ch)
                except Exception:
                    pass
            self._disk_rows.clear()
            for d in p.disks:
                mount = d["mount"]
                with dpg.table_row(parent="disk_table") as row:
                    name = dpg.add_text(mount, color=_l(C_TEXT))
                    bar = dpg.add_progress_bar(default_value=0, width=-1, height=8)
                    pct = dpg.add_text("0%", color=_l(C_DIM))
                    used = dpg.add_text("", color=_l(C_TEXT))
                    free = dpg.add_text("", color=_l(C_TEXT))
                    total = dpg.add_text("", color=_l(C_TEXT))
                    fstype = dpg.add_text("", color=_l(C_DIM))
                self._disk_rows[mount] = dict(
                    row=row, name=name, bar=bar, pct=pct, used=used,
                    free=free, total=total, fstype=fstype, cls=None,
                )
        for d in p.disks:
            row = self._disk_rows.get(d["mount"])
            if not row:
                continue
            pct = float(d["pct"])
            dpg.set_value(row["bar"], max(0.0, min(100.0, pct)) / 100.0)
            dpg.set_value(row["pct"], f"{pct:.0f}%")
            dpg.set_value(row["used"], _fmt_gb(d["used_gb"]))
            dpg.set_value(row["free"], _fmt_gb(d["free_gb"]))
            dpg.set_value(row["total"], _fmt_gb(d["total_gb"]))
            dpg.set_value(row["fstype"], d["fstype"])
            cls = 2 if pct >= 90 else 1 if pct >= 75 else 0
            if row["cls"] != cls:
                dpg.bind_item_theme(row["bar"], self._pb_themes[cls])
                row["cls"] = cls
            is_sys = d["mount"].lower().rstrip("\\") == \
                self._sys.get("sys_drive", DEFAULT_SYS_DRIVE).lower().rstrip("\\")
            dpg.configure_item(row["name"],
                               color=_l(C_CPU if is_sys else C_TEXT))

    def _update_nic_table(self, p):
        if not dpg.does_item_exist("nic_table"):
            return
        names = [n["name"] for n in p.nics]
        if set(names) != set(self._nic_rows.keys()):
            for ch in list(dpg.get_item_children("nic_table", slot=1)):
                try:
                    dpg.delete_item(ch)
                except Exception:
                    pass
            self._nic_rows.clear()
            for nic in p.nics:
                with dpg.table_row(parent="nic_table") as row:
                    name = dpg.add_text(_short(nic["name"], 32), color=_l(C_TEXT))
                    status = dpg.add_text("", color=_l(C_DIM))
                    speed = dpg.add_text("", color=_l(C_TEXT))
                    sent = dpg.add_text("", color=_l(C_TEXT))
                    recv = dpg.add_text("", color=_l(C_TEXT))
                    pkts = dpg.add_text("", color=_l(C_TEXT))
                    errs = dpg.add_text("", color=_l(C_DIM))
                self._nic_rows[nic["name"]] = dict(
                    row=row, name=name, status=status, speed=speed,
                    sent=sent, recv=recv, pkts=pkts, errs=errs, st=None,
                )
        for nic in p.nics:
            row = self._nic_rows.get(nic["name"])
            if not row:
                continue
            up = nic["is_up"]
            dpg.set_value(row["status"], "UP" if up else "DOWN")
            dpg.set_value(row["speed"],
                          f"{nic['speed']} Mbps" if nic["speed"] else "—")
            dpg.set_value(row["sent"], f"{nic['sent_mb']:,.1f} MB")
            dpg.set_value(row["recv"], f"{nic['recv_mb']:,.1f} MB")
            dpg.set_value(row["pkts"], f"{nic['pkts']:,}")
            dpg.set_value(row["errs"], f"{nic['errors']:,}")
            col = C_GREEN if up else C_RED
            if row["st"] != col:
                dpg.configure_item(row["status"], color=_l(col))
                row["st"] = col

    # ─────────────────────────────────────────────────────────────────────────
    # History helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _run_query(self, sender=None, app_data=None):
        if not self._conn:
            self._set_query_status("Database unavailable", False)
            return
        sql = ""
        if dpg.does_item_exist("sql_in"):
            sql = (dpg.get_value("sql_in") or "").strip()
        if not sql:
            return
        t0 = time.perf_counter()
        try:
            rows, desc = self._db_query(sql)
            ms = (time.perf_counter() - t0) * 1000
            self._last_sql = sql
            self._last_rows = rows
            self._last_desc = desc
            self._set_query_status(f"OK  •  {len(rows):,} rows  •  {ms:.0f} ms", True)
            self._render_query_result(rows, desc)
        except Exception as e:
            self._set_query_status("ERROR", False)
            self._set_query_output(f"Error:\n{e}", C_RED)

    def _render_query_result(self, rows, desc):
        if not rows:
            self._set_query_output("(no rows returned)", C_DIM)
            return
        headers = [str(d[0]) for d in desc]
        shown = rows[:MAX_QUERY_ROWS]
        widths = []
        for i, h in enumerate(headers):
            w = max(len(h), 1)
            for r in shown:
                w = max(w, len(str(r[i])))
            widths.append(min(w, 34))
        def cell(v, w):
            s = str(v)
            if len(s) > w:
                s = s[: max(1, w - 3)] + "..."
            return s.ljust(w)
        head = "  ".join(cell(h, widths[i]) for i, h in enumerate(headers))
        sep = "  ".join("-" * w for w in widths)
        lines = [head, sep]
        for r in shown:
            lines.append("  ".join(cell(r[i], widths[i]) for i in range(len(headers))))
        if len(rows) > MAX_QUERY_ROWS:
            lines.append(f"\n... {len(rows):,} total rows "
                         f"(showing {MAX_QUERY_ROWS:,})")
        self._set_query_output("\n".join(lines), C_TEXT)

    def _set_query_output(self, text, color=C_TEXT):
        if dpg.does_item_exist("query_out"):
            dpg.set_value("query_out", text)
            dpg.configure_item("query_out", color=_l(color))

    def _set_query_status(self, text, ok):
        if dpg.does_item_exist("query_status"):
            dpg.set_value("query_status", text)
            dpg.configure_item("query_status",
                               color=_l(C_GREEN if ok else C_RED))

    def _export_csv(self, sender=None, app_data=None):
        if not self._conn:
            return
        sql = ""
        if dpg.does_item_exist("sql_in"):
            sql = (dpg.get_value("sql_in") or "").strip()
        if not sql:
            self._set_query_status("Nothing to export", False)
            return
        try:
            rows, desc = self._db_query(sql)
            fname = os.path.join(
                _export_dir(),
                "spanda_export_"
                + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv",
            )
            with open(fname, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([d[0] for d in desc])
                w.writerows(rows)
            self._set_query_status(
                f"Exported {len(rows):,} rows -> {os.path.basename(fname)}", True)
            self._toast(f"Exported {os.path.basename(fname)}", C_GREEN)
        except Exception as e:
            self._set_query_status(f"Export error: {e}", False)

    def _ask_clear_db(self):
        self._show_modal("confirm_clear", 460, 210)

    def _do_clear_db(self):
        if dpg.does_item_exist("confirm_clear"):
            dpg.configure_item("confirm_clear", show=False)
        if not self._conn:
            return
        try:
            with self._db_lock:
                self._conn.execute("DELETE FROM system_metrics")
            self._set_query_output("Database cleared - all metrics deleted.", C_AMBER)
            self._set_query_status("Database cleared", True)
            self._refresh_db_stats()
            self._toast("History cleared", C_AMBER)
        except Exception as e:
            self._set_query_status(f"Error: {e}", False)

    def _refresh_db_stats(self):
        self._db_stats_t = time.monotonic()
        if not self._conn:
            self._text("db_stats_lbl", "Database unavailable")
            return
        try:
            with self._db_lock:
                n = self._conn.execute(
                    "SELECT COUNT(*) FROM system_metrics").fetchone()[0]
            size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
            self._db_count = n
            self._db_size = size
            self._text(
                "db_stats_lbl",
                f"{n:,} samples  •  {_fmt_bytes(size)}  •  "
                f"{os.path.basename(DB_PATH)}",
            )
            self._text("ftr_db", f"DB {_fmt_bytes(size)}")
        except Exception:
            pass

    # ── Start / stop ─────────────────────────────────────────────────────────
    def _toggle(self, sender=None, app_data=None):
        if not self._running:
            self._running = True
            self._stop_event.clear()
            self._bg_thread = threading.Thread(
                target=self._bg_loop, daemon=True, name="spanda-sampler")
            self._bg_thread.start()
            if dpg.does_item_exist("toggle_btn"):
                dpg.set_item_label("toggle_btn", "Pause")
                dpg.bind_item_theme("toggle_btn", self._btn_pause)
            if dpg.does_item_exist("status_text"):
                dpg.set_value("status_text", "LIVE")
                dpg.configure_item("status_text", color=_l(C_GREEN))
            self._toast("Monitoring resumed", C_GREEN)
        else:
            self._running = False
            self._stop_event.set()
            if self._bg_thread:
                self._bg_thread.join(timeout=3)
            self._flush_db()
            if dpg.does_item_exist("toggle_btn"):
                dpg.set_item_label("toggle_btn", "Resume")
                dpg.bind_item_theme("toggle_btn", self._btn_resume)
            if dpg.does_item_exist("status_text"):
                dpg.set_value("status_text", "PAUSED")
                dpg.configure_item("status_text", color=_l(C_DIM))
            self._toast("Monitoring paused", C_AMBER)

    # ─────────────────────────────────────────────────────────────────────────
    # Small UI helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _text(self, tag, value):
        if not dpg.does_item_exist(tag):
            return
        if self._last_text.get(tag) == value:
            return
        self._last_text[tag] = value
        dpg.set_value(tag, value)

    def _set_color(self, tag, col):
        if not dpg.does_item_exist(tag):
            return
        key = tuple(col)
        if self._last_color.get(tag) == key:
            return
        self._last_color[tag] = key
        dpg.configure_item(tag, color=_l(col))

    def _set_series(self, tag, xy):
        x, y = xy
        if x and dpg.does_item_exist(tag):
            dpg.set_value(tag, [x, y])

    def _scroll_axis(self, tag, ts):
        """Follow the live edge of a chart's X axis (relative-time units).

        If the user has zoomed/panned that axis, leave it alone so their view
        is never yanked away. The per-chart reset button restores following.
        """
        if not dpg.does_item_exist(tag) or self._user_zoom.get(tag):
            return
        rt = ts - self._plot_t0
        span = self._window_span
        # Never show negative time.  Clamping the lower bound to 0 means every
        # pixel of a fresh chart is filled with real data; the window only
        # starts scrolling once history is older than the visible span.
        lo = max(0.0, rt - span)
        hi = max(lo + 6.0, rt + 2.0)
        dpg.set_axis_limits(tag, lo, hi)
        self._axis_expected[tag] = (lo, hi)

    def _auto_axis(self, tag, values):
        target = max(1.0, (max(values) if values else 0.0) * 1.18)
        self._axis_target[tag] = target
        if tag not in self._axis_cur:
            self._axis_cur[tag] = target

    def _mark_user_zoom(self):
        """Detect manual zoom/pan by comparing live limits to where we put
        them. Called once per frame; results gate all automatic axis updates."""
        for tag in list(self._user_zoom.keys()):
            if not dpg.does_item_exist(tag):
                continue
            want = self._axis_expected.get(tag)
            if want is None:
                continue
            try:
                cur = dpg.get_axis_limits(tag)
            except Exception:
                continue
            if not cur or cur[1] <= cur[0]:
                continue
            tol = max(0.5, (want[1] - want[0]) * 0.06)
            if abs(cur[0] - want[0]) > tol or abs(cur[1] - want[1]) > tol:
                self._user_zoom[tag] = True

    def _reset_zoom(self, chart_tag):
        """Restore auto-follow for both axes of one chart."""
        x_tag, y_tag = self._chart_axes.get(chart_tag, (None, None))
        for tag in (x_tag, y_tag):
            if not tag:
                continue
            self._user_zoom[tag] = False
            self._axis_expected.pop(tag, None)
        # Re-apply immediately so the view snaps back this frame
        packet = self._latest
        if y_tag in self._auto_y_axes and y_tag in self._axis_target:
            target = self._axis_target[y_tag]
            self._axis_cur[y_tag] = target
            dpg.set_axis_limits(y_tag, 0.0, max(1.0, target))
            self._axis_expected[y_tag] = (0.0, max(1.0, target))
        elif y_tag:
            dpg.set_axis_limits(y_tag, 0, 100)
            self._axis_expected[y_tag] = (0.0, 100.0)
        if packet and getattr(packet, "ts", 0):
            self._scroll_axis(x_tag, packet.ts)

    def _anim_bar(self, tag, target, rate=12.0):
        if not dpg.does_item_exist(tag):
            return
        a = self._anim.get(tag)
        if a is None:
            self._anim[tag] = dict(
                tag=tag, cur=0.0, target=float(target), rate=rate,
                kind="bar", last=None, cls=-1,
            )
        else:
            a["target"] = float(target)
            a["rate"] = rate

    def _anim_text(self, tag, target, fmt, rate=8.0):
        if not dpg.does_item_exist(tag):
            return
        a = self._anim.get(tag)
        if a is None:
            self._anim[tag] = dict(
                tag=tag, cur=0.0, target=float(target), rate=rate,
                kind="text", fmt=fmt, last=None,
            )
        else:
            a["target"] = float(target)
            a["rate"] = rate
            a["fmt"] = fmt
            a["kind"] = "text"

    def _animate(self, dt):
        # Bars / animated text
        for a in list(self._anim.values()):
            cur = a["cur"]
            target = a["target"]
            diff = target - cur
            if abs(diff) < 0.02:
                cur = target
            else:
                cur += diff * (1.0 - math.exp(-dt * a["rate"]))
            a["cur"] = cur
            if a["kind"] == "bar":
                if not dpg.does_item_exist(a["tag"]):
                    continue
                dpg.set_value(a["tag"], max(0.0, min(100.0, cur)) / 100.0)
                cls = 2 if cur >= 86 else 1 if cur >= 62 else 0
                if cls != a["cls"]:
                    dpg.bind_item_theme(a["tag"], self._pb_themes[cls])
                    a["cls"] = cls
            else:
                s = a["fmt"](cur)
                if s != a["last"]:
                    dpg.set_value(a["tag"], s)
                    a["last"] = s

        # Smooth auto-scaling axes - but never fight a user who zoomed in.
        for tag, target in self._axis_target.items():
            if not dpg.does_item_exist(tag) or self._user_zoom.get(tag):
                continue
            cur = self._axis_cur.get(tag, target)
            cur += (target - cur) * (1.0 - math.exp(-dt * 4.0))
            self._axis_cur[tag] = cur
            hi = max(1.0, cur)
            dpg.set_axis_limits(tag, 0.0, hi)
            self._axis_expected[tag] = (0.0, hi)

        # Live dot pulse
        self._animate_live_dot()

        # Toast expiry
        if self._toast_until and time.monotonic() > self._toast_until:
            self._toast_until = 0.0
            self._text("ftr_msg", "Ready")
            self._set_color("ftr_msg", C_MUTED)

    def _animate_live_dot(self):
        if not dpg.does_item_exist("live_dot"):
            return
        try:
            if self._running:
                t = time.time()
                pulse = 0.5 + 0.5 * math.sin(t * 3.2)
                r = 3.6 + 1.4 * pulse
                alpha = int(160 + 95 * pulse)
                dpg.configure_item("live_dot", radius=r,
                                   fill=_a(C_GREEN, alpha),
                                   color=_a(C_GREEN, alpha))
                if dpg.does_item_exist("live_halo"):
                    dpg.configure_item("live_halo", radius=7.5 + 3.0 * pulse,
                                       color=_a(C_GREEN, int(55 * (1 - pulse))))
            else:
                dpg.configure_item("live_dot", radius=4.0,
                                   fill=_a(C_MUTED, 210),
                                   color=_a(C_MUTED, 210))
                if dpg.does_item_exist("live_halo"):
                    dpg.configure_item("live_halo", radius=8.0,
                                       color=_a(C_MUTED, 0))
        except Exception:
            pass

    def _toast(self, msg, color=C_TEXT):
        self._toast_until = time.monotonic() + 6.0
        self._text("ftr_msg", msg)
        self._set_color("ftr_msg", color)

    def _update_fps(self):
        if self._fps <= 0:
            return
        self._text("ftr_fps", f"{self._fps:.0f} FPS")
        col = C_GREEN if self._fps >= 50 else C_AMBER if self._fps >= 30 else C_RED
        self._set_color("ftr_fps", col)

    @staticmethod
    def _show_modal(tag, w, h):
        if not dpg.does_item_exist(tag):
            return
        try:
            vw = dpg.get_viewport_client_width()
            vh = dpg.get_viewport_client_height()
            x = max(20, (vw - w) // 2)
            y = max(20, (vh - h) // 2)
            dpg.set_item_pos(tag, [x, y])
        except Exception:
            pass
        dpg.configure_item(tag, show=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Layout helpers
    # ─────────────────────────────────────────────────────────────────────────
    @contextmanager
    def _card(self, tag, height, theme=None, no_scrollbar=True):
        with dpg.child_window(
            tag=tag, width=-1, height=height, border=True,
            no_scrollbar=no_scrollbar,
        ) as cw:
            dpg.bind_item_theme(cw, theme or self._card_theme)
            yield cw

    def _title(self, text, color=C_DIM, font="small", tag=None, wrap=0):
        kwargs = dict(color=_l(color), wrap=wrap)
        if tag is not None:
            kwargs["tag"] = tag
        item = dpg.add_text(text, **kwargs)
        f = self._fonts.get(font)
        if f:
            dpg.bind_item_font(item, f)
        return item

    def _value(self, text, tag, color=C_TEXT, font="big", wrap=0):
        kwargs = dict(color=_l(color), wrap=wrap)
        if tag is not None:
            kwargs["tag"] = tag
        item = dpg.add_text(text, **kwargs)
        f = self._fonts.get(font)
        if f:
            dpg.bind_item_font(item, f)
        return item

    def _stat(self, label, tag, color=C_TEXT, big=False):
        with dpg.group():
            self._title(label, C_DIM, "small")
            self._value("—", tag, color, "big" if big else "body")

    def _set_h(self, tag, h):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, height=max(30, int(h)))

    def _relayout(self):
        if not dpg.does_item_exist("main_win"):
            return
        try:
            H = dpg.get_viewport_client_height()
        except Exception:
            return
        if H < 100:
            return
        avail = max(360.0, H - HEADER_H - FOOTER_H - TABBAR_CHROME
                    - MAIN_PAD - 6.0)

        # Overview
        fixed = 110 + 42 + 24
        row_h = max(140.0, (avail - fixed) / 2.0)
        for tag in ("ov_chart_cpu", "ov_chart_mem", "ov_chart_gpu",
                    "ov_chart_disk", "ov_chart_net"):
            self._set_h(tag, row_h)

        # CPU
        self._set_h("cpu_chart", max(150.0, avail - 48 - 200 - 16))

        # Memory
        mem_h = max(130.0, avail - 128 - 8)
        self._set_h("mem_chart", mem_h)
        self._set_h("swap_chart", mem_h)

        # Disk
        disk_main = max(200.0, avail - 50 - 8)
        self._set_h("disk_parts_card", disk_main)
        self._set_h("disk_chart", disk_main)
        self._set_h("disk_table", disk_main - 44)

        # Network
        nic_h = max(150.0, min(280.0, avail * 0.30))
        net_chart = max(130.0, avail - 96 - nic_h - 16)
        self._set_h("net_nic_card", nic_h)
        self._set_h("net_chart", net_chart)
        self._set_h("nic_table", nic_h - 44)

        # GPU
        self._set_h("gpu_chart", max(150.0, avail - 48 - 128 - 16))
        self._set_h("gpu_empty", avail)

        # Processes
        # The toolbar and the trailing summary strip consume more vertical
        # space than their configured heights (DPG child-window padding), so
        # this correction keeps the summary above the footer at every size.
        proc_h = max(200.0, avail - 50 - 8 - 28 - 40)
        self._set_h("proc_table_card", proc_h)
        self._set_h("proc_details_card", proc_h)
        self._set_h("proc_table", proc_h - 22)

        # History
        self._set_h("hist_results_card",
                    max(120.0, avail - 50 - 100 - 132 - 24))

    # ─────────────────────────────────────────────────────────────────────────
    # Fonts and themes
    # ─────────────────────────────────────────────────────────────────────────
    def _load_fonts(self):
        if IS_LINUX:
            font_dirs = (
                "/usr/share/fonts/truetype/dejavu",
                "/usr/share/fonts/dejavu",
                "/usr/share/fonts/truetype/liberation2",
                "/usr/share/fonts/truetype/freefont",
            )
            faces = {
                "body": ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", 17),
                "bold": ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", 17),
                "small": ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", 14),
                "big": ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", 23),
                "huge": ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", 32),
                "brand": ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", 24),
                "mono": ("DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", 15),
            }

            def load(names, size):
                for name in names:
                    for directory in font_dirs:
                        path = os.path.join(directory, name)
                        if not os.path.exists(path):
                            continue
                        try:
                            with dpg.font(path, size) as f:
                                pass
                            return f
                        except Exception:
                            return None
                return None
        else:
            win = os.environ.get("WINDIR", r"C:\Windows")
            font_dir = os.path.join(win, "Fonts")

            def load(name, size):
                path = os.path.join(font_dir, name)
                if not os.path.exists(path):
                    return None
                try:
                    with dpg.font(path, size) as f:
                        pass
                    return f
                except Exception:
                    return None

        with dpg.font_registry():
            if IS_LINUX:
                for key, (primary, fallback, size) in faces.items():
                    self._fonts[key] = load((primary, fallback), size)
            else:
                self._fonts["body"] = load("segoeui.ttf", 17)
                self._fonts["bold"] = load("segoeuib.ttf", 17)
                self._fonts["small"] = load("segoeui.ttf", 14)
                self._fonts["big"] = load("segoeuib.ttf", 23)
                self._fonts["huge"] = load("segoeuib.ttf", 32)
                self._fonts["brand"] = load("segoeuib.ttf", 24)
                self._fonts["mono"] = load("consola.ttf", 15)
        if self._fonts.get("body"):
            dpg.bind_font(self._fonts["body"])
        for key in ("small", "big", "huge", "brand", "mono"):
            if not self._fonts.get(key):
                self._fonts[key] = self._fonts.get("body")

    def _create_themes(self):
        # Global theme
        with dpg.theme() as g:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 7)
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, 9)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 7)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, 7)
                dpg.add_theme_style(dpg.mvStyleVar_TabRounding, 7)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 9, 6)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 9, 7)
                dpg.add_theme_style(dpg.mvStyleVar_ItemInnerSpacing, 6, 5)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 10, 8)
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 7, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 11)
                dpg.add_theme_style(dpg.mvStyleVar_GrabMinSize, 11)
                dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize, 0)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_PopupBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_TabBorderSize, 0)
                dpg.add_theme_style(dpg.mvStyleVar_TabBarBorderSize, 0)
                dpg.add_theme_style(dpg.mvStyleVar_TabBarOverlineSize, 2)
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, _l(C_BG))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, _l(C_PANEL))
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, _l(C_PANEL2))
                dpg.add_theme_color(dpg.mvThemeCol_Border, _l(C_BORDER))
                dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, [0, 0, 0, 0])
                dpg.add_theme_color(dpg.mvThemeCol_Text, _l(C_TEXT))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, _l(C_DIM))
                dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg,
                                    _a(C_ACCENT, 90))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, _l(C_PANEL3))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered,
                                    (33, 40, 58, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,
                                    (40, 48, 70, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, _l(C_BG))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, _l(C_BG))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, _l(C_BG))
                dpg.add_theme_color(dpg.mvThemeCol_MenuBarBg, _l(C_PANEL))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab,
                                    (44, 50, 70, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered,
                                    (58, 66, 92, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive,
                                    _a(C_ACCENT, 220))
                dpg.add_theme_color(dpg.mvThemeCol_Separator, _l(C_BORDER))
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorHovered,
                                    _a(C_ACCENT, 160))
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorActive,
                                    _a(C_ACCENT, 220))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (32, 39, 56, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                                    (44, 54, 78, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,
                                    _a(C_ACCENT, 210))
                dpg.add_theme_color(dpg.mvThemeCol_Header, _a(C_ACCENT, 70))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,
                                    _a(C_ACCENT, 110))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive,
                                    _a(C_ACCENT, 170))
                dpg.add_theme_color(dpg.mvThemeCol_Tab, _l(C_BG))
                dpg.add_theme_color(dpg.mvThemeCol_TabHovered,
                                    _a(C_ACCENT, 55))
                dpg.add_theme_color(dpg.mvThemeCol_TabSelected, _l(C_PANEL))
                dpg.add_theme_color(dpg.mvThemeCol_TabSelectedOverline,
                                    _l(C_ACCENT))
                dpg.add_theme_color(dpg.mvThemeCol_TabDimmed, _l(C_BG))
                dpg.add_theme_color(dpg.mvThemeCol_TabDimmedSelected,
                                    _l(C_PANEL))
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocused, _l(C_BG))
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocusedActive,
                                    _l(C_PANEL))
                dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg,
                                    (18, 22, 32, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight,
                                    _l(C_BORDER))
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong,
                                    _l(C_BORDER))
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBg,
                                    (17, 20, 29, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt,
                                    (20, 24, 34, 255))
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, _l(C_ACCENT))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, _l(C_ACCENT))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive,
                                    _l(C_CPU))
                dpg.add_theme_color(dpg.mvThemeCol_NavHighlight,
                                    _a(C_ACCENT, 120))
                dpg.add_theme_color(dpg.mvThemeCol_ModalWindowDimBg,
                                    (0, 0, 0, 160))
        dpg.bind_theme(g)

        # Card
        with dpg.theme() as card:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, _l(C_PANEL))
                dpg.add_theme_color(dpg.mvThemeCol_Border, _l(C_BORDER))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 10)
        self._card_theme = card

        # Chart card (slightly different background)
        with dpg.theme() as chart_card:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, _l(C_PANEL2))
                dpg.add_theme_color(dpg.mvThemeCol_Border, _l(C_BORDER))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 10, 8)
        self._chart_theme = chart_card

        # Compact strip
        with dpg.theme() as strip:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, _l(C_PANEL))
                dpg.add_theme_color(dpg.mvThemeCol_Border, _l(C_BORDER))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 6)
        self._strip_theme = strip

        # Plot theme
        with dpg.theme() as plot:
            with dpg.theme_component(dpg.mvPlot):
                dpg.add_theme_color(dpg.mvPlotCol_PlotBg,
                                    (14, 17, 25, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_PlotBorder,
                                    _l(C_BORDER), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_FrameBg,
                                    _l(C_PANEL2), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_LegendBg,
                                    (16, 19, 28, 220), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_LegendBorder,
                                    _l(C_BORDER), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_LegendText,
                                    _l(C_TEXT), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_AxisGrid,
                                    _l(C_GRID), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_AxisText,
                                    _l(C_DIM), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_AxisTick,
                                    _l(C_GRID), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_TitleText,
                                    _l(C_TEXT), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_InlayText,
                                    _l(C_DIM), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_Crosshairs,
                                    _a(C_ACCENT, 160), category=dpg.mvThemeCat_Plots)
        self._plot_theme = plot

        # Progress bar themes: green / amber / red
        self._pb_themes = []
        for col in (C_GREEN, C_AMBER, C_RED):
            with dpg.theme() as t:
                with dpg.theme_component(dpg.mvProgressBar):
                    dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, _l(col))
                    dpg.add_theme_color(dpg.mvThemeCol_FrameBg, _l(C_TRACK))
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
            self._pb_themes.append(t)

        # Button themes
        def button_theme(bg, hover, active, text=C_WHITE):
            with dpg.theme() as t:
                with dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, _l(bg))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, _l(hover))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, _l(active))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, _l(text))
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 7)
                    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 6)
            return t

        self._btn_primary = button_theme(C_ACCENT, (72, 155, 255),
                                         (40, 118, 230))
        self._btn_danger = button_theme((190, 55, 65), (220, 70, 80),
                                        (160, 42, 52))
        self._btn_ghost = button_theme((30, 36, 52), (42, 51, 72),
                                       (50, 60, 84), C_TEXT)
        self._btn_active = button_theme(C_ACCENT, (72, 155, 255),
                                        (40, 118, 230))
        self._btn_pause = button_theme((190, 55, 65), (220, 70, 80),
                                       (160, 42, 52))
        self._btn_resume = button_theme((34, 160, 110), (48, 190, 132),
                                        (26, 130, 90))

    def _series_theme(self, col):
        key = tuple(col)
        if key in self._series_themes:
            return self._series_themes[key]
        with dpg.theme() as t:
            with dpg.theme_component(dpg.mvLineSeries):
                dpg.add_theme_color(dpg.mvPlotCol_Line, _l(col),
                                    category=dpg.mvThemeCat_Plots)
        self._series_themes[key] = t
        return t

    # ─────────────────────────────────────────────────────────────────────────
    # Chart builder
    # ─────────────────────────────────────────────────────────────────────────
    def _chart(self, tag, title, color, x_tag, y_tag, series,
               height=200, auto=False, legend=False):
        # no_scroll_with_mouse keeps the mouse wheel inside the plot (zoom)
        # instead of scrolling the surrounding tab/window.
        with dpg.child_window(tag=tag, width=-1, height=height, border=True,
                              no_scrollbar=True,
                              no_scroll_with_mouse=True) as cw:
            dpg.bind_item_theme(cw, self._chart_theme)
            with dpg.group(horizontal=True):
                dpg.add_text("●", color=_l(color))
                self._title(title, C_DIM, "small")
                dpg.add_spacer(width=10)
                self._value("—", tag + "_val", color, "small")
                dpg.add_spacer(width=6)
                reset = dpg.add_button(label="fit", width=30, height=18,
                                       callback=lambda s, a, u=tag:
                                       self._reset_zoom(u))
                dpg.bind_item_theme(reset, self._btn_ghost)
            # no_menus disables the right-click context menu whose inputs were
            # bubbling to the window; wheel zoom is handled by ImPlot itself.
            with dpg.plot(tag=tag + "_plot", height=-1, width=-1,
                          no_menus=True, no_title=True) as plot:
                dpg.bind_item_theme(plot, self._plot_theme)
                if legend:
                    dpg.add_plot_legend(
                        location=dpg.mvPlot_Location_NorthWest, outside=False)
                dpg.add_plot_axis(dpg.mvXAxis, tag=x_tag, auto_fit=False)
                with dpg.plot_axis(dpg.mvYAxis, tag=y_tag):
                    if not auto:
                        dpg.set_axis_limits(y_tag, 0, 100)
                    for stag, slbl, scol in series:
                        s = dpg.add_line_series([], [], label=slbl, tag=stag)
                        dpg.bind_item_theme(s, self._series_theme(scol))
        self._chart_tags.append(tag)
        self._chart_axes[tag] = (x_tag, y_tag)
        self._user_zoom[x_tag] = False
        self._user_zoom[y_tag] = False
        if auto:
            self._auto_y_axes.add(y_tag)
            self._axis_cur[y_tag] = 1.0
            self._axis_target[y_tag] = 1.0
        return tag

    # ─────────────────────────────────────────────────────────────────────────
    # Header / footer
    # ─────────────────────────────────────────────────────────────────────────
    def _load_logo_texture(self):
        """Load Spanda.png as a 128px RGBA header texture."""
        self._logo_tex = None
        path = _resource_path("Spanda.png")
        try:
            from PIL import Image
            im = Image.open(path).convert("RGBA")
            im.thumbnail((128, 128), Image.LANCZOS)
            canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
            canvas.alpha_composite(im, ((128 - im.width) // 2,
                                        (128 - im.height) // 2))
            data = [v / 255.0 for px in canvas.getdata() for v in px]
            with dpg.texture_registry():
                self._logo_tex = dpg.add_static_texture(128, 128, data)
        except Exception:
            self._logo_tex = None

    def _build_header(self):
        s = self._sys
        with dpg.child_window(tag="header", width=-1, height=HEADER_H,
                              border=False, no_scrollbar=True):
            dpg.add_spacer(height=5)
            with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp,
                           pad_outerX=False):
                dpg.add_table_column(width_stretch=True, init_width_or_weight=1.0)
                dpg.add_table_column(width_fixed=True, init_width_or_weight=420)
                with dpg.table_row():
                    with dpg.group(horizontal=True):
                        if self._logo_tex:
                            dpg.add_image(self._logo_tex, width=52, height=52,
                                          tag="header_logo")
                        else:
                            with dpg.drawlist(width=48, height=48):
                                dpg.draw_rectangle(
                                    (1, 1), (47, 47), color=_a(C_CPU, 110),
                                    fill=_a(C_CPU, 26), rounding=10, thickness=1)
                                dpg.draw_rectangle((11, 28), (16, 40),
                                                   fill=_l(C_CPU))
                                dpg.draw_rectangle((21, 19), (26, 40),
                                                   fill=_l(C_CPU))
                                dpg.draw_rectangle((31, 11), (36, 40),
                                                   fill=_l(C_CPU))
                        dpg.add_spacer(width=10)
                        brand = dpg.add_text("SPANDA", color=_l(C_CPU),
                                             tag="brand_name")
                        dpg.bind_item_font(brand, self._fonts.get("brand"))
                        dpg.add_spacer(width=16)
                        dpg.add_text("|", color=_l(C_BORDER))
                        dpg.add_spacer(width=16)
                        self._title(
                            f"{s['os']}  •  {s['cpu_log']} cores  •  "
                            f"{s['ram']} GB RAM  •  {s['node']}", C_DIM, "small")
                    with dpg.group(horizontal=True):
                        dpg.add_text("", tag="hdr_batt", color=_l(C_DIM))
                        dpg.add_spacer(width=10)
                        dpg.add_text("", tag="hdr_uptime", color=_l(C_DIM))
                        dpg.add_spacer(width=14)
                        with dpg.drawlist(width=18, height=18):
                            dpg.draw_circle((9, 9), 8, color=_a(C_GREEN, 0),
                                            tag="live_halo")
                            dpg.draw_circle((9, 9), 5, color=_a(C_GREEN, 220),
                                            fill=_a(C_GREEN, 220), tag="live_dot")
                        dpg.add_spacer(width=4)
                        status = dpg.add_text("LIVE", tag="status_text",
                                              color=_l(C_GREEN))
                        dpg.bind_item_font(status, self._fonts.get("small"))
                        dpg.add_spacer(width=16)
                        btn = dpg.add_button(label="Pause", tag="toggle_btn",
                                             callback=self._toggle, width=96,
                                             height=30)
                        dpg.bind_item_theme(btn, self._btn_pause)

    def _build_footer(self):
        with dpg.child_window(tag="footer", width=-1, height=FOOTER_H,
                              border=False, no_scrollbar=True):
            with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp,
                           pad_outerX=False):
                dpg.add_table_column(width_stretch=True, init_width_or_weight=1.0)
                dpg.add_table_column(width_fixed=True, init_width_or_weight=230)
                with dpg.table_row():
                    with dpg.group(horizontal=True):
                        dpg.add_text("●", color=_l(C_GREEN))
                        dpg.add_text("1 Hz", color=_l(C_DIM))
                        dpg.add_spacer(width=10)
                        dpg.add_text("|", color=_l(C_BORDER))
                        dpg.add_spacer(width=10)
                        dpg.add_text("", tag="ftr_samples", color=_l(C_DIM))
                        dpg.add_spacer(width=10)
                        dpg.add_text("|", color=_l(C_BORDER))
                        dpg.add_spacer(width=10)
                        dpg.add_text("", tag="ftr_db", color=_l(C_DIM))
                        dpg.add_spacer(width=10)
                        dpg.add_text("|", color=_l(C_BORDER))
                        dpg.add_spacer(width=10)
                        dpg.add_text("", tag="ftr_msg", color=_l(C_MUTED))
                    with dpg.group(horizontal=True):
                        dpg.add_text(f"v{APP_VERSION}", color=_l(C_MUTED))
                        dpg.add_spacer(width=10)
                        dpg.add_text("|", color=_l(C_BORDER))
                        dpg.add_spacer(width=10)
                        dpg.add_text("", tag="ftr_fps", color=_l(C_MUTED))

    # ─────────────────────────────────────────────────────────────────────────
    # Overview tab
    # ─────────────────────────────────────────────────────────────────────────
    def _metric_card(self, tag, title, color, bar_tag, val_tag, sub_tag, sub_text):
        with self._card(tag, 110):
            with dpg.group(horizontal=True):
                self._title(title, color, "small")
                dpg.add_spacer(width=8)
                self._value("0.0%", val_tag, C_WHITE, "big")
            dpg.add_spacer(height=2)
            dpg.add_progress_bar(tag=bar_tag, default_value=0,
                                 width=-1, height=8)
            dpg.add_spacer(height=5)
            self._title(sub_text, C_DIM, "small", tag=sub_tag)

    def _build_overview_tab(self):
        with dpg.tab(label="  Overview  ", tag="tab_overview"):
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchSame,
                           pad_outerX=False):
                for _ in range(4):
                    dpg.add_table_column()
                with dpg.table_row():
                    self._metric_card(
                        "ov_card_cpu", "CPU", C_CPU, "ov_cpu_bar",
                        "ov_cpu_val", "ov_cpu_sub",
                        f"{self._sys['cpu_log']} logical cores")
                    self._metric_card(
                        "ov_card_mem", "MEMORY", C_MEM, "ov_mem_bar",
                        "ov_mem_val", "ov_mem_sub",
                        f"Total {self._sys['ram']} GB")
                    self._metric_card(
                        "ov_card_disk", "DISK", C_DISK, "ov_disk_bar",
                        "ov_disk_val", "ov_disk_sub",
                        f"System drive {_fmt_gb(self._sys['disk_t'])}")
                    self._metric_card(
                        "ov_card_gpu", "GPU", C_GPU, "ov_gpu_bar",
                        "ov_gpu_val", "ov_gpu_sub",
                        _short(self._sys["gpu"], 28))
            dpg.add_spacer(height=GAP)
            with dpg.child_window(tag="ov_strip", width=-1, height=42,
                                  border=True, no_scrollbar=True) as cw:
                dpg.bind_item_theme(cw, self._strip_theme)
                with dpg.group(horizontal=True):
                    self._title("NETWORK", C_NET, "small")
                    dpg.add_spacer(width=8)
                    dpg.add_text("↑ —", tag="ov_net_up", color=_l(C_NET))
                    dpg.add_spacer(width=14)
                    dpg.add_text("↓ —", tag="ov_net_down", color=_l(C_NET2))
                    dpg.add_spacer(width=22)
                    dpg.add_text("", tag="ov_proc_lbl", color=_l(C_DIM))
                    dpg.add_spacer(width=22)
                    dpg.add_text("", tag="ov_batt_lbl", color=_l(C_DIM))
            dpg.add_spacer(height=GAP)
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchSame,
                           pad_outerX=False):
                for _ in range(3):
                    dpg.add_table_column()
                with dpg.table_row():
                    self._chart("ov_chart_cpu", "CPU %", C_CPU,
                                "ov_xax_cpu", "ov_yax_cpu",
                                [("ov_cpu_s", "CPU %", C_CPU)], height=200)
                    self._chart("ov_chart_mem", "Memory %", C_MEM,
                                "ov_xax_mem", "ov_yax_mem",
                                [("ov_mem_s", "Memory %", C_MEM)], height=200)
                    self._chart("ov_chart_gpu", "GPU %", C_GPU,
                                "ov_xax_gpu", "ov_yax_gpu",
                                [("ov_gpu_s", "GPU %", C_GPU)], height=200)
            dpg.add_spacer(height=GAP)
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchProp,
                           pad_outerX=False):
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=1.0)
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=2.0)
                with dpg.table_row():
                    self._chart("ov_chart_disk", "Disk %", C_DISK,
                                "ov_xax_disk", "ov_yax_disk",
                                [("ov_disk_s", "Disk %", C_DISK)], height=200)
                    self._chart("ov_chart_net", "Network", C_NET,
                                "ov_xax_net", "ov_yax_net",
                                [("ov_net_s1", "Upload", C_NET),
                                 ("ov_net_s2", "Download", C_NET2)],
                                height=200, auto=True, legend=True)

    # ─────────────────────────────────────────────────────────────────────────
    # CPU tab
    # ─────────────────────────────────────────────────────────────────────────
    def _build_cpu_tab(self):
        s = self._sys
        with dpg.tab(label="  CPU  ", tag="tab_cpu"):
            with self._card("cpu_info", 48):
                with dpg.group(horizontal=True):
                    self._value(s["cpu_name"], "cpu_info_name", C_CPU, "body")
                    dpg.add_spacer(width=14)
                    dpg.add_text("|", color=_l(C_BORDER))
                    dpg.add_spacer(width=14)
                    self._title(
                        f"{s['cpu_log']} logical / {s['cpu_phys']} physical  •  "
                        f"Max {s['cpu_max_freq']}  •  {s['arch']}",
                        C_DIM, "small")
            dpg.add_spacer(height=GAP)
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchProp,
                           pad_outerX=False):
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=0.9)
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=2.1)
                with dpg.table_row():
                    with self._card("cpu_overall_card", 200):
                        self._title("OVERALL UTILIZATION", C_CPU, "small")
                        self._value("0.0%", "cpu_overall_val", C_WHITE, "huge")
                        dpg.add_spacer(height=2)
                        dpg.add_progress_bar(tag="cpu_overall_bar",
                                             default_value=0, width=-1,
                                             height=10)
                        dpg.add_spacer(height=8)
                        dpg.add_separator()
                        dpg.add_spacer(height=6)
                        with dpg.table(header_row=False,
                                       policy=dpg.mvTable_SizingStretchSame,
                                       pad_outerX=False):
                            for _ in range(2):
                                dpg.add_table_column()
                            for row_items in (
                                (("Frequency", "cpu_freq_val"),
                                 ("Temperature", "cpu_temp_val")),
                                (("Processes", "cpu_procs_val"),
                                 ("Threads", "cpu_threads_val")),
                                (("Context switches", "cpu_ctx_val"),
                                 ("Interrupts", "cpu_int_val")),
                            ):
                                with dpg.table_row():
                                    for lbl, tag in row_items:
                                        self._stat(lbl, tag, C_TEXT)
                    with self._card("cpu_cores_card", 200):
                        with dpg.group(horizontal=True):
                            self._title("PER-CORE UTILIZATION", C_CPU, "small")
                            dpg.add_spacer(width=12)
                            self._title("", C_DIM, "small", tag="cpu_core_summary")
                        dpg.add_spacer(height=4)
                        n = max(1, self._display_cores)
                        if n <= 16:
                            ncols = 4
                        elif n <= 24:
                            ncols = 6
                        else:
                            ncols = 8
                        nrows = (n + ncols - 1) // ncols
                        with dpg.table(header_row=False,
                                       policy=dpg.mvTable_SizingStretchSame,
                                       pad_outerX=False):
                            for _ in range(ncols):
                                dpg.add_table_column()
                            for r in range(nrows):
                                with dpg.table_row():
                                    for c in range(ncols):
                                        idx = r * ncols + c
                                        if idx >= n:
                                            with dpg.group():
                                                pass
                                            continue
                                        with dpg.group():
                                            with dpg.group(horizontal=True):
                                                self._title(f"C{idx}", C_DIM,
                                                            "small")
                                                dpg.add_spacer(width=4)
                                                self._value("0%", f"cpu_core_{idx}_lbl",
                                                            C_CPU, "small")
                                            dpg.add_progress_bar(
                                                tag=f"cpu_core_{idx}_bar",
                                                default_value=0, width=-1,
                                                height=6)
                                            dpg.add_spacer(height=3)
            dpg.add_spacer(height=GAP)
            self._chart("cpu_chart", "CPU Usage History", C_CPU,
                        "cpu_xax", "cpu_yax",
                        [("cpu_series", "CPU %", C_CPU)], height=300)

    # ─────────────────────────────────────────────────────────────────────────
    # Memory tab
    # ─────────────────────────────────────────────────────────────────────────
    def _build_memory_tab(self):
        with dpg.tab(label="  Memory  ", tag="tab_memory"):
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchProp,
                           pad_outerX=False):
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=1.35)
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=1.0)
                with dpg.table_row():
                    with self._card("mem_phy_card", 128):
                        with dpg.group(horizontal=True):
                            self._title("PHYSICAL MEMORY", C_MEM, "small")
                            dpg.add_spacer(width=10)
                            self._value("0.0%", "mem_phy_val", C_WHITE, "big")
                            dpg.add_spacer(width=10)
                            self._title(f"Total {self._sys['ram']} GB",
                                        C_DIM, "small")
                        dpg.add_spacer(height=2)
                        dpg.add_progress_bar(tag="mem_phy_bar",
                                             default_value=0, width=-1,
                                             height=10)
                        dpg.add_spacer(height=6)
                        with dpg.table(header_row=False,
                                       policy=dpg.mvTable_SizingStretchSame,
                                       pad_outerX=False):
                            for _ in range(4):
                                dpg.add_table_column()
                            with dpg.table_row():
                                self._stat("Used", "mem_used_val", C_MEM)
                                self._stat("Available", "mem_avail_val", C_TEXT)
                                self._stat("Free", "mem_free_val", C_TEXT)
                                self._stat("Total", "mem_total_val", C_TEXT)
                    with self._card("mem_swap_card", 128):
                        with dpg.group(horizontal=True):
                            self._title("SWAP / PAGE FILE", C_SWAP, "small")
                            dpg.add_spacer(width=10)
                            self._value("0.0%", "mem_swap_val", C_WHITE, "big")
                        dpg.add_spacer(height=2)
                        dpg.add_progress_bar(tag="mem_swap_bar",
                                             default_value=0, width=-1,
                                             height=10)
                        dpg.add_spacer(height=6)
                        with dpg.table(header_row=False,
                                       policy=dpg.mvTable_SizingStretchSame,
                                       pad_outerX=False):
                            for _ in range(3):
                                dpg.add_table_column()
                            with dpg.table_row():
                                self._stat("Used", "mem_swap_used", C_SWAP)
                                self._stat("Free", "mem_swap_free", C_TEXT)
                                self._stat("Total", "mem_swap_total", C_TEXT)
            dpg.add_spacer(height=GAP)
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchSame,
                           pad_outerX=False):
                for _ in range(2):
                    dpg.add_table_column()
                with dpg.table_row():
                    self._chart("mem_chart", "RAM Usage %", C_MEM,
                                "mem_xax", "mem_yax",
                                [("mem_series", "RAM %", C_MEM)], height=260)
                    self._chart("swap_chart", "Swap Usage %", C_SWAP,
                                "swap_xax", "swap_yax",
                                [("swap_series", "Swap %", C_SWAP)], height=260)

    # ─────────────────────────────────────────────────────────────────────────
    # Disk tab
    # ─────────────────────────────────────────────────────────────────────────
    def _build_disk_tab(self):
        with dpg.tab(label="  Disk  ", tag="tab_disk"):
            with self._card("disk_io_card", 50):
                with dpg.group(horizontal=True):
                    self._title("DISK I/O", C_DISK, "small")
                    dpg.add_spacer(width=14)
                    self._title("Read", C_DIM, "small")
                    self._value("0.00 MB/s", "disk_read_val", C_MEM, "body")
                    dpg.add_spacer(width=18)
                    self._title("Write", C_DIM, "small")
                    self._value("0.00 MB/s", "disk_write_val", C_AMBER, "body")
                    dpg.add_spacer(width=18)
                    self._title("", C_DIM, "small", tag="disk_vol_count")
            dpg.add_spacer(height=GAP)
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchProp,
                           pad_outerX=False):
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=1.15)
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=1.85)
                with dpg.table_row():
                    with self._card("disk_parts_card", 260):
                        with dpg.group(horizontal=True):
                            self._title("VOLUMES", C_DISK, "small")
                            dpg.add_spacer(width=10)
                            self._title("used / free / total", C_MUTED, "small")
                        dpg.add_spacer(height=2)
                        with dpg.table(tag="disk_table", header_row=True,
                                       borders_innerH=True, borders_outerH=True,
                                       borders_innerV=False, borders_outerV=True,
                                       row_background=True, scrollY=True,
                                       height=220, resizable=True,
                                       no_host_extendX=True,
                                       policy=dpg.mvTable_SizingStretchProp):
                            dpg.add_table_column(label="Drive",
                                                 width_stretch=True,
                                                 init_width_or_weight=0.8)
                            dpg.add_table_column(label="Usage",
                                                 width_stretch=True,
                                                 init_width_or_weight=1.4)
                            dpg.add_table_column(label="%",
                                                 width_fixed=True,
                                                 init_width_or_weight=48)
                            dpg.add_table_column(label="Used",
                                                 width_fixed=True,
                                                 init_width_or_weight=76)
                            dpg.add_table_column(label="Free",
                                                 width_fixed=True,
                                                 init_width_or_weight=76)
                            dpg.add_table_column(label="Total",
                                                 width_fixed=True,
                                                 init_width_or_weight=76)
                            dpg.add_table_column(label="Type",
                                                 width_fixed=True,
                                                 init_width_or_weight=58)
                    self._chart("disk_chart", "Disk I/O", C_DISK,
                                "disk_xax", "disk_yax",
                                [("disk_rd_s", "Read MB/s", C_MEM),
                                 ("disk_wr_s", "Write MB/s", C_AMBER)],
                                height=260, auto=True, legend=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Network tab
    # ─────────────────────────────────────────────────────────────────────────
    def _build_network_tab(self):
        with dpg.tab(label="  Network  ", tag="tab_network"):
            with self._card("net_tp_card", 96):
                with dpg.group(horizontal=True):
                    self._title("THROUGHPUT", C_NET, "small")
                dpg.add_spacer(height=2)
                with dpg.table(header_row=False,
                               policy=dpg.mvTable_SizingStretchSame,
                               pad_outerX=False):
                    for _ in range(4):
                        dpg.add_table_column()
                    with dpg.table_row():
                        self._stat("Upload", "net_sent_val", C_NET, big=True)
                        self._stat("Download", "net_recv_val", C_NET2, big=True)
                        self._stat("Total sent", "net_tsent_val", C_TEXT)
                        self._stat("Total received", "net_trecv_val", C_TEXT)
            dpg.add_spacer(height=GAP)
            with self._card("net_nic_card", 220):
                with dpg.group(horizontal=True):
                    self._title("ADAPTERS", C_NET, "small")
                    dpg.add_spacer(width=10)
                    self._title("live link state", C_MUTED, "small")
                dpg.add_spacer(height=2)
                with dpg.table(tag="nic_table", header_row=True,
                               borders_innerH=True, borders_outerH=True,
                               borders_innerV=False, borders_outerV=True,
                               row_background=True, scrollY=True,
                               height=170, resizable=True,
                               no_host_extendX=True,
                               policy=dpg.mvTable_SizingStretchProp):
                    for lbl, w, stretch in (
                        ("Adapter", 1.6, True), ("Status", 70, False),
                        ("Link", 92, False), ("Sent", 92, False),
                        ("Received", 100, False), ("Packets", 100, False),
                        ("Errors", 62, False),
                    ):
                        if stretch:
                            dpg.add_table_column(
                                label=lbl, width_stretch=True,
                                init_width_or_weight=w)
                        else:
                            dpg.add_table_column(
                                label=lbl, width_fixed=True,
                                init_width_or_weight=w)
            dpg.add_spacer(height=GAP)
            self._chart("net_chart", "Network Throughput", C_NET,
                        "net_xax", "net_yax",
                        [("net_sent_s", "Upload", C_NET),
                         ("net_recv_s", "Download", C_NET2)],
                        height=260, auto=True, legend=True)

    # ─────────────────────────────────────────────────────────────────────────
    # GPU tab
    # ─────────────────────────────────────────────────────────────────────────
    def _build_gpu_tab(self):
        with dpg.tab(label="  GPU  ", tag="tab_gpu"):
            if not self._has_gpu:
                with self._card("gpu_empty", 500, no_scrollbar=True):
                    dpg.add_spacer(height=30)
                    self._value("GPU", "gpu_empty_title", C_GPU, "huge")
                    dpg.add_spacer(height=6)
                    if not GPU_LIB:
                        self._value("GPU monitoring is not enabled",
                                    "gpu_empty_sub", C_TEXT, "big")
                        dpg.add_spacer(height=8)
                        self._title(
                            "Install the optional NVIDIA monitoring library, "
                            "then restart Spanda:", C_DIM, "body")
                        dpg.add_spacer(height=8)
                        cmd = dpg.add_text("pip install gputil",
                                           color=_l(C_AMBER), tag="gpu_install_cmd")
                        dpg.bind_item_font(cmd, self._fonts.get("mono"))
                        dpg.add_spacer(height=10)
                        dpg.add_button(
                            label="Copy install command",
                            callback=lambda: (dpg.set_clipboard_text(
                                "pip install gputil"), self._toast(
                                    "Copied install command", C_GREEN)),
                            width=190)
                    else:
                        self._value("No compatible GPU detected",
                                    "gpu_empty_sub", C_TEXT, "big")
                        dpg.add_spacer(height=8)
                        self._title(
                            "GPUtil is installed but did not report an NVIDIA "
                            "GPU. Integrated GPUs and some drivers are not "
                            "exposed through this API.", C_DIM, "body")
                    dpg.add_spacer(height=18)
                    self._title(
                        "CPU, memory, disk, network and process monitoring "
                        "remain fully available.", C_MUTED, "small")
                return
            with self._card("gpu_info", 48):
                with dpg.group(horizontal=True):
                    self._title("GPU", C_DIM, "small")
                    dpg.add_spacer(width=8)
                    self._value(_short(self._sys["gpu"], 70), "gpu_name",
                                C_GPU, "body")
            dpg.add_spacer(height=GAP)
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchSame,
                           pad_outerX=False):
                for _ in range(3):
                    dpg.add_table_column()
                with dpg.table_row():
                    with self._card("gpu_load_card", 128):
                        self._title("GPU LOAD", C_GPU, "small")
                        self._value("0.0%", "gpu_load_val", C_WHITE, "huge")
                        dpg.add_spacer(height=2)
                        dpg.add_progress_bar(tag="gpu_load_bar",
                                             default_value=0, width=-1,
                                             height=10)
                        dpg.add_spacer(height=4)
                        self._title("Core utilization", C_DIM, "small")
                    with self._card("gpu_temp_card", 128):
                        self._title("TEMPERATURE", C_AMBER, "small")
                        self._value("0 °C", "gpu_temp_val", C_WHITE, "huge")
                        dpg.add_spacer(height=6)
                        with dpg.group(horizontal=True):
                            self._title("Fan", C_DIM, "small")
                            dpg.add_spacer(width=8)
                            self._value("0%", "gpu_fan_val", C_TEXT, "body")
                        dpg.add_spacer(height=4)
                        self._title("Core temperature / fan speed",
                                    C_DIM, "small")
                    with self._card("gpu_mem_card", 128):
                        self._title("VRAM", C_NET2, "small")
                        self._value("0.0%", "gpu_mem_val", C_WHITE, "huge")
                        dpg.add_spacer(height=2)
                        dpg.add_progress_bar(tag="gpu_mem_bar",
                                             default_value=0, width=-1,
                                             height=10)
                        dpg.add_spacer(height=4)
                        self._value("0 / 0 MB", "gpu_mem_detail", C_DIM, "small")
            dpg.add_spacer(height=GAP)
            self._chart("gpu_chart", "GPU Load History", C_GPU,
                        "gpu_xax", "gpu_yax",
                        [("gpu_series", "GPU %", C_GPU)], height=300)

    # ─────────────────────────────────────────────────────────────────────────
    # Processes tab
    # ─────────────────────────────────────────────────────────────────────────
    def _build_process_tab(self):
        # Textures must exist before any row or detail icon is created.
        self._init_icon_textures()
        with dpg.tab(label="  Processes  ", tag="tab_processes"):
            with self._card("proc_toolbar", 50):
                with dpg.group(horizontal=True):
                    self._title("SEARCH", C_DIM, "small")
                    dpg.add_input_text(tag="proc_filter",
                                       hint="Name, PID, user or status",
                                       callback=self._on_proc_filter, width=250,
                                       height=28)
                    dpg.add_button(label="Clear", callback=lambda s, a: self._clear_filter(),
                                   width=54, height=28)
                    dpg.bind_item_theme(dpg.last_item(), self._btn_ghost)
                    dpg.add_spacer(width=16)
                    self._title("VIEW", C_DIM, "small")
                    expand = dpg.add_button(
                        label="Expand all", tag="proc_expand_btn",
                        callback=lambda s, a: self._on_expand_all_click(),
                        width=92, height=28)
                    dpg.bind_item_theme(expand, self._btn_ghost)
                    with dpg.tooltip(expand):
                        dpg.add_text("Show or hide every individual PID")
                    self._value("0 apps", "proc_groups_lbl", C_DIM, "small")
                    dpg.add_spacer(width=16)
                    self._title("SORT BY", C_DIM, "small")
                    # A real, working sort control: pick a column here, then
                    # flip the arrow button.  Header clicks do the same thing.
                    combo = dpg.add_combo(
                        list(self._proc_sort_labels.values()),
                        default_value=self._proc_sort_labels[self._proc_sort_col],
                        tag="proc_sort_combo", callback=self._on_proc_sort_combo,
                        width=110)
                    dpg.bind_item_theme(combo, self._btn_ghost)
                    with dpg.tooltip(combo):
                        dpg.add_text("Sort grouped apps and visible PIDs")
                    direction = dpg.add_button(
                        label="▼", tag="proc_sort_dir_btn",
                        callback=lambda s, a: self._toggle_proc_sort_dir(),
                        width=32, height=28)
                    dpg.bind_item_theme(direction, self._btn_active)
                    with dpg.tooltip(direction):
                        dpg.add_text("▼ largest first   •   ▲ smallest first")
                    dpg.add_spacer(width=16)
                    end = dpg.add_button(label="End task", tag="end_proc_btn",
                                         callback=lambda s, a: self._ask_end_process(),
                                         width=104, height=28, enabled=False)
                    dpg.bind_item_theme(end, self._btn_danger)
            dpg.add_spacer(height=GAP)
            with dpg.table(header_row=False,
                           policy=dpg.mvTable_SizingStretchProp,
                           pad_outerX=False):
                dpg.add_table_column(width_stretch=True,
                                     init_width_or_weight=1.0)
                dpg.add_table_column(width_fixed=True,
                                     init_width_or_weight=316)
                with dpg.table_row():
                    with self._card("proc_table_card", 300, no_scrollbar=True):
                        # sortable=True gives native header arrows and sends
                        # clicks to _on_proc_sort; the actual ordering is done
                        # on our numeric data (sorting formatted text would be
                        # wrong for values like "9.5%" vs "12.0%").
                        with dpg.table(tag="proc_table", header_row=True,
                                       borders_innerH=True, borders_outerH=True,
                                       borders_innerV=False, borders_outerV=True,
                                       row_background=True, scrollY=True,
                                       freeze_rows=1, clipper=True,
                                       height=288, resizable=True,
                                       no_host_extendX=True, sortable=True,
                                       callback=self._on_proc_sort,
                                       policy=dpg.mvTable_SizingStretchProp):
                            dpg.add_table_column(label="", width_fixed=True,
                                                 init_width_or_weight=48)
                            dpg.add_table_column(label="Name",
                                                 width_stretch=True,
                                                 init_width_or_weight=2.2)
                            dpg.add_table_column(label="PID", width_fixed=True,
                                                 init_width_or_weight=64)
                            dpg.add_table_column(label="Status", width_fixed=True,
                                                 init_width_or_weight=88)
                            dpg.add_table_column(label="CPU", width_fixed=True,
                                                 init_width_or_weight=70,
                                                 default_sort=True,
                                                 prefer_sort_descending=True)
                            dpg.add_table_column(label="Memory %", width_fixed=True,
                                                 init_width_or_weight=78,
                                                 prefer_sort_descending=True)
                            dpg.add_table_column(label="Memory", width_fixed=True,
                                                 init_width_or_weight=94,
                                                 prefer_sort_descending=True)
                            dpg.add_table_column(label="Threads", width_fixed=True,
                                                 init_width_or_weight=68,
                                                 prefer_sort_descending=True)
                            dpg.add_table_column(label="User", width_fixed=True,
                                                 init_width_or_weight=104)
                    with self._card("proc_details_card", 300):
                        self._title("PROCESS DETAILS", C_ACCENT, "small")
                        dpg.add_spacer(height=4)
                        with dpg.group(tag="det_placeholder"):
                            dpg.add_spacer(height=18)
                            self._title("Select an app or PID in the table",
                                        C_DIM, "body")
                            dpg.add_spacer(height=4)
                            self._title("Its instances, path, owner, start time "
                                        "and live usage will appear here.",
                                        C_MUTED, "small")
                        with dpg.group(tag="det_body", show=False):
                            with dpg.group(horizontal=True):
                                dpg.add_image(self._icon_fallback or 0,
                                              width=24, height=24,
                                              tag="det_proc_icon")
                                self._value("—", "det_name", C_WHITE, "big",
                                            wrap=252)
                            dpg.add_spacer(height=4)
                            with dpg.table(header_row=False,
                                           policy=dpg.mvTable_SizingStretchSame,
                                           pad_outerX=False):
                                for _ in range(2):
                                    dpg.add_table_column()
                                for a, b in (
                                    ("PID", "det_pid"), ("User", "det_user"),
                                    ("Status", "det_status"), ("Started", "det_started"),
                                    ("CPU", "det_cpu"), ("Threads", "det_threads"),
                                ):
                                    with dpg.table_row():
                                        self._stat(a, b, C_TEXT)
                            dpg.add_spacer(height=2)
                            self._stat("Memory", "det_mem", C_TEXT)
                            dpg.add_spacer(height=6)
                            self._title("Executable", C_DIM, "small")
                            self._title("—", C_TEXT, "small", tag="det_exe", wrap=280)
                            dpg.add_spacer(height=4)
                            self._title("Command line", C_DIM, "small")
                            self._title("—", C_MUTED, "small", tag="det_cmd", wrap=280)
                            dpg.add_spacer(height=10)
                            with dpg.group(horizontal=True):
                                b1 = dpg.add_button(label="Copy PID", tag="det_copy_btn",
                                                    callback=lambda s, a: self._copy_pid(),
                                                    width=82, height=28, enabled=False)
                                dpg.bind_item_theme(b1, self._btn_ghost)
                                b2 = dpg.add_button(label="Open folder", tag="det_open_btn",
                                                    callback=lambda s, a: self._open_location(),
                                                    width=98, height=28, enabled=False)
                                dpg.bind_item_theme(b2, self._btn_ghost)
                            dpg.add_spacer(height=6)
                            b3 = dpg.add_button(label="End Process", tag="det_end_btn",
                                                callback=lambda s, a: self._ask_end_process(),
                                                width=118, height=30, enabled=False)
                            dpg.bind_item_theme(b3, self._btn_danger)
            dpg.add_spacer(height=4)
            with dpg.group(horizontal=True):
                self._title("SHOWING", C_DIM, "small")
                self._value("0 / 0", "proc_shown_lbl", C_ACCENT, "small")
                dpg.add_spacer(width=12)
                dpg.add_text("|", color=_l(C_BORDER))
                dpg.add_spacer(width=12)
                self._title("PROCESSES", C_DIM, "small")
                self._value("0", "proc_count_lbl", C_TEXT, "small")
                dpg.add_spacer(width=12)
                dpg.add_text("|", color=_l(C_BORDER))
                dpg.add_spacer(width=12)
                self._title("THREADS", C_DIM, "small")
                self._value("0", "proc_threads_lbl", C_TEXT, "small")

        # Pre-create the row pool so the first data tick never stalls a frame
        try:
            initial = len(_win_process_snapshot() or [])
        except Exception:
            initial = 120
        self._ensure_proc_rows(min(max(initial or 120, 60), MAX_PROC_ROWS))

    # ─────────────────────────────────────────────────────────────────────────
    # Specs tab
    # ─────────────────────────────────────────────────────────────────────────
    def _build_specs_tab(self):
        with dpg.tab(label="  Specs  ", tag="tab_specs"):
            with dpg.child_window(width=-1, height=-1, border=False):
                self._title("FULL SYSTEM SPECIFICATION", C_ACCENT, "body")
                self._title("Hardware and operating-system inventory detected "
                            "at startup.", C_MUTED, "small")
                dpg.add_spacer(height=GAP)
                for i, (section, rows) in enumerate(self._specs):
                    lines = sum(str(v).count("\n") + 1 for _, v in rows)
                    height = 40 + 24 * max(1, lines)
                    with self._card(f"spec_card_{i}", height):
                        self._title(section.upper(), C_ACCENT, "small")
                        dpg.add_spacer(height=4)
                        with dpg.table(header_row=False,
                                       policy=dpg.mvTable_SizingStretchProp,
                                       pad_outerX=False):
                            dpg.add_table_column(width_fixed=True,
                                                 init_width_or_weight=170)
                            dpg.add_table_column(width_stretch=True,
                                                 init_width_or_weight=1.0)
                            for label, value in rows:
                                with dpg.table_row():
                                    self._title(str(label), C_DIM, "small")
                                    txt = dpg.add_text(
                                        str(value), color=_l(C_TEXT), wrap=820)
                                    f = self._fonts.get("body")
                                    if f:
                                        dpg.bind_item_font(txt, f)
                    dpg.add_spacer(height=GAP)

    def _presets(self):
        return [
            ("Avg / Max per metric",
             "SELECT metric_name,\n"
             "       ROUND(AVG(value),2) AS avg,\n"
             "       ROUND(MAX(value),2) AS max,\n"
             "       COUNT(*)            AS samples\n"
             "FROM   system_metrics\n"
             "GROUP  BY metric_name\n"
             "ORDER  BY metric_name"),
            ("CPU spikes > 80%",
             "SELECT ts, ROUND(value,1) AS cpu_pct\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name = 'cpu_percent' AND value > 80\n"
             "ORDER  BY ts DESC LIMIT 50"),
            ("Last 60 CPU readings",
             "SELECT ts, ROUND(value,1) AS cpu_pct\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name = 'cpu_percent'\n"
             "ORDER  BY ts DESC LIMIT 60"),
            ("Mem trend (5-min avg)",
             "SELECT DATE_TRUNC('minute', ts) AS minute,\n"
             "       ROUND(AVG(value),1)       AS avg_mem_pct\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name = 'mem_percent'\n"
             "GROUP  BY 1 ORDER BY 1 DESC LIMIT 30"),
            ("High memory > 80%",
             "SELECT ts, ROUND(value,1) AS mem_pct\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name = 'mem_percent' AND value > 80\n"
             "ORDER  BY ts DESC LIMIT 50"),
            ("Disk I/O summary",
             "SELECT metric_name,\n"
             "       ROUND(AVG(value),2) AS avg,\n"
             "       ROUND(MAX(value),2) AS peak\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name IN ('disk_read_mbps','disk_write_mbps')\n"
             "GROUP  BY metric_name"),
            ("Network summary",
             "SELECT metric_name,\n"
             "       ROUND(AVG(value),2) AS avg_kbps,\n"
             "       ROUND(MAX(value),2) AS peak_kbps\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name IN ('net_sent_kbps','net_recv_kbps')\n"
             "GROUP  BY metric_name"),
            ("GPU temperature peaks",
             "SELECT ts, ROUND(value,1) AS gpu_temp_c\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name = 'gpu_temp_c' AND value > 50\n"
             "ORDER  BY ts DESC LIMIT 30"),
            ("Last hour overall",
             "SELECT metric_name,\n"
             "       ROUND(AVG(value),2) AS avg,\n"
             "       ROUND(MAX(value),2) AS max\n"
             "FROM   system_metrics\n"
             "WHERE  ts >= NOW() - INTERVAL '1 hour'\n"
             "GROUP  BY metric_name ORDER BY metric_name"),
            ("Swap spikes > 50%",
             "SELECT ts, ROUND(value,1) AS swap_pct\n"
             "FROM   system_metrics\n"
             "WHERE  metric_name = 'swap_percent' AND value > 50\n"
             "ORDER  BY ts DESC LIMIT 40"),
        ]

    def _load_sql(self, sql):
        if dpg.does_item_exist("sql_in"):
            dpg.set_value("sql_in", sql)

    # ─────────────────────────────────────────────────────────────────────────
    # Build UI
    # ─────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        dpg.create_context()
        dpg.create_viewport(
            title=f"{APP_NAME} - System Health Dashboard",
            width=1480, height=860, min_width=1180, min_height=680,
        )
        for icon in ("Spanda.ico", "Spanda.png"):
            path = _resource_path(icon)
            if os.path.exists(path):
                try:
                    dpg.set_viewport_small_icon(path)
                    dpg.set_viewport_large_icon(path)
                    break
                except Exception:
                    pass

        self._load_fonts()
        self._load_logo_texture()
        self._create_themes()

        with dpg.window(tag="main_win", no_title_bar=True, no_move=True,
                        no_resize=True, no_collapse=True, no_scrollbar=True,
                        no_scroll_with_mouse=True):
            self._build_header()
            dpg.add_separator()
            with dpg.tab_bar(tag="tabs", callback=self._on_tab_change):
                self._build_overview_tab()
                self._build_cpu_tab()
                self._build_memory_tab()
                self._build_disk_tab()
                self._build_network_tab()
                self._build_gpu_tab()
                self._build_process_tab()
                self._build_specs_tab()
            dpg.add_separator()
            self._build_footer()

        # Modal dialogs
        with dpg.window(tag="confirm_end", modal=True, show=False,
                        no_title_bar=True, no_resize=True, no_move=True,
                        width=460, height=230):
            self._value("End process?", "confirm_end_title", C_WHITE, "big")
            dpg.add_spacer(height=8)
            dpg.add_text("", tag="confirm_end_text", color=_l(C_DIM), wrap=420)
            dpg.add_spacer(height=6)
            dpg.add_text("", tag="confirm_end_warn", color=_l(C_RED), wrap=420)
            dpg.add_spacer(height=12)
            with dpg.group(horizontal=True):
                cancel = dpg.add_button(
                    label="Cancel",
                    callback=lambda s, a: self._cancel_end_process(),
                    width=120, height=32)
                dpg.bind_item_theme(cancel, self._btn_ghost)
                dpg.add_spacer(width=8)
                ok = dpg.add_button(label="End Process",
                                    callback=lambda s, a: self._do_end_process(),
                                    width=140, height=32)
                dpg.bind_item_theme(ok, self._btn_danger)

        with dpg.window(tag="confirm_clear", modal=True, show=False,
                        no_title_bar=True, no_resize=True, no_move=True,
                        width=460, height=210):
            self._value("Clear history?", "confirm_clear_title", C_WHITE, "big")
            dpg.add_spacer(height=8)
            dpg.add_text(
                "All recorded samples in the local DuckDB database will be "
                "deleted. This cannot be undone.",
                color=_l(C_DIM), wrap=420)
            dpg.add_spacer(height=12)
            with dpg.group(horizontal=True):
                cancel = dpg.add_button(
                    label="Cancel",
                    callback=lambda s, a: dpg.configure_item("confirm_clear", show=False),
                    width=120, height=32)
                dpg.bind_item_theme(cancel, self._btn_ghost)
                dpg.add_spacer(width=8)
                ok = dpg.add_button(label="Clear history",
                                    callback=lambda s, a: self._do_clear_db(),
                                    width=140, height=32)
                dpg.bind_item_theme(ok, self._btn_danger)

        dpg.set_primary_window("main_win", True)
        dpg.set_viewport_resize_callback(self._on_resize)
        try:
            dpg.set_exit_callback(self._on_exit)
        except Exception:
            pass
        self._update_sort_buttons()

    def _on_resize(self, sender=None, app_data=None):
        self._relayout()

    def _on_tab_change(self, sender, app_data):
        if app_data == "tab_processes":
            self._refresh_proc_table()
        self._relayout()

    def _on_exit(self, sender=None, app_data=None):
        self._running = False
        self._stop_event.set()

    # ─────────────────────────────────────────────────────────────────────────
    # Run
    # ─────────────────────────────────────────────────────────────────────────
    def run(self):
        dpg.setup_dearpygui()
        dpg.show_viewport()
        try:
            dpg.set_viewport_vsync(True)
        except Exception:
            pass

        smoke = 0
        try:
            smoke = int(_env("SMOKE", "0") or 0)
        except ValueError:
            smoke = 0
        shot = _env("SHOT")
        if smoke:
            try:
                dpg.set_viewport_always_top(True)
                size = _env("SIZE")
                if size:
                    w, h = (int(x) for x in size.lower().split("x"))
                    dpg.set_viewport_width(w)
                    dpg.set_viewport_height(h)
                tab = _env("TAB")
                if tab:
                    dpg.set_value("tabs", tab)
            except Exception as e:
                print(f"[smoke] {e}")

        self._relayout()
        self._toggle(None, None)
        self._refresh_db_stats()
        self._text("ftr_msg", "Ready")
        self._set_color("ftr_msg", C_MUTED)

        last = time.perf_counter()
        smoke_t0 = last
        frame = 0
        try:
            while dpg.is_dearpygui_running():
                now = time.perf_counter()
                dt = min(0.1, max(0.0, now - last))
                last = now

                # Catch initial sizing and DPI changes
                try:
                    size = (dpg.get_viewport_client_width(),
                            dpg.get_viewport_client_height())
                except Exception:
                    size = None
                if size and size != self._last_size:
                    self._last_size = size
                    self._relayout()

                # Detect manual zoom/pan using the limits DPG committed on the
                # previous frame, *before* we overwrite them this frame.
                self._mark_user_zoom()

                if self._new_data:
                    with self._lock:
                        packet = self._latest
                        self._new_data = False
                    try:
                        ta = time.perf_counter()
                        self._apply_packet(packet)
                        if _env("PROFILE"):
                            print(f"[apply] {time.perf_counter() - ta:.3f}s")
                    except Exception as e:
                        print(f"[UI] {e}")

                try:
                    ta = time.perf_counter()
                    self._animate(dt)
                    if _env("PROFILE"):
                        print(f"[anim] {time.perf_counter() - ta:.4f}s")
                except Exception as e:
                    print(f"[ANIM] {e}")

                self._fps_frames += 1
                if now - self._fps_t >= 0.5:
                    elapsed = max(0.001, now - self._fps_t)
                    self._fps = self._fps_frames / elapsed
                    self._fps_frames = 0
                    self._fps_t = now
                    self._update_fps()

                if now - self._db_stats_t > DB_STATS_INTERVAL:
                    self._refresh_db_stats()

                frame += 1
                if smoke and frame >= smoke:
                    if shot:
                        self._capture(shot)
                    print(f"[smoke] {frame} frames in "
                          f"{time.perf_counter() - smoke_t0:.1f}s")
                    dpg.stop_dearpygui()

                dpg.render_dearpygui_frame()
                if _env("PROFILE"):
                    print(f"[render] {time.perf_counter() - now:.4f}s")
        finally:
            self._running = False
            self._stop_event.set()
            if self._bg_thread:
                self._bg_thread.join(timeout=3)
            self._flush_db()
            with self._db_lock:
                if self._conn:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
            try:
                dpg.destroy_context()
            except Exception:
                pass

    def _capture(self, path):
        try:
            from PIL import ImageGrab
            if IS_LINUX:
                # X11/XWayland sessions expose the composited desktop here.
                ImageGrab.grab().save(path)
                print(f"[shot] saved {path}")
                return
            img = None
            try:  # crop to the app window so overlays never pollute the shot
                import ctypes
                from ctypes import wintypes
                u32 = ctypes.windll.user32
                hwnd = u32.FindWindowW(None, f"{APP_NAME} - System Health Dashboard")
                if hwnd:
                    u32.SetForegroundWindow(hwnd)
                    rect = wintypes.RECT()
                    u32.GetWindowRect(hwnd, ctypes.byref(rect))
                    img = ImageGrab.grab(
                        bbox=(rect.left, rect.top, rect.right, rect.bottom),
                        all_screens=True,
                    )
            except Exception:
                img = None
            # A stale/tiny window or an off-screen rect can yield a blank grab;
            # fall back to the full screen in that case.
            if (img is None or img.width < 300 or img.height < 200
                    or len(img.getcolors(maxcolors=8) or []) <= 1):
                img = ImageGrab.grab()
            img.save(path)
            print(f"[shot] saved {path}")
        except Exception as e:
            print(f"[shot] {e}")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = SystemMonitorApp()
    app.run()







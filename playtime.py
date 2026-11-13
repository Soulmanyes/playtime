#!/usr/bin/env python3
"""Playtime tracker: records how long games run in a local SQLite history,
backs it up daily next to it, and optionally mirrors it to a server over SSH.

  playtime.py run            the daemon (systemd user service)
  playtime.py sync           push/pull sessions with the server now
  playtime.py stats          print today/week/month/year/all-time totals
  playtime.py summary        rewrite the summary file the Noctalia panel reads
  playtime.py import         reread Steam's and Prism's own playtime totals
  playtime.py backup         write a local backup of the history now
  playtime.py restore FILE   merge a backup (or any playtime.db) into the history
  playtime.py deploy-server  copy server.py to the server
  playtime.py version        print the installed version
  playtime.py update         install the latest release now (--check: only look)

Games are found by scanning /proc. Steam games run under Steam's `reaper
SteamLaunch AppId=N` process, Prism Launcher's Minecraft runs as java with
org.prismlauncher.EntryPoint, and anything else comes from [[game]] rules in
the config file.

The all-time totals also include what Steam (localconfig.vdf) and Prism
(instance.cfg) recorded before tracking started; see the Import section.
"""

import datetime as dt
import json
import os
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "playtime" / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / "playtime"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / "playtime"
DB_PATH = DATA_DIR / "playtime.db"
BACKUP_DIR = DATA_DIR / "backups"
SUMMARY_PATH = STATE_DIR / "summary.json"
# Per-game session spans for the panel's drill-down chart. A separate file so
# the bar widget, which reads the summary every minute, never parses it.
HISTORY_PATH = STATE_DIR / "history.json"
APP_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = APP_DIR / "server.py"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "playtime"
UPDATE_STATE = STATE_DIR / "update.json"
REPO = "Soulmanyes/playtime"
# How often the daemon asks the updater; it asks GitHub once a day at most.
UPDATE_POLL_SECONDS = 3600

DEFAULTS = {
    # Empty: local only, no server sync.
    "ssh_host": "",
    "remote_dir": "playtime",
    "poll_seconds": 10,
    "sync_minutes": 15,
    # Sessions shorter than this are never saved (crash on launch, a quick
    # menu check).
    "min_session_seconds": 60,
    "ignore_steam_appids": [],
    # Steam app types whose playtime the all-time import counts ("game",
    # "application", "tool", "demo", ...). Apps like Wallpaper Engine are
    # "application" and would otherwise top the list.
    "import_steam_types": ["game"],
    # Daily local backups of the history to keep; 0 turns them off.
    "backup_keep": 7,
    # Install new releases from GitHub by itself (checked once a day).
    "auto_update": True,
}


def suspended_total():
    """Seconds this boot has spent suspended: BOOTTIME counts suspend,
    MONOTONIC does not. Unlike a wall-clock gap, a slow sync or an NTP jump
    does not move it."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


CHECKPOINT_SECONDS = 60
# Heartbeats come every CHECKPOINT_SECONDS; a gap up to this long still
# counts as the daemon running.
UPTIME_GRACE = 2 * CHECKPOINT_SECONDS
# Prism's total may grow by at most the time between two cutoffs; this much
# more still counts as the same counter (rounding, clock steps).
PRISM_SLACK = 120
# A new instance whose total equals another instance's is a copy made with
# "Keep play time" (or a moved folder). Shorter totals may match by chance.
PRISM_COPY_MIN = 60
SUMMARY_SECONDS = 60
BACKUP_SECONDS = 24 * 3600

STEAM_ROOTS = [
    HOME / ".local/share/Steam",
    HOME / ".steam/steam",
    HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
]
# Steam launches these through reaper too, but they are not games.
STEAM_TOOL_PREFIXES = ("Proton", "Steam Linux Runtime", "Steamworks Common")
PRISM_ROOTS = [
    HOME / ".local/share/PrismLauncher",
    HOME / ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    game_key TEXT NOT NULL,
    game_name TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    host TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_end ON sessions(end);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- Totals Steam/Prism recorded themselves. `cutoff` is when that total ends:
-- tracked sessions only add the part after it.
CREATE TABLE IF NOT EXISTS imported (
    key TEXT PRIMARY KEY,
    game_name TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    seconds INTEGER NOT NULL,
    cutoff INTEGER NOT NULL
);
-- When this machine's daemon ran (start, last heartbeat). Prism can only
-- have counted play the tracker missed while no daemon was running.
CREATE TABLE IF NOT EXISTS uptime (
    start INTEGER PRIMARY KEY,
    end INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS steam_apps (
    appid INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL
);
"""

UPSERT = """
INSERT INTO sessions (id, game_key, game_name, detail, start, end, host)
VALUES (:id, :game_key, :game_name, :detail, :start, :end, :host)
ON CONFLICT(id) DO UPDATE SET
    start = min(start, excluded.start),
    end = max(end, excluded.end)
"""

FIELDS = ("id", "game_key", "game_name", "detail", "start", "end", "host")

# Only a newer cutoff with at least as many seconds replaces a total, so a
# baseline never goes down: not through an import, a sync or a restore.
# `seen:` rows hold Prism's raw readings, which can drop (see import_prism);
# for them the newer cutoff alone wins. Names always follow the latest import
# (a name looked up later replaces the "Steam app N" placeholder).
UPSERT_IMPORTED = """
INSERT INTO imported (key, game_name, detail, seconds, cutoff)
VALUES (:key, :game_name, :detail, :seconds, :cutoff)
ON CONFLICT(key) DO UPDATE SET
    game_name = excluded.game_name,
    detail = excluded.detail,
    seconds = CASE WHEN {newer} THEN excluded.seconds ELSE imported.seconds END,
    cutoff = CASE WHEN {newer} THEN excluded.cutoff ELSE imported.cutoff END
""".format(newer="excluded.cutoff > imported.cutoff AND (excluded.seconds >= "
                 "imported.seconds OR imported.key LIKE 'seen:%')")

# Raw Prism readings are stored as imported rows under this prefix, so they
# reach the server and the backups like the baselines do; summaries skip them.
SEEN = "seen:"

IMPORTED_FIELDS = ("key", "game_name", "detail", "seconds", "cutoff")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    games = []
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        games = data.pop("game", [])
        cfg.update(data)
    rules = []
    for g in games:
        name = g.get("name")
        if not name:
            continue
        rules.append({
            "name": name,
            "process": g.get("process"),
            "cmdline": re.compile(g["cmdline"]) if g.get("cmdline") else None,
        })
    cfg["rules"] = rules
    return cfg


def open_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# ── Detection ────────────────────────────────────────────────────────────────

class SteamNames:
    """AppId to name, read from the appmanifest files of every Steam library."""

    def __init__(self):
        self.names = {}
        self.loaded_at = 0

    def _library_dirs(self):
        dirs = []
        for root in STEAM_ROOTS:
            vdf = root / "steamapps" / "libraryfolders.vdf"
            if not vdf.exists():
                continue
            dirs.append(root / "steamapps")
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                dirs.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps")
        seen, out = set(), []
        for d in dirs:
            try:
                key = d.resolve()
            except OSError:
                continue
            if key not in seen:
                seen.add(key)
                out.append(d)
        return out

    def reload(self):
        names = {}
        for d in self._library_dirs():
            for acf in d.glob("appmanifest_*.acf"):
                try:
                    text = acf.read_text(errors="replace")
                except OSError:
                    continue
                appid = re.search(r'"appid"\s+"(\d+)"', text)
                name = re.search(r'"name"\s+"([^"]*)"', text)
                if appid and name:
                    names[int(appid.group(1))] = name.group(1)
        self.names = names
        self.loaded_at = time.time()

    def get(self, appid):
        # A game installed since the last read has no name yet: reread once.
        if appid not in self.names and time.time() - self.loaded_at > 60:
            self.reload()
        return self.names.get(appid)


def read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    return [a.decode(errors="replace") for a in raw.rstrip(b"\0").split(b"\0")]


def read_instance_cfg(inst_dir):
    cfg = {}
    try:
        for line in (inst_dir / "instance.cfg").read_text(errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def prism_instance(pid):
    """(instance folder, display name) from the game's working directory.
    The folder is the stable key: Prism renames the display name freely."""
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None, ""
    parts = cwd.parts
    if "instances" not in parts:
        return None, ""
    i = len(parts) - 1 - parts[::-1].index("instances")
    if i + 1 >= len(parts):
        return None, ""
    folder = parts[i + 1]
    name = read_instance_cfg(Path(*parts[: i + 2])).get("name") or folder
    return folder, name


def shortcut_name(argv):
    """Name for a non-Steam shortcut from the reaper command line: the .exe
    under Proton, else the first program after `--` that is not Steam's own."""
    if "--" not in argv:
        return None
    args = argv[argv.index("--") + 1:]
    for a in reversed(args):
        if a.lower().endswith(".exe"):
            return os.path.basename(a)[:-4]
    for a in args:
        if a.startswith("-") or "/ubuntu12_" in a or "/steamapps/common/" in a:
            continue
        return os.path.basename(a) or None
    return None


def scan(cfg, steam):
    """Return {game_key: (game_name, detail)} for every game running now."""
    found = {}
    ignore = {int(a) for a in cfg["ignore_steam_appids"]}
    rules = cfg["rules"]
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        argv = read_cmdline(entry.name)
        if not argv:
            continue
        base = os.path.basename(argv[0])

        if base == "reaper" and "SteamLaunch" in argv:
            appid = None
            for a in argv:
                if a.startswith("AppId="):
                    try:
                        appid = int(a[6:])
                    except ValueError:
                        pass
                    break
            if appid is None or appid in ignore:
                continue
            name = steam.get(appid)
            if name is None:
                # Non-Steam shortcuts get a generated AppId with no manifest.
                name = shortcut_name(argv) or f"Non-Steam game {appid}"
            if name.startswith(STEAM_TOOL_PREFIXES):
                continue
            found[f"steam:{appid}"] = (name, "")
            continue

        if "org.prismlauncher.EntryPoint" in argv:
            folder, name = prism_instance(entry.name)
            key = f"prism:{folder}" if folder else "minecraft"
            found[key] = ("Minecraft", name)
            continue

        if rules:
            joined = None
            for r in rules:
                if r["process"] and base == r["process"]:
                    found[f"rule:{r['name']}"] = (r["name"], "")
                    break
                if r["cmdline"]:
                    if joined is None:
                        joined = " ".join(argv)
                    if r["cmdline"].search(joined):
                        found[f"rule:{r['name']}"] = (r["name"], "")
                        break
    return found


# ── Import ───────────────────────────────────────────────────────────────────
#
# Steam keeps every game's total playtime for the account in each user's
# localconfig.vdf (the same numbers SteamDB reads through the Web API), with
# LastPlayed = when the game last exited. Prism keeps totalTimePlayed per
# instance. Each import becomes a baseline with a cutoff at the end of the
# last session it includes; all-time adds only tracked time after the cutoff,
# so nothing is counted twice however often the import runs.

VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text):
    """Text KeyValues (localconfig.vdf, libraryfolders.vdf) to nested dicts."""
    root = {}
    stack = [root]
    key = None
    for m in VDF_TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            child = {}
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = s
        else:
            stack[-1][key] = s.replace('\\"', '"').replace("\\\\", "\\")
            key = None
    return root


def vdf_get(d, *path):
    """Case-insensitive walk; Steam is not consistent about key case."""
    for p in path:
        if not isinstance(d, dict):
            return None
        lower = p.lower()
        d = next((v for k, v in d.items() if k.lower() == lower), None)
    return d


def read_appinfo(path, wanted):
    """Name and type of the wanted appids from Steam's binary appinfo.vdf
    cache (formats 28 and 29). Unknown formats return nothing."""
    try:
        data = path.read_bytes()
    except OSError:
        return {}
    magic, = struct.unpack_from("<I", data, 0)
    if magic == 0x07564429:
        table_at, = struct.unpack_from("<Q", data, 8)
        count, = struct.unpack_from("<I", data, table_at)
        strings = data[table_at + 4:].split(b"\0", count)[:count]
        pos = 16
    elif magic == 0x07564428:
        strings = None
        pos = 8
    else:
        return {}

    def cstring(at):
        end = data.index(b"\0", at)
        return data[at:end].decode(errors="replace"), end + 1

    def read_kv(at):
        out = {}
        while True:
            kind = data[at]
            at += 1
            if kind in (0x08, 0x0B):
                return out, at
            if strings is not None:
                idx, = struct.unpack_from("<I", data, at)
                key = strings[idx].decode(errors="replace")
                at += 4
            else:
                key, at = cstring(at)
            if kind == 0x00:
                out[key], at = read_kv(at)
            elif kind == 0x01:
                out[key], at = cstring(at)
            elif kind in (0x02, 0x04, 0x06):
                out[key], = struct.unpack_from("<i", data, at)
                at += 4
            elif kind == 0x03:
                out[key], = struct.unpack_from("<f", data, at)
                at += 4
            elif kind == 0x07:
                out[key], = struct.unpack_from("<Q", data, at)
                at += 8
            elif kind == 0x0A:
                out[key], = struct.unpack_from("<q", data, at)
                at += 8
            else:
                raise ValueError(f"appinfo: unknown value type {kind}")

    found = {}
    while pos + 8 <= len(data):
        appid, size = struct.unpack_from("<II", data, pos)
        if appid == 0:
            break
        start = pos + 8
        pos = start + size
        if appid not in wanted:
            continue
        # info state, last updated, PICS token, SHA-1, change number, binary SHA-1
        try:
            kv, _ = read_kv(start + 4 + 4 + 8 + 20 + 4 + 20)
        except (ValueError, IndexError, struct.error):
            continue
        common = vdf_get(kv, "appinfo", "common") or vdf_get(kv, "common") or {}
        name = common.get("name")
        if name:
            found[appid] = (name, str(common.get("type", "")).lower())
    return found


def steam_roots():
    seen, out = set(), []
    for root in STEAM_ROOTS:
        try:
            key = root.resolve()
        except OSError:
            continue
        if root.exists() and key not in seen:
            seen.add(key)
            out.append(root)
    return out


def steam_playtimes():
    """{appid: (seconds, last_played)} over every Steam user on this PC."""
    totals = {}
    for root in steam_roots():
        for cfg in (root / "userdata").glob("*/config/localconfig.vdf"):
            if cfg.parts[-3] == "0":
                continue
            try:
                apps = vdf_get(parse_vdf(cfg.read_text(errors="replace")),
                               "UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
            except OSError:
                continue
            for appid, info in (apps or {}).items():
                if not appid.isdigit() or not isinstance(info, dict):
                    continue
                appid = int(appid)
                # Non-Steam shortcuts use ids with the top bit set.
                if appid >= 2 ** 31:
                    continue
                try:
                    minutes = int(vdf_get(info, "Playtime") or 0)
                    last = int(vdf_get(info, "LastPlayed") or 0)
                except ValueError:
                    continue
                if minutes > 0 and minutes * 60 > totals.get(appid, (0, 0))[0]:
                    totals[appid] = (minutes * 60, last)
    return totals


def steam_app_info(db, steam, appids):
    """{appid: (name, type)}: names from installed manifests win (they match
    the tracker's names), the rest come from appinfo.vdf, cached in the DB.
    Apps in neither are left for fetch_store_names."""
    known = {r["appid"]: (r["name"], r["type"])
             for r in db.execute("SELECT appid, name, type FROM steam_apps")}
    missing = {a for a in appids if a not in known}
    if missing:
        for root in steam_roots():
            for appid, info in read_appinfo(root / "appcache" / "appinfo.vdf", missing).items():
                known[appid] = info
                db.execute("INSERT OR REPLACE INTO steam_apps (appid, name, type) VALUES (?, ?, ?)",
                           (appid, info[0], info[1]))
        db.commit()
    out = {}
    for appid in appids:
        name, kind = known.get(appid, (None, ""))
        out[appid] = (steam.get(appid) or name or f"Steam app {appid}", kind)
    return out


def missing_app_names(db, appids):
    have = {r[0] for r in db.execute("SELECT appid FROM steam_apps")}
    return [a for a in appids if a not in have]


def fetch_store_names(appids, stopping=lambda: False):
    """Look up names the local caches lack in the public Steam store API (no
    key; it allows about 200 requests per 5 minutes, so this goes slowly).
    Runs in its own thread with its own DB connection. A failed lookup is
    stored with an empty name so it is not retried every start."""
    db = open_db()
    for appid in appids:
        if stopping():
            break
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"
        req = urllib.request.Request(url, headers={"User-Agent": "playtime"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                entry = json.load(r).get(str(appid)) or {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(120)
            continue
        except (OSError, ValueError):
            time.sleep(30)  # offline: try the rest later, slower
            continue
        data = entry.get("data") or {} if entry.get("success") else {}
        db.execute("INSERT OR REPLACE INTO steam_apps (appid, name, type) VALUES (?, ?, ?)",
                   (appid, data.get("name", ""), str(data.get("type", "")).lower()))
        db.commit()
        time.sleep(1.6)
    db.close()


def prism_playtimes():
    """{folder: (display name, seconds, cutoff, last session)} for each Prism
    instance. With both the native and the Flatpak data folder, the newer
    reading wins."""
    out = {}
    for root in PRISM_ROOTS:
        for cfg_path in (root / "instances").glob("*/instance.cfg"):
            cfg = read_instance_cfg(cfg_path.parent)
            try:
                total = int(cfg.get("totalTimePlayed") or 0)
                launched = int(cfg.get("lastLaunchTime") or 0) // 1000
                last = int(cfg.get("lastTimePlayed") or 0)
            except ValueError:
                continue
            if total > 0:
                folder = cfg_path.parent.name
                # lastTimePlayed is the length of the last session.
                if folder not in out or launched + last > out[folder][2]:
                    out[folder] = (cfg.get("name") or folder, total, launched + last, last)
    return out


def tracked_seconds(db, key, a, b):
    """Tracked time of one game inside (a, b]."""
    return sum(max(0, min(e, b) - max(s, a)) for s, e in db.execute(
        "SELECT start, end FROM sessions WHERE game_key = ? AND end > ? AND start < ?",
        (key, a, b)))


def downtime(db, a, b):
    """Seconds of (a, b] when no daemon ran here. Time before the first
    heartbeat counts as down: nothing is known about it."""
    covered, pos = 0, a
    for s, e in db.execute("SELECT start, end FROM uptime WHERE end + ? > ? AND start < ? "
                           "ORDER BY start", (UPTIME_GRACE, a, b)):
        s, e = max(s, pos), min(e + UPTIME_GRACE, b)
        if e > s:
            covered += e - s
            pos = e
    return max(0, b - a - covered)


def prism_gain(db, key, base, seen, seconds, cutoff, last, name):
    """Seconds to add to an instance's baseline for a new reading, or None to
    leave it alone. Prism's counter only says something new about time after
    the stored cutoff; tracked time there is known for sure.

    - Same counter, grown by no more than the time that passed: Prism saw
      `seconds - seen` more; the tracker's figure wins when it saw more.
    - Anything else is a reset or a swapped instance.cfg (reinstalled OS or
      modpack, native/Flatpak switch, a restored backup); the old baseline
      stays and the tracked time is added. A counter holding more than the
      time since the stored cutoff carries time from before, already
      counted: of it only the last session (Prism adds exactly that on exit)
      can be new. Otherwise it started fresh and all of it can be. Either
      way, only the part the tracker missed while no daemon was running is
      added, so with the daemon up a reset adds just the tracked time.
    - An older cutoff (a restored older instance.cfg, not played yet) waits
      until it is played past the stored one."""
    if cutoff <= seen["cutoff"]:
        return None
    tracked = tracked_seconds(db, key, base["cutoff"], cutoff)
    grown = seconds - seen["seconds"]
    if 0 <= grown <= cutoff - seen["cutoff"] + PRISM_SLACK:
        return max(grown, tracked)
    if seconds > cutoff - seen["cutoff"] + PRISM_SLACK:
        start = max(cutoff - last, base["cutoff"])
        untracked = min(last, cutoff - start) - tracked_seconds(db, key, start, cutoff)
    else:
        start = seen["cutoff"]
        untracked = seconds - tracked
    gain = tracked + max(0, min(untracked, downtime(db, start, cutoff)))
    log(f"Prism total of {name} went from {fmt(seen['seconds'])} to {fmt(seconds)}: "
        f"counted as a reset, adding {fmt(gain)}")
    return gain


def import_prism(db, running=()):
    """Turn Prism's per-instance totals into baselines that never go down.
    The raw reading is kept in a `seen:` row next to each baseline, so the
    next import can tell growth from a reset. Returns the instances read."""
    readings = prism_playtimes()
    stored = {r["key"]: dict(r) for r in db.execute(
        "SELECT key, seconds, cutoff FROM imported WHERE key LIKE 'prism:%' OR key LIKE ?",
        (SEEN + "prism:%",))}
    # Totals already accounted for: a new instance that starts from one of
    # them is a copy.
    known = {r["seconds"] for k, r in stored.items() if k.startswith(SEEN)}
    known |= {r[1] for f, r in readings.items() if f"prism:{f}" in stored}
    rows = []
    for folder in sorted(readings):
        name, seconds, cutoff, last = readings[folder]
        key = f"prism:{folder}"
        if key in running:
            continue
        base = stored.get(key)
        if base is None:
            # A copy is often launched right away, so its first reading may
            # already include one session: Prism adds exactly lastTimePlayed.
            copied = next((t for t in (seconds, seconds - last)
                           if t >= PRISM_COPY_MIN and t in known), None)
            if copied is not None:
                log(f"Prism instance {name} started from another one's total "
                    f"({fmt(copied)}): a copy, counting only its own time")
                baseline = max(seconds - copied, tracked_seconds(db, key, 0, cutoff))
            else:
                baseline = seconds
        else:
            # Rows from before the seen: rows existed hold the raw reading.
            gain = prism_gain(db, key, base, stored.get(SEEN + key) or base,
                              seconds, cutoff, last, name)
            if gain is None:
                continue
            baseline = base["seconds"] + gain
        known.add(seconds)
        rows.append({"key": key, "game_name": "Minecraft", "detail": name,
                     "seconds": baseline, "cutoff": cutoff})
        rows.append({"key": SEEN + key, "game_name": "", "detail": "",
                     "seconds": seconds, "cutoff": cutoff})
    db.executemany(UPSERT_IMPORTED, rows)
    db.commit()
    return len(readings)


def import_totals(db, cfg, steam, running=()):
    """Refresh the baselines. Games running now are skipped: their files may
    be mid-update, and the next import after they exit picks them up."""
    rows = []
    ignore = {int(a) for a in cfg["ignore_steam_appids"]}
    types = {t.lower() for t in cfg["import_steam_types"]}
    playtimes = steam_playtimes()
    infos = steam_app_info(db, steam, [a for a in playtimes if a not in ignore])
    for appid, (name, kind) in infos.items():
        key = f"steam:{appid}"
        # An unknown type (not looked up yet, or the lookup failed) is
        # imported; summarize() drops it again once the type is known.
        if key in running or (kind and kind not in types) or name.startswith(STEAM_TOOL_PREFIXES):
            continue
        seconds, last = playtimes[appid]
        rows.append({"key": key, "game_name": name, "detail": "",
                     "seconds": seconds, "cutoff": last})
    db.executemany(UPSERT_IMPORTED, rows)
    db.commit()
    return len(rows) + import_prism(db, running)


# ── Sync ─────────────────────────────────────────────────────────────────────

def server_enabled(cfg):
    return bool(cfg["ssh_host"])


def ssh_base(cfg):
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=10", cfg["ssh_host"]]


def set_meta(db, key, value):
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()


def get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def sync(db, cfg):
    """Send every local session, upsert what comes back. Returns an error
    string, or None on success."""
    rows = [dict(r) for r in db.execute(f"SELECT {', '.join(FIELDS)} FROM sessions")]
    imported = [dict(r) for r in db.execute(f"SELECT {', '.join(IMPORTED_FIELDS)} FROM imported")]
    remote = cfg["remote_dir"].rstrip("/")
    cmd = ssh_base(cfg) + [f"python3 {remote}/server.py {remote}/playtime.db"]
    try:
        proc = subprocess.run(cmd, input=json.dumps({"sessions": rows, "imported": imported}),
                              capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        err = f"sync failed: {e}"
    else:
        if proc.returncode != 0:
            lines = proc.stderr.strip().splitlines()
            err = f"sync failed: {lines[-1] if lines else f'exit {proc.returncode}'}"
        else:
            try:
                reply = json.loads(proc.stdout)
                back = reply["sessions"]
                back_imported = reply.get("imported", [])
            except (ValueError, KeyError) as e:
                err = f"sync failed: bad reply ({e})"
            else:
                db.executemany(UPSERT, [{k: r[k] for k in FIELDS} for r in back])
                db.executemany(UPSERT_IMPORTED,
                               [{k: r[k] for k in IMPORTED_FIELDS} for r in back_imported])
                db.commit()
                set_meta(db, "sync_ok", str(int(time.time())))
                set_meta(db, "sync_error", "")
                return None
    set_meta(db, "sync_error", err)
    return err


def deploy_server(cfg):
    """Returns an error string, or None on success (or with no server set)."""
    if not server_enabled(cfg):
        print("no server set (ssh_host is empty), nothing to deploy")
        return None
    remote = cfg["remote_dir"].rstrip("/")
    try:
        subprocess.run(ssh_base(cfg) + [f"mkdir -p {remote}"],
                       capture_output=True, text=True, timeout=60, check=True)
        with open(SERVER_SCRIPT, "rb") as f:
            subprocess.run(ssh_base(cfg) + [f"cat > {remote}/server.py"],
                           stdin=f, capture_output=True, timeout=60, check=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        lines = stderr.strip().splitlines()
        return f"deploy failed: {lines[-1] if lines else f'exit {e.returncode}'}"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"deploy failed: {e}"
    print(f"server.py copied to {cfg['ssh_host']}:{remote}/")
    return None


# ── Local backups ────────────────────────────────────────────────────────────

BACKUP_NAME = re.compile(r"playtime-\d{4}-\d{2}-\d{2}\.db")


def backup_files():
    """Our own backups, oldest first (the date in the name sorts)."""
    if not BACKUP_DIR.is_dir():
        return []
    return sorted(p for p in BACKUP_DIR.iterdir() if BACKUP_NAME.fullmatch(p.name))


def last_backup():
    """Epoch of the newest backup, 0 if none. Taken from the files, so
    deleting the folder simply starts over."""
    files = backup_files()
    return int(files[-1].stat().st_mtime) if files else 0


def backup(db, keep):
    """Snapshot the history to backups/playtime-<date>.db (one per day, the
    latest wins) and drop all but the newest `keep` (None keeps all).
    Returns the path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"playtime-{dt.date.today().isoformat()}.db"
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    try:
        db.backup(out)
    finally:
        out.close()
    # Never leave a half-written file under a backup's name.
    tmp.replace(dest)
    if keep:
        files = backup_files()
        for old in files[:max(0, len(files) - keep)]:
            old.unlink()
    return dest


def restore(db, path):
    """Merge sessions and imported totals from another playtime database into
    this one. Same upserts as sync, so nothing newer is lost and restoring
    twice changes nothing. Returns (sessions, imported) row counts read."""
    if not Path(path).is_file():
        raise ValueError(f"{path} is not a file")
    src = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "sessions" not in tables:
            raise ValueError(f"{path} is not a playtime database (no sessions table)")
        rows = [dict(r) for r in src.execute(f"SELECT {', '.join(FIELDS)} FROM sessions")]
        imported = ([dict(r) for r in src.execute(
            f"SELECT {', '.join(IMPORTED_FIELDS)} FROM imported")]
            if "imported" in tables else [])
        uptime = (list(src.execute("SELECT start, end FROM uptime"))
                  if "uptime" in tables else [])
    finally:
        src.close()
    db.executemany(UPSERT, rows)
    db.executemany(UPSERT_IMPORTED, imported)
    db.executemany("INSERT INTO uptime (start, end) VALUES (?, ?) "
                   "ON CONFLICT(start) DO UPDATE SET end = max(end, excluded.end)", uptime)
    db.commit()
    return len(rows), len(imported)


# ── Stats ────────────────────────────────────────────────────────────────────

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def ts(d):
    return int(d.timestamp())


def period_edges(now, first_start):
    """Bucket edges (epochs) and labels for each period, in local time."""
    today = dt.datetime(now.year, now.month, now.day)
    out = {}

    # Naive datetimes are wall-clock time, so .timestamp() handles DST days.
    edges = [today + dt.timedelta(hours=h) for h in range(24)] + [today + dt.timedelta(days=1)]
    out["day"] = ([ts(e) for e in edges], [str(h) for h in range(24)])

    monday = today - dt.timedelta(days=today.weekday())
    edges = [monday + dt.timedelta(days=d) for d in range(8)]
    out["week"] = ([ts(e) for e in edges], WEEKDAYS)

    m0 = dt.datetime(now.year, now.month, 1)
    m1 = dt.datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    days = (m1 - m0).days
    edges = [m0 + dt.timedelta(days=d) for d in range(days)] + [m1]
    out["month"] = ([ts(e) for e in edges], [str(d + 1) for d in range(days)])

    edges = [dt.datetime(now.year, m, 1) for m in range(1, 13)] + [dt.datetime(now.year + 1, 1, 1)]
    out["year"] = ([ts(e) for e in edges], MONTHS)

    y0 = dt.datetime.fromtimestamp(first_start).year if first_start else now.year
    y0 = min(y0, now.year)
    edges = [dt.datetime(y, 1, 1) for y in range(y0, now.year + 2)]
    out["all"] = ([ts(e) for e in edges], [str(y) for y in range(y0, now.year + 1)])
    return out


def group_key(game_key):
    """Steam games group by AppId (names can differ between the manifest and
    the store); every Prism instance groups under Minecraft."""
    return "minecraft" if game_key.startswith(("prism:", "minecraft")) else game_key


def imported_rows(db, import_types):
    """Baselines, minus Steam apps whose type the config does not count. The
    filter runs here, not at import, so the server's copy can stay complete."""
    types = {t.lower() for t in import_types}
    kinds = {f"steam:{r[0]}": r[1] for r in db.execute("SELECT appid, type FROM steam_apps")}
    return [dict(r) for r in db.execute(f"SELECT {', '.join(IMPORTED_FIELDS)} FROM imported")
            if not r["key"].startswith(SEEN)
            and not (kinds.get(r["key"]) and kinds[r["key"]] not in types)]


def load_sessions(db, live=()):
    """Every stored session by id, with `live` ones merged in or extended."""
    sessions = {r["id"]: dict(r) for r in db.execute(
        "SELECT id, game_key, game_name, detail, start, end FROM sessions")}
    for s in live:
        if s.get("id") in sessions:
            sessions[s["id"]]["end"] = max(sessions[s["id"]]["end"], s["end"])
        else:
            sessions[s.get("id") or uuid.uuid4().hex] = dict(s)
    return sessions


def summarize(db, live=(), import_types=DEFAULTS["import_steam_types"]):
    """Totals per period, per game and per bucket. `live` holds sessions not
    yet (fully) in the DB as dicts with game_key, game_name, detail, start, end.

    All time also counts the imported Steam/Prism totals: a game's total is
    its baseline plus tracked time after the baseline's cutoff. The year bars
    show tracked time only; `before` is the rest of the all-time total."""
    now = dt.datetime.now()
    sessions = load_sessions(db, live)
    first = min((s["start"] for s in sessions.values()), default=None)
    baselines = imported_rows(db, import_types)
    cutoffs = {b["key"]: b["cutoff"] for b in baselines}

    periods = {}
    for pname, (edges, labels) in period_edges(now, first).items():
        all_time = pname == "all"
        lo, hi = edges[0], edges[-1]
        buckets = [0] * (len(edges) - 1)
        games = {}

        def add(key, name, detail, seconds, games=games):
            g = games.setdefault(group_key(key),
                                 {"key": group_key(key), "name": name, "seconds": 0, "details": {}})
            if not key.startswith(("prism:", "minecraft")):
                g["name"] = name
            g["seconds"] += seconds
            if detail:
                g["details"][detail] = g["details"].get(detail, 0) + seconds

        for s in sessions.values():
            a, b = max(s["start"], lo), min(s["end"], hi)
            if b <= a:
                continue
            for i in range(len(buckets)):
                x, y = max(a, edges[i]), min(b, edges[i + 1])
                if y > x:
                    buckets[i] += y - x
            if all_time:
                # Time before the cutoff is already in the baseline.
                a = max(a, cutoffs.get(s["game_key"], 0))
                if b <= a:
                    continue
            add(s["game_key"], s["game_name"], s["detail"], b - a)
        if all_time:
            for base in baselines:
                add(base["key"], base["game_name"], base["detail"], base["seconds"])

        game_list = sorted(
            ({"key": g["key"], "name": g["name"], "seconds": g["seconds"],
              "details": sorted(({"name": k, "seconds": v} for k, v in g["details"].items()),
                                key=lambda d: -d["seconds"])}
             for g in games.values()),
            key=lambda g: -g["seconds"])
        total = sum(g["seconds"] for g in game_list)
        periods[pname] = {
            "total": total,
            "games": game_list,
            "buckets": [{"label": l, "seconds": s} for l, s in zip(labels, buckets)],
        }
        if all_time:
            periods[pname]["before"] = max(0, total - sum(buckets))
    return periods


def averages(db, live=()):
    """Tracked seconds overall and per game group, with the span they cover:
    from the first tracked session to now. The panel divides by `days` for
    its per day/week/month/year averages. Imported totals have no dates, so
    they are left out."""
    sessions = load_sessions(db, live)
    first = min((s["start"] for s in sessions.values()), default=None)
    if first is None:
        return {"since": None, "days": 0, "total": 0, "games": []}
    games = {}
    for s in sessions.values():
        seconds = s["end"] - s["start"]
        if seconds <= 0:
            continue
        key = group_key(s["game_key"])
        g = games.setdefault(key, {"key": key, "name": s["game_name"], "seconds": 0})
        if not s["game_key"].startswith(("prism:", "minecraft")):
            g["name"] = s["game_name"]
        g["seconds"] += seconds
    game_list = sorted(games.values(), key=lambda g: -g["seconds"])
    return {"since": first,
            # At least one day, so a first session doesn't blow up the rate.
            "days": max(1.0, (time.time() - first) / 86400),
            "total": sum(g["seconds"] for g in game_list),
            "games": game_list}


def history(db, live=(), import_types=DEFAULTS["import_steam_types"]):
    """Per game group: tracked session spans [start, end] sorted by start, and
    the imported baseline seconds, which have no dates and so no bars."""
    out = {}

    def game(key, name):
        g = out.setdefault(group_key(key), {"name": name, "spans": [], "untracked": 0})
        if not key.startswith(("prism:", "minecraft")):
            g["name"] = name
        return g

    for s in load_sessions(db, live).values():
        if s["end"] > s["start"]:
            game(s["game_key"], s["game_name"])["spans"].append([s["start"], s["end"]])
    for base in imported_rows(db, import_types):
        game(base["key"], base["game_name"])["untracked"] += base["seconds"]
    for g in out.values():
        g["spans"].sort()
    return out


def write_json(path, data):
    """Atomic replace, so a reader never sees half a file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


_updater = None


def get_updater():
    """The updater (updater.py next to this file), or None without one. The
    service runs python -I, which leaves this file's folder off sys.path."""
    global _updater
    if _updater is None:
        if str(APP_DIR) not in sys.path:
            sys.path.insert(0, str(APP_DIR))
        try:
            import updater
        except ImportError:
            return None
        _updater = updater.Updater(REPO, APP_DIR, UPDATE_STATE, CACHE_DIR)
    return _updater


def write_summary(db, live_sessions, cfg):
    now = int(time.time())
    data = {
        "updated": now,
        "live": [{"name": s["game_name"], "detail": s["detail"], "seconds": s["end"] - s["start"]}
                 for s in live_sessions],
        "periods": summarize(db, live_sessions, cfg["import_steam_types"]),
        "averages": averages(db, live_sessions),
        "sync": {
            "enabled": server_enabled(cfg),
            "ok": int(get_meta(db, "sync_ok") or 0),
            # An error from before the server was turned off is not news.
            "error": (get_meta(db, "sync_error") or "") if server_enabled(cfg) else "",
        },
        "backup": {"ok": last_backup()},
    }
    upd = get_updater()
    if upd:
        data["update"] = dict(upd.status(), auto=bool(cfg["auto_update"]))
    write_json(SUMMARY_PATH, data)
    write_json(HISTORY_PATH, {"updated": now,
                              "games": history(db, live_sessions, cfg["import_steam_types"])})


def fmt(seconds):
    m = int(seconds) // 60
    if m < 1:
        return "<1m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def print_stats(db, cfg, limit=25):
    titles = {"day": "Today", "week": "This week", "month": "This month",
              "year": "This year", "all": "All time"}
    for pname, p in summarize(db, (), cfg["import_steam_types"]).items():
        print(f"{titles[pname]}: {fmt(p['total'])}")
        if p.get("before"):
            print(f"  ({fmt(p['before'])} of it from Steam/Prism before tracking)")
        for g in p["games"][:limit]:
            print(f"  {fmt(g['seconds']):>9}  {g['name']}")
            for d in g["details"]:
                print(f"  {'':>9}    {fmt(d['seconds'])}  {d['name']}")
    print()
    print(f"History: {DB_PATH}")
    b = last_backup()
    print(f"Backup:  {ago(b) if b else 'none yet'}"
          + (f", {len(backup_files())} kept in {BACKUP_DIR}" if b else ""))
    if not server_enabled(cfg):
        print("Server:  off (ssh_host is empty), local only")
    else:
        ok, err = int(get_meta(db, "sync_ok") or 0), get_meta(db, "sync_error") or ""
        print(f"Server:  {cfg['ssh_host']}, "
              + (f"last sync {ago(ok)}" if ok else "never synced")
              + (f"; {err}" if err else ""))


def ago(epoch):
    s = max(0, int(time.time()) - epoch)
    if s < 60:
        return "just now"
    return f"{fmt(s)} ago" if s < 86400 else f"{s // 86400} days ago"


# ── Daemon ───────────────────────────────────────────────────────────────────

class Tracker:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.host = socket.gethostname()
        self.active = {}  # game_key -> session dict

    def _save(self, s):
        self.db.execute(UPSERT, {k: s[k] for k in FIELDS})
        self.db.commit()

    def close(self, key):
        """End a session at the last time it was seen. Returns True when the
        session counted (long enough to be kept)."""
        s = self.active.pop(key)
        long_enough = s["end"] - s["start"] >= self.cfg["min_session_seconds"]
        if long_enough:
            self._save(s)
            log(f"session ended: {s['game_name']} {fmt(s['end'] - s['start'])}")
        return long_enough

    def close_all(self):
        any_saved = False
        for key in list(self.active):
            any_saved |= self.close(key)
        return any_saved

    def update(self, found, now):
        """Feed one scan. Returns True when a session was saved."""
        saved = False
        for key in list(self.active):
            if key not in found:
                saved |= self.close(key)
        for key, (name, detail) in found.items():
            s = self.active.get(key)
            if s is None:
                s = {"id": uuid.uuid4().hex, "game_key": key, "game_name": name,
                     "detail": detail, "start": now, "end": now, "host": self.host}
                self.active[key] = s
                log(f"session started: {name}{' (' + detail + ')' if detail else ''}")
            s["end"] = now
            if detail and not s["detail"]:
                s["detail"] = detail
        return saved

    def checkpoint(self):
        for s in self.active.values():
            if s["end"] - s["start"] >= self.cfg["min_session_seconds"]:
                self._save(s)

    def live(self):
        return list(self.active.values())


def run():
    cfg = load_config()
    db = open_db()
    steam = SteamNames()
    steam.reload()
    tracker = Tracker(db, cfg)

    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    poll = cfg["poll_seconds"]
    sync_every = cfg["sync_minutes"] * 60
    last_suspended = suspended_total()
    last_checkpoint = last_summary = 0
    next_sync = 0  # sync right away, which also restores history after a reinstall
    server = server_enabled(cfg)
    keep = cfg["backup_keep"]
    next_backup = 0 if keep > 0 else float("inf")
    started = int(time.time())

    def heartbeat():
        db.execute("INSERT INTO uptime (start, end) VALUES (?, ?) "
                   "ON CONFLICT(start) DO UPDATE SET end = excluded.end",
                   (started, int(time.time())))
        db.commit()

    heartbeat()
    upd = get_updater()
    if upd:
        upd.note_start()
    # The update check runs in a thread (network); the main loop installs, and
    # only between games, since the install restarts this daemon.
    update_ready = []
    next_update_check = 0

    def check_update():
        try:
            latest = upd.check()
        except (OSError, ValueError, KeyError) as e:
            log(f"update check failed: {e!r}")
            return
        if latest and not upd.dev_checkout and not upd.attempted(latest):
            update_ready.append(latest)

    log(f"playtime {upd.version if upd else '?'} running, db {DB_PATH}, "
        + (f"server {cfg['ssh_host']}" if server else "local only (no ssh_host)"))

    # Names of played Steam apps that no local cache knows come from the
    # store, slowly, in the background; the next import picks them up.
    names_done = threading.Event()
    try:
        # Also fills the name cache from appinfo.vdf before the store lookup.
        import_totals(db, cfg, steam, set(scan(cfg, steam)))
    except Exception as e:  # noqa: BLE001 - a bad Steam/Prism file must not stop the daemon
        log(f"import failed: {e!r}")
    missing = missing_app_names(db, list(steam_playtimes()))
    if missing:
        log(f"looking up {len(missing)} Steam app names")

        def lookup():
            fetch_store_names(missing, lambda: stopping)
            names_done.set()

        threading.Thread(target=lookup, daemon=True).start()

    while not stopping:
        now = time.time()
        suspended = suspended_total()
        if suspended - last_suspended > poll:
            # Nothing was played while suspended: end sessions at last seen.
            if tracker.active:
                log(f"suspended {int(suspended - last_suspended)}s, closing sessions at last poll")
            if tracker.close_all():
                next_sync = 0
        last_suspended = suspended

        if tracker.update(scan(cfg, steam), int(now)):
            next_sync = 0
            last_summary = 0

        if now - last_checkpoint >= CHECKPOINT_SECONDS:
            tracker.checkpoint()
            heartbeat()
            last_checkpoint = now

        if names_done.is_set():
            names_done.clear()
            log("Steam app names looked up")
            next_sync = 0

        if now >= next_sync:
            tracker.checkpoint()
            try:
                import_totals(db, cfg, steam, set(tracker.active))
            except Exception as e:  # noqa: BLE001 - a half-written Steam file must not stop tracking
                log(f"import failed: {e!r}")
            if server:
                err = sync(db, cfg)
                if err:
                    log(err)
            next_sync = now + sync_every
            last_summary = 0

        if now >= next_backup:
            # Due a day after the newest backup, so restarts do not add more.
            if now - last_backup() >= BACKUP_SECONDS:
                tracker.checkpoint()
                try:
                    backup(db, keep)
                except (OSError, sqlite3.Error) as e:
                    log(f"backup failed: {e!r}")
            next_backup = now + 3600

        if upd and cfg["auto_update"] and not upd.dev_checkout and now >= next_update_check:
            next_update_check = now + UPDATE_POLL_SECONDS
            threading.Thread(target=check_update, daemon=True).start()
        if update_ready and not tracker.active:
            latest = update_ready.pop()
            log(f"updating to {latest['tag']}")
            try:
                upd.install(latest, detach=True)
            except (OSError, RuntimeError) as e:
                log(f"update failed: {e}")
            last_summary = 0

        if now - last_summary >= SUMMARY_SECONDS:
            write_summary(db, tracker.live(), cfg)
            last_summary = now

        # Sleep in short steps so SIGTERM is handled quickly.
        end = now + poll
        while not stopping and time.time() < end:
            time.sleep(min(1, end - time.time()))

    tracker.close_all()
    heartbeat()
    write_summary(db, [], cfg)
    if server:
        err = sync(db, cfg)
        if err:
            log(err)
    log("playtime stopped")


def update_now(check_only):
    upd = get_updater()
    if upd is None:
        sys.exit("no updater.py next to playtime.py; reinstall with install.sh")
    try:
        latest = upd.check(force=True)
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"could not reach GitHub: {e}")
    if not latest:
        print(f"playtime {upd.version} is the latest")
        return
    print(f"playtime {latest['tag']} is out (installed: {upd.version})")
    if check_only:
        return
    try:
        upd.install(latest, detach=False)
    except (OSError, RuntimeError) as e:
        sys.exit(f"update failed: {e}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "run":
        run()
    elif cmd == "sync":
        db, cfg = open_db(), load_config()
        if not server_enabled(cfg):
            sys.exit(f"no server set: add ssh_host to {CONFIG_PATH}")
        err = sync(db, cfg)
        write_summary(db, [], cfg)
        if err:
            sys.exit(err)
        print("synced")
    elif cmd == "stats":
        print_stats(open_db(), load_config())
    elif cmd == "summary":
        write_summary(open_db(), [], load_config())
    elif cmd == "import":
        db, cfg = open_db(), load_config()
        steam = SteamNames()
        steam.reload()
        running = set(scan(cfg, steam))
        print(f"imported {import_totals(db, cfg, steam, running)} totals"
              + (f", skipped running: {', '.join(sorted(running))}" if running else ""))
        missing = missing_app_names(db, list(steam_playtimes()))
        if missing:
            print(f"{len(missing)} Steam app names unknown locally; the daemon looks them up")
        write_summary(db, [], cfg)
    elif cmd == "backup":
        cfg = load_config()
        print(f"backed up to {backup(open_db(), cfg['backup_keep'] or None)}")
    elif cmd == "restore":
        if len(sys.argv) < 3:
            sys.exit("usage: playtime restore FILE")
        db, cfg = open_db(), load_config()
        try:
            n, m = restore(db, sys.argv[2])
        except (ValueError, sqlite3.Error) as e:
            sys.exit(f"restore failed: {e}")
        write_summary(db, [], cfg)
        print(f"merged {n} sessions and {m} imported totals from {sys.argv[2]}")
    elif cmd == "deploy-server":
        err = deploy_server(load_config())
        if err:
            sys.exit(err)
    elif cmd == "version":
        upd = get_updater()
        version = (upd.version if upd else "") or "unknown"
        print(version + (" (from a git checkout)" if upd and upd.dev_checkout else ""))
    elif cmd == "update":
        update_now(check_only="--check" in sys.argv[2:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

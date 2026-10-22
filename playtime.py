#!/usr/bin/env python3
"""Playtime tracker: records how long games run and keeps the history on a
server over SSH.

  playtime.py run           the daemon (systemd user service)
  playtime.py sync          push/pull sessions with the server now
  playtime.py stats         print today/week/month/year/all-time totals
  playtime.py summary       rewrite the summary file the Noctalia panel reads
  playtime.py deploy-server copy server.py to the server

Games are found by scanning /proc. Steam games run under Steam's `reaper
SteamLaunch AppId=N` process, Prism Launcher's Minecraft runs as java with
org.prismlauncher.EntryPoint, and anything else comes from [[game]] rules in
the config file.
"""

import datetime as dt
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "playtime" / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")) / "playtime"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / "playtime"
DB_PATH = DATA_DIR / "playtime.db"
SUMMARY_PATH = STATE_DIR / "summary.json"
SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"

DEFAULTS = {
    "ssh_host": "oracle",
    "remote_dir": "playtime",
    "poll_seconds": 10,
    "sync_minutes": 15,
    # Sessions shorter than this are never saved (crash on launch, a quick
    # menu check).
    "min_session_seconds": 60,
    "ignore_steam_appids": [],
}

# A poll gap this much longer than poll_seconds means the PC was suspended.
SUSPEND_GAP_FACTOR = 6
CHECKPOINT_SECONDS = 60
SUMMARY_SECONDS = 60

STEAM_ROOTS = [
    HOME / ".local/share/Steam",
    HOME / ".steam/steam",
    HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
]
# Steam launches these through reaper too, but they are not games.
STEAM_TOOL_PREFIXES = ("Proton", "Steam Linux Runtime", "Steamworks Common")

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
"""

UPSERT = """
INSERT INTO sessions (id, game_key, game_name, detail, start, end, host)
VALUES (:id, :game_key, :game_name, :detail, :start, :end, :host)
ON CONFLICT(id) DO UPDATE SET
    start = min(start, excluded.start),
    end = max(end, excluded.end)
"""

FIELDS = ("id", "game_key", "game_name", "detail", "start", "end", "host")


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


def prism_instance(pid):
    """Instance display name from the game's working directory."""
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return ""
    parts = cwd.parts
    if "instances" not in parts:
        return ""
    i = len(parts) - 1 - parts[::-1].index("instances")
    if i + 1 >= len(parts):
        return ""
    inst_dir = Path(*parts[: i + 2])
    try:
        for line in (inst_dir / "instance.cfg").read_text(errors="replace").splitlines():
            if line.startswith("name="):
                return line[5:].strip()
    except OSError:
        pass
    return parts[i + 1]


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
                name = f"Non-Steam game {appid}"
            if name.startswith(STEAM_TOOL_PREFIXES):
                continue
            found[f"steam:{appid}"] = (name, "")
            continue

        if "org.prismlauncher.EntryPoint" in argv:
            found["minecraft"] = ("Minecraft", prism_instance(entry.name))
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


# ── Sync ─────────────────────────────────────────────────────────────────────

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
    remote = cfg["remote_dir"].rstrip("/")
    cmd = ssh_base(cfg) + [f"python3 {remote}/server.py {remote}/playtime.db"]
    try:
        proc = subprocess.run(cmd, input=json.dumps({"sessions": rows}),
                              capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        err = f"sync failed: {e}"
    else:
        if proc.returncode != 0:
            err = f"sync failed: {proc.stderr.strip().splitlines()[-1:] or proc.returncode}"
        else:
            try:
                back = json.loads(proc.stdout)["sessions"]
            except (ValueError, KeyError) as e:
                err = f"sync failed: bad reply ({e})"
            else:
                db.executemany(UPSERT, [{k: r[k] for k in FIELDS} for r in back])
                db.commit()
                set_meta(db, "sync_ok", str(int(time.time())))
                set_meta(db, "sync_error", "")
                return None
    set_meta(db, "sync_error", err)
    return err


def deploy_server(cfg):
    remote = cfg["remote_dir"].rstrip("/")
    subprocess.run(ssh_base(cfg) + [f"mkdir -p {remote}"], check=True)
    with open(SERVER_SCRIPT, "rb") as f:
        subprocess.run(ssh_base(cfg) + [f"cat > {remote}/server.py"], stdin=f, check=True)
    print(f"server.py copied to {cfg['ssh_host']}:{remote}/")


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


def summarize(db, live=()):
    """Totals per period, per game and per bucket. `live` holds sessions not
    yet (fully) in the DB as dicts with game_name, detail, start, end."""
    now = dt.datetime.now()
    sessions = {r["id"]: dict(r) for r in db.execute(
        "SELECT id, game_name, detail, start, end FROM sessions")}
    for s in live:
        if s.get("id") in sessions:
            sessions[s["id"]]["end"] = max(sessions[s["id"]]["end"], s["end"])
        else:
            sessions[s.get("id") or uuid.uuid4().hex] = dict(s)
    first = min((s["start"] for s in sessions.values()), default=None)

    periods = {}
    for pname, (edges, labels) in period_edges(now, first).items():
        lo, hi = edges[0], edges[-1]
        buckets = [0] * (len(edges) - 1)
        games = {}
        for s in sessions.values():
            a, b = max(s["start"], lo), min(s["end"], hi)
            if b <= a:
                continue
            g = games.setdefault(s["game_name"], {"seconds": 0, "details": {}})
            g["seconds"] += b - a
            if s["detail"]:
                g["details"][s["detail"]] = g["details"].get(s["detail"], 0) + b - a
            for i in range(len(buckets)):
                x, y = max(a, edges[i]), min(b, edges[i + 1])
                if y > x:
                    buckets[i] += y - x
        game_list = sorted(
            ({"name": n, "seconds": g["seconds"],
              "details": sorted(({"name": k, "seconds": v} for k, v in g["details"].items()),
                                key=lambda d: -d["seconds"])}
             for n, g in games.items()),
            key=lambda g: -g["seconds"])
        periods[pname] = {
            "total": sum(g["seconds"] for g in game_list),
            "games": game_list,
            "buckets": [{"label": l, "seconds": s} for l, s in zip(labels, buckets)],
        }
    return periods


def write_summary(db, live_sessions):
    now = int(time.time())
    data = {
        "updated": now,
        "live": [{"name": s["game_name"], "detail": s["detail"], "seconds": s["end"] - s["start"]}
                 for s in live_sessions],
        "periods": summarize(db, live_sessions),
        "sync": {
            "ok": int(get_meta(db, "sync_ok") or 0),
            "error": get_meta(db, "sync_error") or "",
        },
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SUMMARY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(SUMMARY_PATH)


def fmt(seconds):
    m = int(seconds) // 60
    if m < 1:
        return "<1m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def print_stats(db):
    titles = {"day": "Today", "week": "This week", "month": "This month",
              "year": "This year", "all": "All time"}
    for pname, p in summarize(db).items():
        print(f"{titles[pname]}: {fmt(p['total'])}")
        for g in p["games"]:
            print(f"  {fmt(g['seconds']):>9}  {g['name']}")
            for d in g["details"]:
                print(f"  {'':>9}    {fmt(d['seconds'])}  {d['name']}")


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
    last_poll = time.time()
    last_checkpoint = last_summary = 0
    next_sync = 0  # sync right away, which also restores history after a reinstall
    log(f"playtime running, db {DB_PATH}, server {cfg['ssh_host']}")

    while not stopping:
        now = time.time()
        if now - last_poll > poll * SUSPEND_GAP_FACTOR:
            # Suspended (or the clock jumped): nothing was played in the gap.
            if tracker.active:
                log(f"gap of {int(now - last_poll)}s, closing sessions at last poll")
            if tracker.close_all():
                next_sync = 0
        last_poll = now

        if tracker.update(scan(cfg, steam), int(now)):
            next_sync = 0
            last_summary = 0

        if now - last_checkpoint >= CHECKPOINT_SECONDS:
            tracker.checkpoint()
            last_checkpoint = now

        if now >= next_sync:
            tracker.checkpoint()
            err = sync(db, cfg)
            if err:
                log(err)
            next_sync = now + sync_every
            last_summary = 0

        if now - last_summary >= SUMMARY_SECONDS:
            write_summary(db, tracker.live())
            last_summary = now

        # Sleep in short steps so SIGTERM is handled quickly.
        end = now + poll
        while not stopping and time.time() < end:
            time.sleep(min(1, end - time.time()))

    tracker.close_all()
    write_summary(db, [])
    err = sync(db, cfg)
    if err:
        log(err)
    log("playtime stopped")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "run":
        run()
    elif cmd == "sync":
        db = open_db()
        err = sync(db, load_config())
        write_summary(db, [])
        if err:
            sys.exit(err)
        print("synced")
    elif cmd == "stats":
        print_stats(open_db())
    elif cmd == "summary":
        write_summary(open_db(), [])
    elif cmd == "deploy-server":
        deploy_server(load_config())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

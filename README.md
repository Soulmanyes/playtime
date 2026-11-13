# playtime

Tracks how long you play games on Linux. The history lives in a local SQLite
file with daily local backups; optionally it is mirrored to your own server
over SSH, so it survives a reinstall or a distro swap. A Noctalia bar
widget shows today's total; its panel shows day, week, month, year and
all-time stats per game, as bar charts with a time y-axis on round ticks. All time also includes everything Steam and Prism
Launcher recorded before tracking started. Clicking a game opens its own bar
chart, starting at the current year; clicking a bar drills down from year to
month, then to the clicked day by hour. The Averages tab shows average playtime
per day, week, month and year, overall and per game, from tracked time.

## How it works

- **Daemon** (`playtime.py run`, systemd user service) scans `/proc` every
  10 s. It has no hooks into games; one scan takes about 2.5 ms (500
  processes), and the process uses about 18 MB RAM.
  - **Steam** games (native and Proton) are found through Steam's
    `reaper SteamLaunch AppId=N` process; names come from the local
    `appmanifest_*.acf` files of every Steam library. Proton, Steam Linux
    Runtime and redistributables are skipped. Non-Steam shortcuts are
    named after their executable (the `.exe` under Proton).
  - **Minecraft** through Prism Launcher is found by the game's java process
    (`org.prismlauncher.EntryPoint`, so the launcher alone does not count);
    the instance name is recorded too.
  - **Other games** come from `[[game]]` rules in the config (executable
    name or command-line regex).
- Sessions shorter than 60 s are dropped. A running session is saved every
  minute, so a crash loses at most a minute. Suspend (detected through
  `CLOCK_BOOTTIME`) ends the session at the last time the game was seen.
- **Import (all time)**: Steam stores each game's account-wide playtime and
  last exit time in `userdata/*/config/localconfig.vdf` (the numbers SteamDB
  shows, read locally, no API key). Prism stores `totalTimePlayed` per
  instance. Each total becomes a baseline with a cutoff at the end of the last
  session it includes; all time adds only tracked time after the cutoff, so
  re-importing never counts anything twice. The year bars show tracked time
  only. Imports run at start and before every sync, skipping running games.
  - Names come from installed manifests, then Steam's local `appinfo.vdf`
    cache, then (for games the cache lacks) the public store API, looked up
    slowly once in the background and cached.
  - Only Steam apps of type `game` count by default, so tools like Wallpaper
    Engine stay out; see `import_steam_types`.
- **Local history**: every session is written to
  `~/.local/share/playtime/playtime.db` first; everything works from that
  file, with or without a server.
- **Local backups**: once a day the daemon copies the history (SQLite's online
  backup) to `~/.local/share/playtime/backups/playtime-<date>.db` and keeps
  the newest `backup_keep` (default 7). `playtime restore <file>` merges a
  backup back in with the same upserts as sync, so it never loses newer
  sessions and running it twice changes nothing.
- **Sync** (optional, when `ssh_host` is set):
  `ssh <host> python3 playtime/server.py ...` sends every local
  session and imported total and gets every stored one back; both sides
  upsert by key, so syncing twice never duplicates anything. It runs at start, after each
  session and every 15 minutes, and retries later when offline. No port or
  web service is opened on the server; it only needs Python 3.9+.
- **Noctalia plugin** (`noctalia/`) only reads
  `~/.local/state/playtime/summary.json`, which the daemon rewrites once a
  minute, so several bars cost nothing extra.
  - The panel's per-game charts read `history.json` next to it: every tracked
    session as a start/end pair per game, bucketed in the panel. Only tracked
    time has dates; imported Steam and Prism totals show as a note under the
    chart. A separate file keeps the bar widget's minutely read small.
  - The Averages tab uses the `averages` block in `summary.json`: tracked
    seconds overall and per game, and the days since the first tracked
    session. Each average is seconds / days x unit length (a month is 30.44
    days). A unit longer than the tracked span is marked "(projected)".
    Imported totals have no dates, so they are left out.
  - In a game view: the back button or Escape goes up a level (from the year
    back to the list), the arrows or Left/Right move to the previous or next
    period. `noctalia msg panel-open soulmanyes/playtime:panel "game steam:240
    month 2026-11-01"` opens a game view directly (level and date optional).

## Install

Requires Python 3.11+. For the optional server backup: an SSH host alias
whose key works without a prompt (the daemon uses `BatchMode`) and Python
3.9+ on the server.

```sh
./install.sh                      # daemon + systemd user service
./install.sh ~/path/to/plugins    # also copy the Noctalia plugin into a local plugin source
```

When a server is set, `install.sh` also copies `server.py` to it, so both
sides always speak the same sync format.

Edit `~/.config/playtime/config.toml` (from `config.example.toml`) to add rules
for non-Steam games and, optionally, set `ssh_host`. With `ssh_host` empty
(the default) everything stays local.

After a reinstall: with a server, restore the SSH key, run `./install.sh`, and
the first sync pulls the full history back. Without one, run `./install.sh`
and `playtime restore <copy of playtime.db or a backup>`.

## Commands

```
playtime stats          totals per period and game, backup and sync status
playtime sync           sync with the server now
playtime import         reread Steam's and Prism's totals now
playtime summary        rewrite the panel's summary file
playtime backup         write a local backup now
playtime restore FILE   merge a backup (or any playtime.db) into the history
playtime deploy-server  copy server.py to the server
```

## Tested

CachyOS (Hyprland, Noctalia v5), Steam native + Proton, Prism Launcher;
server on Oracle Linux 9 (Python 3.9, SQLite 3.34).

## Prism resets and copies

Prism keeps each instance's total in `instances/<folder>/instance.cfg`, and
that counter can reset or be swapped: an OS reinstall without the instances
folder, a switch between the native package and the Flatpak, a modpack
reinstalled under the same folder name, or an older backup of the folder put
back. Prism's copy dialog can also copy an instance **with** its play time.

The import handles these so the all-time total never goes down:

- Each instance's last raw reading is kept next to its baseline (a `seen:`
  row, synced and backed up like the rest). A reading only counts as growth
  when the counter grew by no more than the time that passed since the last
  one; then the larger of Prism's growth and the tracked time is added.
- Anything else counts as a reset. The old baseline stays, the tracked time
  since is added, and on top of that only what Prism counted while no
  playtime daemon was running (the daemon records when it runs, in the
  `uptime` table) and what fits the wall clock. With the daemon running, a
  reset adds nothing it did not track itself.
- A reading with an older cutoff than the stored one (an older
  `instance.cfg` put back) waits until it is played past the stored one.
- A new instance whose total equals another instance's, or does after its
  last session is taken off, is a copy: only its own new time counts.
- With both the native and the Flatpak data folder present, the instance
  with the newer last session wins.
- Baselines never go down through an import, a sync or `playtime restore`;
  the server applies the same rule.

What is still guessed: a counter that holds more than the time since the
last reading carries old time, so only its last session can count as new.
A smaller counter, read after a time with no daemon running (for example an
OS reinstall), counts as started fresh: if it was really a small older
backup, up to its own size is counted twice. The `uptime` history of a machine stays in its local
database and backups; a fresh install that restores only from the server
has none, so all time before it counts as downtime.

## License

CC0 1.0. No third-party code; Python standard library only.

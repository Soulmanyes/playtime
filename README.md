# playtime

Tracks how long you play games on Linux. The history lives in a local SQLite
file with daily local backups; optionally it is mirrored to your own server
over SSH, so it survives a reinstall or a distro swap. A Noctalia bar
widget shows today's total; its panel shows day, week, month, year and
all-time stats per game. All time also includes everything Steam and Prism
Launcher recorded before tracking started.

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

## Known issues

### Prism playtime resets

Prism keeps each instance's total in `instances/<folder>/instance.cfg`
(`totalTimePlayed`). The import treats a newer cutoff as a newer total, so
when Prism's own total for an instance goes **down**, the all-time baseline
goes down with it. Not fixed yet.

When Prism's total can reset:

- The OS is reinstalled, or `~/.local/share/PrismLauncher` is deleted,
  without restoring the instances folder.
- Switching between the native package and the Flatpak. They use different
  data folders, so instances added in the new one start at 0.
- Uninstalling the Flatpak with `--delete-data`. Removing or reinstalling
  the native package keeps the data.
- A modpack is deleted and installed again under the **same** folder name.
- `RecordGameTime` or a per-instance `OverrideGameTime` is changed. Turned
  off, Prism stops counting; the total does not drop, but it misses time.

What happens then: the next import sees a newer cutoff and replaces the old
total (for example 761 h of GTNH) with the new small one. Tracked sessions
between the old and the new cutoff are skipped too, because the code assumes
the new total already contains them. The server and the local backups do not
help: sync and `playtime restore` use the same upsert, so the older, bigger
row loses to the newer one there too.

Cases that are fine:

- A new instance under a **new** folder name (for example a GTNH update
  installed as a new instance). It gets its own key, and the old row stays.
- Prism crashing before it saves the total. Prism's total is lower, but the
  tracked session still counts after the old cutoff.

Double counting: Prism's copy dialog has a **Keep play time** option. A copy
made with it counts the same hours again under a second key.

Possible fix: when an import has a newer cutoff but fewer seconds than the
stored row, treat it as a reset. Keep the old baseline, and add the new
total on top of the tracked time up to the reset.

## License

CC0 1.0. No third-party code; Python standard library only.

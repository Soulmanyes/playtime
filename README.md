# playtime

Tracks how long you play games on Linux and keeps the history on your own
server over SSH, so it survives a reinstall or a distro swap. A Noctalia bar
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
- **Sync**: `ssh <host> python3 playtime/server.py ...` sends every local
  session and imported total and gets every stored one back; both sides
  upsert by key, so syncing twice never duplicates anything. It runs at start, after each
  session and every 15 minutes, and retries later when offline. No port or
  web service is opened on the server; it only needs Python 3.9+.
- **Noctalia plugin** (`noctalia/`) only reads
  `~/.local/state/playtime/summary.json`, which the daemon rewrites once a
  minute, so several bars cost nothing extra.

## Install

Requires Python 3.11+ locally, an SSH host alias whose key works without a
prompt (the daemon uses `BatchMode`), and Python 3.9+ on the server.

```sh
./install.sh                      # daemon + systemd user service
./install.sh ~/path/to/plugins    # also copy the Noctalia plugin into a local plugin source
```

`install.sh` also copies `server.py` to the server, so both sides always
speak the same sync format.

Edit `~/.config/playtime/config.toml` (from `config.example.toml`) to set the
SSH host and add rules for non-Steam games.

After a reinstall: restore the SSH key, run `./install.sh`, and the first sync
pulls the full history back.

## Commands

```
playtime stats          totals per period and game
playtime sync           sync now
playtime import         reread Steam's and Prism's totals now
playtime summary        rewrite the panel's summary file
playtime deploy-server  copy server.py to the server
```

## Tested

CachyOS (Hyprland, Noctalia v5), Steam native + Proton, Prism Launcher;
server on Oracle Linux 9 (Python 3.9, SQLite 3.34).

## License

CC0 1.0. No third-party code; Python standard library only.

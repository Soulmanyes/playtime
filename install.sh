#!/bin/sh
# Installs Playtime for the current user, or updates it when run again:
#   - the daemon in ~/.local/lib/playtime, run by the playtime.service user unit
#   - the `playtime` command in ~/.local/bin
#   - if the Noctalia shell is installed, its bar widget and panel, as a plugin
#     source named "playtime"
# Without cloning the repo:
#   curl -fsSL https://raw.githubusercontent.com/Soulmanyes/playtime/main/install.sh | sh
# ./install.sh --uninstall removes it again; the history, config and backups stay.
# The whole script is one { } block, so a download cut off midway runs nothing.
{
set -eu

REPO=Soulmanyes/playtime
PLUGIN_ID=soulmanyes/playtime
SOURCE=playtime
here=$(cd "$(dirname "$0")" && pwd)
lib="$HOME/.local/lib/playtime"
bin="$HOME/.local/bin"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/playtime/config.toml"
# PLAYTIME_NO_SYSTEMD=1 skips systemctl and Noctalia (for tests in a scratch HOME).
no_systemd=${PLAYTIME_NO_SYSTEMD:-}

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

if [ "${1:-}" = "--uninstall" ]; then
    say "Removing Playtime"
    if [ -z "$no_systemd" ]; then
        systemctl --user disable --now playtime.service 2>/dev/null || true
        if command -v noctalia >/dev/null; then
            noctalia msg plugins disable "$PLUGIN_ID" >/dev/null 2>&1 || true
            noctalia msg plugins source remove "$SOURCE" >/dev/null 2>&1 || true
        fi
    fi
    rm -rf "$lib" "$bin/playtime" "$units/playtime.service"
    [ -n "$no_systemd" ] || systemctl --user daemon-reload
    say "Done. Your history, backups and config stay in:"
    echo "  ${XDG_DATA_HOME:-$HOME/.local/share}/playtime"
    echo "  ${XDG_CONFIG_HOME:-$HOME/.config}/playtime"
    exit 0
fi

if [ ! -f "$here/playtime.py" ]; then
    # Run through curl | sh: download the latest release (main if there is none
    # yet) and run its own install.sh. releases/latest redirects to .../tag/<tag>
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    say "Downloading Playtime"
    tag=$(curl -fsSLo /dev/null -w '%{url_effective}' \
        "https://github.com/$REPO/releases/latest" | sed -n 's|.*/tag/||p')
    ref=${tag:+tags/$tag}
    curl -fsSL "https://github.com/$REPO/archive/refs/${ref:-heads/main}.tar.gz" |
        tar -xz -C "$tmp" --strip-components=1
    sh "$tmp/install.sh" "$@"
    exit
fi

if ! python3 -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    echo "Playtime needs Python 3.11 or newer (python3)." >&2
    exit 1
fi
if [ -z "$no_systemd" ] && ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "Playtime runs as a systemd user service, and no systemd user session was found." >&2
    exit 1
fi

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/noctalia/plugin.toml")
say "Installing Playtime $version to $lib"
mkdir -p "$lib" "$bin" "$units"
cp "$here/playtime.py" "$here/server.py" "$here/updater.py" "$lib/"
echo "$version" > "$lib/VERSION"
# A git checkout updates with git pull; the updater leaves it alone.
if [ -e "$here/.git" ]; then
    touch "$lib/dev-checkout"
else
    rm -f "$lib/dev-checkout"
fi
cat > "$bin/playtime" <<WRAP
#!/bin/sh
exec python3 -I "$lib/playtime.py" "\$@"
WRAP
chmod +x "$bin/playtime"

if [ ! -e "$conf" ]; then
    mkdir -p "$(dirname "$conf")"
    cp "$here/config.example.toml" "$conf"
    echo "wrote $conf"
fi

# The Noctalia plugin: its own path source, a catalog.toml next to the plugin
# folder. The catalog entry is the top of plugin.toml, so versions can't drift.
rm -rf "$lib/noctalia"
mkdir -p "$lib/noctalia/playtime"
cp -r "$here/noctalia/." "$lib/noctalia/playtime/"
{
    echo "# Written by install.sh from playtime/plugin.toml."
    echo
    echo "[[plugin]]"
    sed -n '/^\[/q; /^[a-z_]* = /p' "$here/noctalia/plugin.toml" | grep -v '^dependencies = '
    echo "deprecated = false"
} > "$lib/noctalia/catalog.toml"

if [ -z "$no_systemd" ]; then
    cp "$here/playtime.service" "$units/"
    systemctl --user daemon-reload
    systemctl --user enable playtime.service >/dev/null 2>&1
    systemctl --user restart playtime.service

    if command -v noctalia >/dev/null; then
        if noctalia msg plugins source add "$SOURCE" path "$lib/noctalia" >/dev/null 2>&1 ||
           noctalia msg plugins source list 2>/dev/null | grep -q "^$SOURCE "; then
            noctalia msg plugins enable "$PLUGIN_ID" >/dev/null 2>&1 || true
            say "Noctalia plugin added. Put the \"Playtime\" widget on your bar in Noctalia's bar settings."
        else
            say "Noctalia isn't running, so the plugin wasn't added. Start Noctalia, then run:"
            echo "  noctalia msg plugins source add $SOURCE path \"$lib/noctalia\""
            echo "  noctalia msg plugins enable $PLUGIN_ID"
        fi
    fi
fi

# The server copy must match the client's sync format, so update it on every
# install when a server is set; offline is fine, `playtime deploy-server` later.
if grep -q '^ssh_host = "..*"' "$conf" 2>/dev/null; then
    "$bin/playtime" deploy-server || echo "could not reach the server; run 'playtime deploy-server' later"
fi
say "Done. Playtime $version is running; see 'playtime stats'."
}

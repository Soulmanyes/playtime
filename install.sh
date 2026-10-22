#!/bin/sh
# Installs the playtime daemon as a systemd user service.
#   ./install.sh                     daemon + service
#   ./install.sh <noctalia-dir>      also copy the Noctalia plugin into a local
#                                    plugin source folder (as <dir>/playtime)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
lib="$HOME/.local/lib/playtime"

mkdir -p "$lib" "$HOME/.local/bin" "$HOME/.config/systemd/user"
cp "$here/playtime.py" "$here/server.py" "$lib/"
cat > "$HOME/.local/bin/playtime" <<WRAP
#!/bin/sh
exec python3 -I "$lib/playtime.py" "\$@"
WRAP
chmod +x "$HOME/.local/bin/playtime"

conf="${XDG_CONFIG_HOME:-$HOME/.config}/playtime/config.toml"
if [ ! -e "$conf" ]; then
    mkdir -p "$(dirname "$conf")"
    cp "$here/config.example.toml" "$conf"
    echo "wrote $conf"
fi

cp "$here/playtime.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable playtime.service
systemctl --user restart playtime.service

if [ $# -ge 1 ]; then
    mkdir -p "$1/playtime"
    cp -r "$here/noctalia/." "$1/playtime/"
    echo "plugin copied to $1/playtime"
fi
# The server copy must match the client's sync format, so update it on every
# install; offline is fine, the next install or `playtime deploy-server` does it.
"$HOME/.local/bin/playtime" deploy-server || echo "could not reach the server; run 'playtime deploy-server' later"
echo "installed"

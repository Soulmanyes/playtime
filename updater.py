"""Update check against Playtime's GitHub releases. Standard library only.

Trimmed from Shellac's updater. check() asks GitHub for the latest release at
most once a day and remembers the answer; install() downloads that release's
source archive and runs its install.sh, which copies the new version over the
installed one and restarts the daemon. Config, history and backups stay where
they are.

The daemon is a systemd service and install.sh restarts it, which kills every
process in the service's cgroup. So the daemon starts the installer outside it,
as a transient `systemd-run --user` unit; from a terminal it runs in place.

A copy installed from a git checkout (install.sh leaves a `dev-checkout` file
next to the code) never installs anything: updating it is `git pull`.
"""

import json
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

CHECK_EVERY = 24 * 3600  # seconds between automatic checks
TIMEOUT = 10


def parse_version(text):
    """"v1.4.0" or "1.4" -> (1, 4, 0); None if it isn't a plain version number."""
    parts = (text or "").strip().lstrip("vV").split(".")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    return tuple(nums + [0] * (3 - len(nums)))


class Updater:
    def __init__(self, repo, app_dir, state_file, cache_dir):
        self.repo = repo                    # "Owner/name" on GitHub
        self.app_dir = Path(app_dir)        # the installed code, with VERSION
        self.cache_dir = Path(cache_dir)    # where downloads unpack
        # {"checked": time, "latest": {...}, "attempted": tag, "running": version,
        #  "updated_from": version, "updated_at": time}
        self.state_file = Path(state_file)
        self.state = self._load()

    @property
    def version(self):
        try:
            return (self.app_dir / "VERSION").read_text().strip()
        except OSError:
            return ""

    @property
    def dev_checkout(self):
        """Installed from a git clone: an update would overwrite local work."""
        return (self.app_dir / "dev-checkout").exists()

    def _load(self):
        try:
            state = json.loads(self.state_file.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2) + "\n")
        tmp.replace(self.state_file)

    def note_start(self):
        """Call once per daemon start: remembers when the version changed, so
        the panel can say what it was updated to."""
        mine, before = self.version, self.state.get("running")
        new, old = parse_version(mine), parse_version(before)
        if new and old and new > old:
            self.state.update(updated_from=before, updated_at=time.time())
        if mine != before:
            self.state["running"] = mine
            self._save()

    def newer(self, latest):
        mine, theirs = parse_version(self.version), parse_version(latest["tag"])
        return bool(mine and theirs and theirs > mine)

    def check(self, force=False):
        """The latest release as {"tag", "url"} if it is newer than this version,
        else None. Asks GitHub only when forced or a day after the last check.
        Raises OSError/ValueError when GitHub can't be reached."""
        latest = self.state.get("latest")
        if force or not latest or time.time() - self.state.get("checked", 0) > CHECK_EVERY:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "playtime"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.load(r)
            latest = {"tag": data["tag_name"], "url": data.get("html_url", "")}
            self.state.update(latest=latest, checked=time.time())
            self._save()
        return latest if self.newer(latest) else None

    def attempted(self, latest):
        """An install of this release was started before and the version is
        still older, so it failed: auto mode doesn't retry it every day."""
        return self.state.get("attempted") == latest["tag"]

    def status(self):
        """For summary.json: what the panel footer shows."""
        latest = self.state.get("latest") or {}
        available = bool(latest) and self.newer(latest)
        updated_at = self.state.get("updated_at") or 0
        return {
            "version": self.version,
            "dev": self.dev_checkout,
            "available": latest.get("tag", "") if available else "",
            "failed": available and self.attempted(latest),
            # Shown for a day after an update.
            "updated_from": self.state.get("updated_from", "")
            if time.time() - updated_at < 24 * 3600 else "",
        }

    def install(self, latest, detach):
        """Downloads the release and runs its install.sh. detach: start it as
        its own systemd unit and return (from the daemon); else wait for it.
        Raises RuntimeError with a message for the user when that fails."""
        if self.dev_checkout:
            raise RuntimeError(f"Installed from a git checkout; update it with git pull "
                               f"and ./install.sh (marker: {self.app_dir / 'dev-checkout'})")
        self.state["attempted"] = latest["tag"]
        self._save()
        src = self._download(latest["tag"])
        if detach:
            cmd = ["systemd-run", "--user", "--collect", "--quiet",
                   f"--unit=playtime-update-{int(time.time())}",
                   "sh", "-c", 'sh "$1/install.sh"; rc=$?; rm -rf "$1"; exit $rc', "sh", str(src)]
        else:
            cmd = ["sh", str(src / "install.sh")]
        run = subprocess.run(cmd, cwd=src, stdin=subprocess.DEVNULL,
                             capture_output=detach, text=True, check=False)
        if not detach:
            shutil.rmtree(src, ignore_errors=True)
        if run.returncode != 0:
            last = ((run.stderr or "").strip() or (run.stdout or "").strip()).splitlines()[-1:]
            raise RuntimeError("The installer stopped" + (f": {last[0]}" if last else "")
                               + ". Run install.sh in a terminal")

    def _download(self, tag):
        """The release's source tree, unpacked under cache_dir."""
        url = f"https://github.com/{self.repo}/archive/refs/tags/{tag}.tar.gz"
        dest = self.cache_dir / f"update-{tag}"
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        archive = dest / "release.tar.gz"
        req = urllib.request.Request(url, headers={"User-Agent": "playtime"})
        with urllib.request.urlopen(req, timeout=60) as r:
            archive.write_bytes(r.read())
        unpacked = dest / "src"
        unpacked.mkdir()
        _extract(archive, unpacked)
        tops = list(unpacked.iterdir())
        if len(tops) != 1 or not (tops[0] / "install.sh").is_file():
            raise RuntimeError("The release download has no install.sh")
        # Move the tree up so the caller can delete `dest` as one folder.
        tree = dest.with_name(dest.name + "-tree")
        shutil.rmtree(tree, ignore_errors=True)
        tops[0].rename(tree)
        shutil.rmtree(dest, ignore_errors=True)
        return tree


def _extract(archive, dest):
    try:
        with tarfile.open(archive) as tar:
            try:
                tar.extractall(dest, filter="data")
            except TypeError:
                # Python without extraction filters (e.g. Debian 12's 3.11.2): only
                # plain files and folders, with no absolute or ../ paths
                for m in tar.getmembers():
                    if not (m.isfile() or m.isdir()) or m.name.startswith("/") \
                            or ".." in m.name.split("/"):
                        raise RuntimeError(f"Unexpected file in the release archive: {m.name}")
                tar.extractall(dest)
    except tarfile.TarError as e:  # a cut-off download, or a file the filter refuses
        raise RuntimeError(f"Couldn't unpack the release: {str(e).splitlines()[0]}") from e

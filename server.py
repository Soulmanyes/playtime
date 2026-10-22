#!/usr/bin/env python3
"""Server half of playtime: run over SSH by `playtime.py sync`.

Reads {"sessions": [...]} on stdin, upserts them into the SQLite file given as
the first argument and prints every stored session back as JSON. Upserting by
session id with max(end) means syncing the same data twice changes nothing,
and a session that grew since the last sync only gets longer.

Kept compatible with Python 3.9 and SQLite 3.24+ (Oracle Linux 9).
"""

import json
import sqlite3
import sys

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
"""

UPSERT = """
INSERT INTO sessions (id, game_key, game_name, detail, start, end, host)
VALUES (:id, :game_key, :game_name, :detail, :start, :end, :host)
ON CONFLICT(id) DO UPDATE SET
    start = min(start, excluded.start),
    end = max(end, excluded.end)
"""

FIELDS = ("id", "game_key", "game_name", "detail", "start", "end", "host")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: server.py <database>")
    incoming = json.load(sys.stdin).get("sessions", [])
    db = sqlite3.connect(sys.argv[1])
    db.executescript(SCHEMA)
    with db:
        db.executemany(UPSERT, [{k: s[k] for k in FIELDS} for s in incoming])
    rows = db.execute("SELECT " + ", ".join(FIELDS) + " FROM sessions").fetchall()
    json.dump({"sessions": [dict(zip(FIELDS, r)) for r in rows]}, sys.stdout)


if __name__ == "__main__":
    main()

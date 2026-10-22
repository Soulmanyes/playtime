#!/usr/bin/env python3
"""Server half of playtime: run over SSH by `playtime.py sync`.

Reads {"sessions": [...], "imported": [...]} on stdin, upserts them into the
SQLite file given as the first argument and prints everything stored back as
JSON. Upserting by session id with max(end) means syncing the same data twice
changes nothing, and a session that grew since the last sync only gets longer.
Imported Steam/Prism totals are replaced only by one with a newer cutoff.

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
CREATE TABLE IF NOT EXISTS imported (
    key TEXT PRIMARY KEY,
    game_name TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    seconds INTEGER NOT NULL,
    cutoff INTEGER NOT NULL
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

UPSERT_IMPORTED = """
INSERT INTO imported (key, game_name, detail, seconds, cutoff)
VALUES (:key, :game_name, :detail, :seconds, :cutoff)
ON CONFLICT(key) DO UPDATE SET
    game_name = excluded.game_name,
    detail = excluded.detail,
    seconds = CASE WHEN excluded.cutoff > imported.cutoff
                   THEN excluded.seconds ELSE imported.seconds END,
    cutoff = max(imported.cutoff, excluded.cutoff)
"""

IMPORTED_FIELDS = ("key", "game_name", "detail", "seconds", "cutoff")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: server.py <database>")
    request = json.load(sys.stdin)
    incoming = request.get("sessions", [])
    imported = request.get("imported", [])
    db = sqlite3.connect(sys.argv[1])
    db.executescript(SCHEMA)
    with db:
        db.executemany(UPSERT, [{k: s[k] for k in FIELDS} for s in incoming])
        db.executemany(UPSERT_IMPORTED, [{k: s[k] for k in IMPORTED_FIELDS} for s in imported])
    rows = db.execute("SELECT " + ", ".join(FIELDS) + " FROM sessions").fetchall()
    base = db.execute("SELECT " + ", ".join(IMPORTED_FIELDS) + " FROM imported").fetchall()
    json.dump({"sessions": [dict(zip(FIELDS, r)) for r in rows],
               "imported": [dict(zip(IMPORTED_FIELDS, r)) for r in base]}, sys.stdout)


if __name__ == "__main__":
    main()

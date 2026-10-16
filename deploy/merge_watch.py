#!/usr/bin/env python3
"""Merge a snapshot of the server's watch.db into the local one, keeping what the local copy
already has. Rows the local copy already has are kept even if the server no longer has them.

Usage: python3 merge_watch.py SNAPSHOT LOCAL
"""
import sqlite3
import sys

snapshot, local = sys.argv[1], sys.argv[2]
db = sqlite3.connect(local)
db.execute("ATTACH DATABASE ? AS src", (snapshot,))
for name, sql in db.execute("SELECT name, sql FROM src.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
    db.execute(sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    have = [r[1] for r in db.execute(f"PRAGMA main.table_info({name})")]
    cols = db.execute(f"PRAGMA src.table_info({name})").fetchall()
    for c in cols:                      # a column added on the server since the last merge
        if c[1] not in have:
            db.execute(f"ALTER TABLE main.{name} ADD COLUMN {c[1]} {c[2]}")
    names = ", ".join(c[1] for c in cols)
    # the server's row is the newer one: a decision gains its result and its followed prices over time
    n = db.execute(f"INSERT OR REPLACE INTO main.{name} ({names}) SELECT {names} FROM src.{name}").rowcount
    total = db.execute(f"SELECT COUNT(*) FROM main.{name}").fetchone()[0]
    print(f"  {name}: {n} rows from the server, {total} kept locally")
db.commit()

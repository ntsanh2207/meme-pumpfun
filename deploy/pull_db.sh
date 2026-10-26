#!/bin/sh
# Copy a consistent snapshot of the VPS databases to data/ (pump.db, holders.db, watch.db) for local analysis.
set -eu
HOST=${HOST:-root@45.76.106.71}
cd "$(dirname "$0")/.."
mkdir -p data
ssh "$HOST" 'python3 -c "
import sqlite3
src = sqlite3.connect(\"file:/opt/pump-graduated/data/pump.db?mode=ro\", uri=True)
dst = sqlite3.connect(\"/tmp/pump_snapshot.db\")
src.backup(dst)
dst.close()
import os
if os.path.exists(\"/opt/pump-graduated/data/holders.db\"):
    src = sqlite3.connect(\"file:/opt/pump-graduated/data/holders.db?mode=ro\", uri=True)
    dst = sqlite3.connect(\"/tmp/holders_snapshot.db\")
    src.backup(dst)
    dst.close()
if os.path.exists(\"/opt/pump-graduated/data/watch.db\"):
    src = sqlite3.connect(\"file:/opt/pump-graduated/data/watch.db?mode=ro\", uri=True)
    dst = sqlite3.connect(\"/tmp/watch_snapshot.db\")
    src.backup(dst)
    dst.close()
" && gzip -1 -f /tmp/pump_snapshot.db'
scp -q "$HOST:/tmp/pump_snapshot.db.gz" data/pump.db.gz
scp -q "$HOST:/tmp/holders_snapshot.db" data/holders.db 2>/dev/null || true
# the live paper trades (watch.py): merged into data/watch.db, which keeps everything ever pulled
scp -q "$HOST:/tmp/watch_snapshot.db" data/watch_snapshot.db 2>/dev/null \
  && python3 deploy/merge_watch.py data/watch_snapshot.db data/watch.db && rm -f data/watch_snapshot.db || true
ssh "$HOST" 'rm -f /tmp/pump_snapshot.db.gz /tmp/holders_snapshot.db /tmp/watch_snapshot.db'
gunzip -f data/pump.db.gz
ls -lh data/pump.db

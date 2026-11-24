import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal(id TEXT PRIMARY KEY, payload TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS buoys(id TEXT PRIMARY KEY, x INTEGER NOT NULL, y INTEGER NOT NULL, service TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS fixes(buoy TEXT REFERENCES buoys NOT NULL, source TEXT NOT NULL, minute INTEGER NOT NULL CHECK(minute>=0), x INTEGER NOT NULL, y INTEGER NOT NULL, error INTEGER NOT NULL CHECK(error BETWEEN 1 AND 8), PRIMARY KEY(buoy,source,minute)) STRICT;
CREATE TABLE IF NOT EXISTS docks(id TEXT PRIMARY KEY, quay TEXT NOT NULL, start INTEGER NOT NULL CHECK(start>=0), stop INTEGER NOT NULL CHECK(stop>start)) STRICT;
CREATE TABLE IF NOT EXISTS trips(id TEXT PRIMARY KEY, dock TEXT REFERENCES docks NOT NULL, boat TEXT NOT NULL, out INTEGER NOT NULL, back INTEGER NOT NULL CHECK(back>out), x INTEGER NOT NULL, y INTEGER NOT NULL, reach INTEGER NOT NULL CHECK(reach>0), service TEXT NOT NULL, seats INTEGER NOT NULL CHECK(seats>0)) STRICT;
CREATE TABLE IF NOT EXISTS recoveries(buoy TEXT PRIMARY KEY REFERENCES buoys, trip TEXT REFERENCES trips NOT NULL, lifted INTEGER, landed INTEGER) STRICT;
CREATE TABLE IF NOT EXISTS equipment(id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('battery','sensor')), buoy TEXT REFERENCES buoys, state TEXT NOT NULL CHECK(state IN ('spare','mounted','deck','locker'))) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS one_mount ON equipment(buoy,kind) WHERE state='mounted';
CREATE TABLE IF NOT EXISTS swaps(buoy TEXT REFERENCES buoys, kind TEXT, old TEXT UNIQUE REFERENCES equipment NOT NULL, new TEXT UNIQUE REFERENCES equipment NOT NULL, minute INTEGER NOT NULL, PRIMARY KEY(buoy,kind)) STRICT;
CREATE TABLE IF NOT EXISTS transfers(part TEXT PRIMARY KEY REFERENCES swaps(old), minute INTEGER NOT NULL, sender TEXT NOT NULL, receiver TEXT NOT NULL CHECK(sender<>receiver)) STRICT;
CREATE TABLE IF NOT EXISTS pairs(buoy TEXT REFERENCES buoys, sensor TEXT REFERENCES equipment NOT NULL, ref INTEGER CHECK(ref>=0), local INTEGER NOT NULL CHECK(local>=0), PRIMARY KEY(buoy,sensor,ref)) STRICT;
CREATE TABLE IF NOT EXISTS qualification_limits(buoy TEXT REFERENCES buoys, version INTEGER CHECK(version>0), radius INTEGER NOT NULL CHECK(radius>0), max_offset INTEGER NOT NULL CHECK(max_offset>=0), PRIMARY KEY(buoy,version)) STRICT;
CREATE TABLE IF NOT EXISTS deployments(buoy TEXT PRIMARY KEY REFERENCES buoys, minute INTEGER NOT NULL) STRICT;
"""


def connect(path, readonly=False):
    db = sqlite3.connect(path, isolation_level=None, uri=readonly)
    db.execute('PRAGMA foreign_keys=ON')
    if not readonly:
        db.executescript(SCHEMA)
    return db


def one(db, sql, *args):
    return db.execute(sql, args).fetchone()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def has_limits(db):
    return bool(one(db, "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='qualification_limits'"))

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
CREATE TABLE IF NOT EXISTS packet_batches(batch TEXT PRIMARY KEY, length INTEGER NOT NULL CHECK(length BETWEEN 1 AND 64), state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','conflict'))) STRICT;
CREATE TABLE IF NOT EXISTS packet_catalog(batch TEXT NOT NULL REFERENCES packet_batches(batch), orig TEXT NOT NULL, buoy TEXT NOT NULL REFERENCES buoys, minute INTEGER NOT NULL CHECK(minute>=0), source TEXT NOT NULL, PRIMARY KEY(batch,orig)) STRICT;
CREATE TABLE IF NOT EXISTS packet_originals(batch TEXT NOT NULL, orig TEXT NOT NULL, payload BLOB NOT NULL, payload_hex TEXT NOT NULL, via TEXT NOT NULL DEFAULT '', PRIMARY KEY(batch,orig,payload_hex)) STRICT;
CREATE TABLE IF NOT EXISTS packet_parities(evidence TEXT NOT NULL, batch TEXT NOT NULL, members TEXT NOT NULL, xor_hex TEXT NOT NULL, xor_bytes BLOB NOT NULL, via TEXT NOT NULL DEFAULT '', PRIMARY KEY(evidence,members,xor_hex)) STRICT;
CREATE TABLE IF NOT EXISTS packet_derived(batch TEXT NOT NULL, orig TEXT NOT NULL, payload_hex TEXT NOT NULL, witnesses TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('derived','withdrawn')), PRIMARY KEY(batch,orig)) STRICT;
CREATE TABLE IF NOT EXISTS packet_conflicts(id INTEGER PRIMARY KEY, batch TEXT NOT NULL, code TEXT NOT NULL, refs TEXT NOT NULL, detail TEXT NOT NULL, signature TEXT NOT NULL UNIQUE) STRICT;
CREATE TABLE IF NOT EXISTS fix_support(buoy TEXT NOT NULL, source TEXT NOT NULL, minute INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('raw','derived')), batch TEXT NOT NULL, orig TEXT NOT NULL, x INTEGER, y INTEGER, error INTEGER, payload_hex TEXT, witnesses TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL CHECK(state IN ('active','withdrawn','rejected')), PRIMARY KEY(buoy,source,minute,batch,orig,kind)) STRICT;
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

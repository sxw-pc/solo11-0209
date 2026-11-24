from position import distance, envelope
from store import has_limits, one, require


def limits(db, buoy):
    if not has_limits(db):
        return 8, 5
    return one(db, '''SELECT radius,max_offset FROM qualification_limits WHERE buoy=?
                     ORDER BY version DESC LIMIT 1''', buoy) or (8, 5)


def status(db, buoy):
    deployment = one(db, 'SELECT minute FROM deployments WHERE buoy=?', buoy)
    if not deployment:
        return 'awaiting-deployment'
    at, = deployment
    x, y = one(db, 'SELECT x,y FROM buoys WHERE id=?', buoy)
    radius, max_offset = limits(db, buoy)
    observed, = one(db, 'SELECT max(minute) FROM fixes WHERE buoy=? AND minute BETWEEN ? AND ?', buoy, at, at+10)
    state, region = envelope(db, buoy, observed if observed is not None else at, since=at)
    if state != 'trusted' or distance(x,y,*region[:2])+region[2] > radius:
        return 'hold-position'
    if one(db, '''SELECT 1 FROM swaps s LEFT JOIN transfers t ON s.old=t.part
                  WHERE s.buoy=? AND (t.minute IS NULL OR t.minute>?)''', buoy, at):
        return 'hold-custody'
    lifted, = one(db, 'SELECT lifted FROM recoveries WHERE buoy=?', buoy)
    old, new = one(db, "SELECT old,new FROM swaps WHERE buoy=? AND kind='sensor'", buoy)
    last, = one(db, 'SELECT max(minute) FROM swaps WHERE buoy=?', buoy)
    before = one(db, '''SELECT local-ref,ref FROM pairs WHERE buoy=? AND sensor=? AND ref>=? AND ref<?
                        ORDER BY ref DESC LIMIT 1''', buoy,old,lifted-15,lifted)
    after = one(db, '''SELECT local-ref,ref FROM pairs WHERE buoy=? AND sensor=? AND ref>? AND ref<=?
                       ORDER BY ref DESC LIMIT 1''', buoy,new,last,at)
    if not before or not after or after[1]-before[1] > 45:
        return 'hold-clock'
    if max(abs(before[0]),abs(after[0])) > max_offset or abs(after[0]-before[0]) > 2:
        return 'hold-clock'
    return 'continuous'


def apply(db, op, args):
    if op == 'pair':
        buoy, sensor, ref, _ = args
        require(one(db, "SELECT 1 FROM equipment WHERE id=? AND kind='sensor' AND buoy=?", sensor, buoy), 'clock pair requires attached sensor')
        mounted = one(db, 'SELECT minute FROM swaps WHERE new=?', sensor)
        removed = one(db, 'SELECT minute FROM swaps WHERE old=?', sensor)
        require((not mounted or mounted[0] < ref) and (not removed or ref < removed[0]), 'clock pair outside sensor tenure')
        db.execute('INSERT INTO pairs VALUES(?,?,?,?)', args)
    elif op == 'limits':
        buoy, *_ = args
        require(not one(db, 'SELECT 1 FROM deployments WHERE buoy=?', buoy), 'qualification limits locked by deployment')
        db.execute('INSERT INTO qualification_limits VALUES(?,?,?,?)', args)
    else:
        buoy, at = args
        require(one(db, 'SELECT count(*) FROM swaps WHERE buoy=? AND minute<?', buoy, at) == (2,), 'redeploy requires both swaps')
        db.execute('INSERT INTO deployments VALUES(?,?)', args)

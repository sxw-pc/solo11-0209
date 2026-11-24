from itertools import combinations


def distance(x, y, a, b):
    return abs(x-a) + abs(y-b)


def envelope(db, buoy, at, since=0):
    fixes = {}
    for source, minute, x, y, error in db.execute('''SELECT source,minute,x,y,error FROM fixes
            WHERE buoy=? AND minute BETWEEN ? AND ? ORDER BY minute DESC,source''', (buoy, max(since, at-20), at)):
        fixes.setdefault(source, (minute, x, y, error))
    if len(fixes) < 2:
        return 'unconfirmed', None
    latest, x, y, _ = next(iter(fixes.values()))
    oldest = min(t for t, *_ in fixes.values())
    if latest-oldest > 10 or any(distance(a,b,c,d) > e+f
                                 for (_,a,b,e), (_,c,d,f) in combinations(fixes.values(), 2)):
        return 'conflicting', None
    radius = max(distance(x,y,a,b)+error for _,a,b,error in fixes.values()) + (at-oldest)//5
    return 'trusted', (x,y,radius)


def apply(db, op, args):
    query = 'INSERT INTO buoys VALUES(?,?,?,?)' if op == 'buoy' else 'INSERT INTO fixes VALUES(?,?,?,?,?,?)'
    db.execute(query, args)

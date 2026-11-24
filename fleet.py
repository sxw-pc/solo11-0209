from position import distance, envelope
from store import one, require


def eligible(db, buoy, service, x, y, reach, at):
    state, region = envelope(db, buoy, at)
    return state == 'trusted' and distance(x,y,*region[:2])+region[2] <= reach and one(
        db, 'SELECT 1 FROM buoys WHERE id=? AND service=?', buoy, service) is not None


def options(db, buoy):
    if one(db, 'SELECT 1 FROM recoveries WHERE buoy=?', buoy):
        return []
    return [trip for trip, at, x, y, reach, service in db.execute('''SELECT id,out,x,y,reach,service FROM trips t
        WHERE seats>(SELECT count(*) FROM recoveries WHERE trip=t.id) ORDER BY out,id''')
            if eligible(db, buoy, service, x, y, reach, at)]


def apply(db, op, args):
    if op == 'dock':
        db.execute('INSERT INTO docks VALUES(?,?,?,?)', args)
    elif op == 'trip':
        _, dock, boat, start, stop, *_ = args
        window = one(db, 'SELECT quay FROM docks WHERE id=? AND start<=? AND stop>=?', dock, start, stop)
        require(window, 'dock closed for trip')
        require(not one(db, '''SELECT 1 FROM trips t JOIN docks d ON d.id=t.dock
            WHERE (boat=? OR quay=?) AND out<? AND back>?''', boat, window[0], stop, start), 'boat or quay overlap')
        db.execute('INSERT INTO trips VALUES(?,?,?,?,?,?,?,?,?,?)', args)
    elif op == 'reserve':
        buoy, trip = args
        require(trip in options(db, buoy), 'trip not feasible')
        db.execute('INSERT INTO recoveries(buoy,trip) VALUES(?,?)', args)
    elif op == 'cancel':
        require(db.execute('DELETE FROM recoveries WHERE buoy=? AND lifted IS NULL', args).rowcount, 'no cancellable reservation')
    else:
        buoy, at = args
        row = one(db, '''SELECT out,back,x,y,reach,service,lifted,landed FROM recoveries r
                         JOIN trips t ON t.id=r.trip WHERE r.buoy=?''', buoy)
        require(row and row[0] <= at <= row[1] and row[7] is None, 'outside open trip')
        _, _, x, y, reach, service, lifted, _ = row
        if op == 'lift':
            require(lifted is None and eligible(db,buoy,service,x,y,reach,at), 'lift position unqualified')
            db.execute('UPDATE recoveries SET lifted=? WHERE buoy=?', (at, buoy))
        else:
            require(lifted is not None and lifted <= at, 'land requires prior lift')
            db.execute('UPDATE recoveries SET landed=? WHERE buoy=?', (at, buoy))

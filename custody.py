from store import one, require


def apply(db, op, args):
    if op == 'equipment':
        part, kind, buoy = args
        require(buoy is None or not one(db, 'SELECT 1 FROM recoveries WHERE buoy=? AND lifted IS NOT NULL', buoy), 'initial mounting closed')
        db.execute('INSERT INTO equipment VALUES(?,?,?,?)', (part,kind,buoy,'spare' if buoy is None else 'mounted'))
    elif op == 'swap':
        buoy, old, new, at = args
        require(one(db, 'SELECT 1 FROM recoveries WHERE buoy=? AND landed<=?', buoy, at), 'swap before landing')
        require(not one(db, 'SELECT 1 FROM deployments WHERE buoy=?', buoy), 'already redeployed')
        current = one(db, "SELECT kind FROM equipment WHERE id=? AND buoy=? AND state='mounted'", old, buoy)
        spare = one(db, "SELECT kind FROM equipment WHERE id=? AND state='spare'", new)
        require(current and current == spare, 'incompatible or unavailable spare')
        db.execute('INSERT INTO swaps VALUES(?,?,?,?,?)', (buoy,current[0],old,new,at))
        db.execute("UPDATE equipment SET state='deck' WHERE id=?", (old,))
        db.execute("UPDATE equipment SET state='mounted',buoy=? WHERE id=?", (buoy,new))
    else:
        part, at, sender, receiver = args
        require(one(db, "SELECT 1 FROM swaps JOIN equipment ON old=id WHERE old=? AND minute<=? AND state='deck'", part, at), 'no removed part to transfer')
        require(sender.strip() and receiver.strip(), 'custodians required')
        db.execute('INSERT INTO transfers VALUES(?,?,?,?)', args)
        db.execute("UPDATE equipment SET state='locker' WHERE id=?", (part,))

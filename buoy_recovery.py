import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import continuity
import custody
import fleet
import position
from store import connect, has_limits, one, require

GROUPS = {position: 'buoy fix packet-batch packet-catalog packet-original packet-retransmit packet-parity',
          fleet: 'dock trip reserve cancel lift land',
          custody: 'equipment swap transfer', continuity: 'pair limits redeploy'}


def load(db, events):
    db.execute('BEGIN IMMEDIATE')
    try:
        require(isinstance(events, list), 'event batch must be an array')
        for row in events:
            require(isinstance(row,list) and len(row)>=2, 'invalid event row')
            event, op, *args = row
            require(isinstance(event,str) and event.strip(), 'event id required')
            payload = json.dumps(row,ensure_ascii=False,separators=(',', ':'))
            prev = one(db, 'SELECT payload FROM journal WHERE id=?', event)
            if prev:
                require(prev == (payload,), 'event id conflict')
                continue
            module = next((m for m, names in GROUPS.items() if op in names.split()), None)
            require(module, f'unknown operation {op}')
            module.apply(db, op, args)
            db.execute('INSERT INTO journal VALUES(?,?)', (event,payload))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def snapshot(db):
    tables = ('journal buoys fixes docks trips recoveries equipment swaps transfers pairs deployments '
              'packet_batches packet_catalog packet_originals packet_parities packet_derived '
              'packet_conflicts fix_support').split()
    state = {}
    for table in tables:
        present = one(db, "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", table)
        state[table] = db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() if present else []
    state['qualification_limits'] = db.execute('SELECT * FROM qualification_limits ORDER BY rowid').fetchall() if has_limits(db) else []
    return state


def report(db, at):
    for buoy, in db.execute('SELECT id FROM buoys ORDER BY id'):
        print(buoy, json.dumps({'options':fleet.options(db,buoy),'continuity':continuity.status(db,buoy),
            'recovery':one(db,'SELECT trip,lifted,landed FROM recoveries WHERE buoy=?',buoy)}))
        if at is not None:
            print(buoy, 'position', json.dumps(position.envelope(db,buoy,at)))
    batches = one(db, "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='packet_batches'")
    if batches:
        for batch, in db.execute('SELECT batch FROM packet_batches ORDER BY batch'):
            print('packet-batch', batch, json.dumps(position.batch_status(db, batch), ensure_ascii=False, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description='Offline annual buoy recovery ledger')
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--at', type=int, help='optional position confidence minute')
    parser.add_argument('action', choices=('demo','load','plan','audit'))
    parser.add_argument('file', nargs='?', type=Path)
    args = parser.parse_args()
    try:
        require((args.action == 'load') == bool(args.file), 'a fixture path is required only for load')
        readonly = args.action in ('plan','audit')
        db_path = args.db.resolve().as_uri()+'?mode=ro' if readonly else args.db
        with closing(connect(db_path,readonly)) as db:
            if not readonly:
                fixture = args.file or Path(__file__).with_name('fixtures')/'demo.json'
                load(db, json.loads(fixture.read_text(encoding='utf-8')))
            db.execute('BEGIN')
            if args.action == 'audit':
                events = [json.loads(row[0]) for row in db.execute('SELECT payload FROM journal ORDER BY rowid')]
                with closing(connect(':memory:')) as replay:
                    load(replay,events)
                    require(snapshot(replay)==snapshot(db), 'audit mismatch')
                    print(f'audit OK: {len(events)} events')
                    report(replay,args.at)
            else:
                report(db,args.at)
    except (ValueError, TypeError, IndexError, AttributeError, OSError, sqlite3.Error) as exc:
        parser.exit(1,f'{exc}\n')


if __name__ == '__main__':
    main()

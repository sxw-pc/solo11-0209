"""Portable copies of original journal evidence for offline handoff."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import continuity
import fleet
from buoy_recovery import load
from store import connect, one, require


def outcomes(db, buoy):
    require(one(db, 'SELECT 1 FROM buoys WHERE id=?', buoy), 'unknown buoy at checkpoint')
    recovery = one(db, 'SELECT trip,lifted,landed FROM recoveries WHERE buoy=?', buoy)
    return {'options': fleet.options(db, buoy), 'continuity': continuity.status(db, buoy),
            'recovery': list(recovery) if recovery else None}


def replay_checkpoints(events, buoy, checkpoints):
    require(isinstance(checkpoints, list) and len(set(checkpoints)) == len(checkpoints) > 0,
            'unique checkpoints required')
    wanted = set(checkpoints)
    found = {}
    start = 0
    with closing(connect(':memory:')) as replay:
        for index, event in enumerate(events):
            event_id = event[0]
            if event_id in wanted:
                load(replay, events[start:index + 1])
                found[event_id] = outcomes(replay, buoy)
                start = index + 1
        require(len(found) == len(wanted) and start == len(events),
                'missing checkpoint or trailing records')
    return found


def capture(db, buoy, checkpoints):
    rows = db.execute('SELECT id,payload FROM journal ORDER BY rowid').fetchall()
    positions = {event_id: index for index, (event_id, _) in enumerate(rows)}
    require(checkpoints and all(event_id in positions for event_id in checkpoints),
            'checkpoint is not in the journal')
    end = max(positions[event_id] for event_id in checkpoints)
    events = [json.loads(payload) for _, payload in rows[:end + 1]]
    return {'buoy': buoy, 'events': events,
            'checkpoints': replay_checkpoints(events, buoy, checkpoints)}


def verify(bundle):
    checkpoints = bundle['checkpoints']
    require(replay_checkpoints(bundle['events'], bundle['buoy'], list(checkpoints)) == checkpoints,
            'checkpoint outcome mismatch')
    return checkpoints


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('capture')
    export.add_argument('--db', type=Path, required=True)
    export.add_argument('buoy')
    export.add_argument('checkpoints', nargs='+')
    review = commands.add_parser('verify')
    review.add_argument('bundle', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'capture':
            with closing(connect(args.db.resolve().as_uri() + '?mode=ro', readonly=True)) as db:
                result = capture(db, args.buoy, args.checkpoints)
        else:
            result = verify(json.loads(args.bundle.read_text(encoding='utf-8-sig')))
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

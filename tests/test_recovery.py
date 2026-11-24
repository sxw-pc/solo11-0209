import json
import sqlite3
from contextlib import closing
from tempfile import TemporaryDirectory

import fleet
import position
from buoy_recovery import load, snapshot
from store import connect, one
from recovery_support import EVENTS, FIXTURES, INDEX, LedgerTest


class PositionTest(LedgerTest):
    def test_single_reporter_and_stale_evidence(self):
        self.through('a1')
        self.event('same-source', 'fix', 'alpha', 'beacon', 21, 11, 0, 1)
        self.assertEqual(position.envelope(self.db, 'alpha', 21), ('unconfirmed', None))
        self.row('a2')
        self.event('old-shore', 'fix', 'alpha', 'shore', 19, 40, 0, 1)
        self.assertEqual(position.envelope(self.db, 'alpha', 25), ('trusted', (11, 0, 1)))
        self.assertEqual(position.envelope(self.db, 'alpha', 42), ('unconfirmed', None))
        self.assertEqual(one(self.db, 'SELECT count(*) FROM fixes'), (4,))

    def test_same_source_minute_conflict_keeps_first_evidence(self):
        self.through('a2')
        with self.assertRaises(sqlite3.IntegrityError):
            self.event('duplicate', 'fix', 'alpha', 'shore', 22, 99, 0, 1)
        self.assertEqual(one(self.db, "SELECT x FROM fixes WHERE buoy='alpha' AND source='shore' AND minute=22"), (11,))
        self.assertIsNone(one(self.db, "SELECT 1 FROM journal WHERE id='duplicate'"))

    def test_envelope_includes_measurement_error_and_elapsed_margin(self):
        self.through('a2')
        self.assertEqual(position.envelope(self.db, 'alpha', 24), ('trusted', (11, 0, 2)))
        self.assertEqual(position.envelope(self.db, 'alpha', 25), ('trusted', (11, 0, 3)))
        self.event('inconsistent', 'fix', 'alpha', 'ranger', 25, 19, 0, 1)
        self.assertEqual(position.envelope(self.db, 'alpha', 25), ('conflicting', None))
        self.assertEqual(position.envelope(self.db, 'alpha', 24), ('trusted', (11, 0, 2)))


class FleetTest(LedgerTest):
    def test_trip_requires_dock_open_for_full_interval(self):
        self.through('q3')
        with self.assertRaisesRegex(ValueError, 'dock closed'):
            self.event('outside', 'trip', 'too-late', 'east-am', 'skiff', 25, 56, 10, 0, 4, 'precision', 1)
        self.event('on-edge', 'trip', 'edge', 'east-am', 'skiff', 20, 55, 10, 0, 4, 'precision', 1)
        self.assertEqual(one(self.db, 'SELECT id,out,back FROM trips'), ('edge', 20, 55))

    def test_physical_quay_conflict_rolls_back_window(self):
        self.through('t3')
        batch = json.loads((FIXTURES / 'rejected.json').read_text(encoding='utf-8'))
        before = snapshot(self.db)
        with self.assertRaisesRegex(ValueError, 'quay overlap'):
            load(self.db, batch)
        self.assertEqual(snapshot(self.db), before)

    def test_boat_conflict_spans_different_quays(self):
        self.through('t1')
        with self.assertRaisesRegex(ValueError, 'boat or quay overlap'):
            self.event('clash', 'trip', 'same-boat', 'west-am', 'skiff-a', 52, 55, 10, 0, 9, 'precision', 1)
        self.event('adjacent', 'trip', 'same-boat', 'west-am', 'skiff-a', 53, 55, 10, 0, 9, 'precision', 1)
        self.assertEqual(one(self.db, "SELECT out FROM trips WHERE id='same-boat'"), (53,))

    def test_assignment_checks_full_envelope_and_maintenance_skill(self):
        self.through('t3')
        self.assertEqual(fleet.options(self.db, 'alpha'), ['narrow', 'wide'])
        self.assertEqual(fleet.options(self.db, 'beta'), ['wide'])
        for trip in ('narrow', 'survey'):
            with self.subTest(trip=trip), self.assertRaisesRegex(ValueError, 'trip not feasible'):
                self.event('bad', 'reserve', 'beta', trip)
        self.assertEqual(one(self.db, 'SELECT count(*) FROM recoveries'), (0,))

    def test_cancellation_releases_only_selected_reservation(self):
        self.through('book')
        self.event('keep-narrow', 'buoy', 'gamma', 0, 0, 'precision')
        self.event('fix-g1', 'fix', 'gamma', 'beacon', 20, 10, 0, 1)
        self.event('fix-g2', 'fix', 'gamma', 'shore', 22, 11, 0, 1)
        self.event('reserve-g', 'reserve', 'gamma', 'narrow')
        self.assertEqual(fleet.options(self.db, 'beta'), [])
        self.event('cancel-a', 'cancel', 'alpha')
        self.assertEqual(fleet.options(self.db, 'beta'), ['wide'])
        self.assertEqual(self.db.execute('SELECT buoy,trip FROM recoveries').fetchall(), [('gamma', 'narrow')])
        self.assertEqual(one(self.db, 'SELECT count(*) FROM trips'), (3,))

    def test_late_drift_invalidates_lift_and_allows_reassignment(self):
        self.through('t3')
        self.event('r', 'reserve', 'alpha', 'narrow')
        load(self.db, json.loads((FIXTURES / 'drift.json').read_text(encoding='utf-8')))
        with self.assertRaisesRegex(ValueError, 'position unqualified'):
            self.row('lift')
        self.assertEqual(one(self.db, 'SELECT trip,lifted FROM recoveries'), ('narrow', None))
        self.event('c', 'cancel', 'alpha')
        self.row('book')
        self.row('lift')
        self.assertEqual(one(self.db, 'SELECT trip,lifted FROM recoveries'), ('wide', 27))

    def test_lifted_reservation_cannot_release_seat(self):
        self.through('lift')
        with self.assertRaisesRegex(ValueError, 'no cancellable'):
            self.event('c', 'cancel', 'alpha')
        self.assertEqual(fleet.options(self.db, 'beta'), [])
        self.assertEqual(one(self.db, 'SELECT lifted FROM recoveries'), (27,))

    def test_landing_requires_order_and_trip_deadline(self):
        self.through('p0')
        with self.assertRaisesRegex(ValueError, 'prior lift'):
            self.row('land')
        self.row('lift')
        with self.assertRaisesRegex(ValueError, 'outside open trip'):
            self.event('after-deadline', 'land', 'alpha', 54)
        self.row('land')
        self.assertEqual(one(self.db, 'SELECT lifted,landed FROM recoveries'), (27, 53))


class CustodyTest(LedgerTest):
    def test_swap_requires_landing(self):
        self.through('lift')
        with self.assertRaisesRegex(ValueError, 'swap before landing'):
            self.row('swap-b')
        self.row('land')
        self.row('swap-b')
        self.assertEqual(one(self.db, "SELECT buoy,state FROM equipment WHERE id='bat-new'"), ('alpha', 'mounted'))
        self.assertEqual(one(self.db, "SELECT state FROM equipment WHERE id='bat-old'"), ('deck',))

    def test_swap_batch_durable_rollback_and_same_id_retry(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'recovery.sqlite'
            with closing(connect(path)) as db:
                load(db, EVENTS[:INDEX['land'] + 1])
                before = snapshot(db)
                invalid = ['swap-s', 'swap', 'alpha', 'sen-old', 'bat-new', 56]
                with self.assertRaisesRegex(ValueError, 'incompatible'):
                    load(db, [EVENTS[INDEX['swap-b']], invalid])
            with closing(connect(path)) as reopened:
                self.assertEqual(snapshot(reopened), before)
                swaps = EVENTS[INDEX['swap-b']:INDEX['swap-s'] + 1]
                load(reopened, swaps)
                committed = snapshot(reopened)
                load(reopened, swaps)
                self.assertEqual(snapshot(reopened), committed)
            with closing(connect(path)) as reopened:
                self.assertEqual(snapshot(reopened), committed)
                self.assertEqual(one(reopened, 'SELECT count(*) FROM swaps'), (2,))

    def test_transfer_requires_two_parties_and_event_after_removal(self):
        self.through('swap-b')
        with self.assertRaises(sqlite3.IntegrityError):
            self.event('same', 'transfer', 'bat-old', 56, 'deck', 'deck')
        with self.assertRaisesRegex(ValueError, 'no removed part'):
            self.event('early', 'transfer', 'bat-old', 54, 'deck', 'locker')
        self.row('locker-b')
        with self.assertRaisesRegex(ValueError, 'no removed part'):
            self.event('again', 'transfer', 'bat-old', 57, 'other', 'locker')
        self.assertEqual(one(self.db, 'SELECT sender,receiver,minute FROM transfers'), ('deck-crew', 'locker-keeper', 56))
        self.assertEqual(one(self.db, "SELECT state FROM equipment WHERE id='bat-old'"), ('locker',))

    def test_custody_as_of_deployment_uses_occurrence_time(self):
        self.through('a6', omit=('locker-b',))
        self.assertEqual(self.qualification(), 'hold-custody')
        self.row('locker-b')
        self.assertEqual(self.qualification(), 'continuous')

    def test_transfer_after_deployment_cannot_retroactively_clear_hold(self):
        self.through('a6', omit=('locker-b',))
        self.event('after', 'transfer', 'bat-old', 68, 'deck', 'locker')
        self.assertEqual(one(self.db, "SELECT state FROM equipment WHERE id='bat-old'"), ('locker',))
        self.assertEqual(self.qualification(), 'hold-custody')


class JournalTest(LedgerTest):
    def test_journal_conflict_rolls_back_earlier_valid_event_and_retry(self):
        self.through('a2')
        before = snapshot(self.db)
        fix = ['fix-extra', 'fix', 'alpha', 'ranger', 23, 11, 0, 1]
        with self.assertRaisesRegex(ValueError, 'event id conflict'):
            load(self.db, [fix, ['a2', 'fix', 'alpha', 'shore', 22, 99, 0, 1]])
        self.assertEqual(snapshot(self.db), before)
        load(self.db, [fix])
        self.assertEqual(one(self.db, "SELECT count(*) FROM journal WHERE id='fix-extra'"), (1,))

    def test_legacy_ledger_replays_with_default_limits_and_no_future_evidence(self):
        self.through('water')
        self.db.execute('DROP TABLE qualification_limits')
        self.assertEqual(continuity.limits(self.db, 'alpha'), (8, 5))
        self.assertEqual(self.qualification(), 'hold-position')
        with closing(connect(':memory:')) as rebuilt:
            load(rebuilt, [json.loads(payload) for payload, in self.db.execute('SELECT payload FROM journal ORDER BY rowid')])
            self.assertEqual(snapshot(rebuilt), snapshot(self.db))

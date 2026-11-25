import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import continuity
import fleet
import position
from buoy_recovery import load, snapshot
from store import connect, one
from recovery_support import EVENTS, FIXTURES, INDEX, LedgerTest

ACCEPT = json.loads((FIXTURES / 'recovery-accept.json').read_text(encoding='utf-8'))
REQ_A1 = json.loads((FIXTURES / 'recovery-request-a1.json').read_text(encoding='utf-8'))
REQ_B1 = json.loads((FIXTURES / 'recovery-request-b1.json').read_text(encoding='utf-8'))
LOCK = json.loads((FIXTURES / 'recovery-lock.json').read_text(encoding='utf-8'))
LOCK_CONFLICT = json.loads((FIXTURES / 'recovery-lock-conflict.json').read_text(encoding='utf-8'))


def acceptance_base(db):
    """demo 到 t3，省略 a1、b1 两条原 fix，再铺同批目录与已收到包。"""
    load(db, [row for row in EVENTS[:INDEX['t3'] + 1] if row[0] not in ('a1', 'b1')])
    load(db, ACCEPT)
    return db


def lock_base(db):
    load(db, [row for row in EVENTS[:INDEX['t3'] + 1] if row[0] not in ('a1', 'b1')])
    load(db, LOCK)
    return db


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


def gf2_rank(rows, width):
    rank = 0
    rows = list(rows)
    for col in range(width):
        pivot = next((i for i in range(rank, len(rows)) if (rows[i] >> col) & 1), None)
        if pivot is None:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        for i in range(len(rows)):
            if i != rank and ((rows[i] >> col) & 1):
                rows[i] ^= rows[rank]
        rank += 1
    return rank


def peel_singletons(members):
    """朴素“每条校验式只剩一个未知数”剥壳法，用作回归结构的对照。"""
    unknown = set().union(*members)
    solved = set()
    changed = True
    while changed:
        changed = False
        for group in members:
            left = [o for o in group if o in unknown and o not in solved]
            if len(left) == 1:
                solved.add(left[0])
                changed = True
    return solved


class PacketAcceptanceTest(LedgerTest):
    def test_fixture_retransmit_payloads_equal_original_a1_b1(self):
        a1 = EVENTS[INDEX['a1']]
        b1 = EVENTS[INDEX['b1']]
        self.assertEqual(REQ_A1[0][-1], position.encode_payload(a1[5], a1[6], a1[7]).hex())
        self.assertEqual(REQ_B1[0][-1], position.encode_payload(b1[5], b1[6], b1[7]).hex())

    def test_no_bookable_trip_before_retransmit(self):
        acceptance_base(self.db)
        self.assertEqual(fleet.options(self.db, 'alpha'), [])
        self.assertEqual(fleet.options(self.db, 'beta'), [])
        self.assertEqual(position.envelope(self.db, 'alpha', 25)[0], 'unconfirmed')
        self.assertEqual(position.envelope(self.db, 'beta', 26)[0], 'unconfirmed')

    def test_parity_packets_are_not_extra_observations(self):
        acceptance_base(self.db)
        # 只有 a2、b2、r1、r2 四条真实负载进入 fixes；三条拼校包不产生观测。
        self.assertEqual(one(self.db, 'SELECT count(*) FROM fixes'), (4,))
        before = self.db.execute('SELECT * FROM fixes ORDER BY rowid').fetchall()
        self.event('extra-parity', 'packet-parity', 'ev-extra', 'shore-accept', ['r1', 'r2'], '0002000000')
        self.assertEqual(self.db.execute('SELECT * FROM fixes ORDER BY rowid').fetchall(), before)
        self.assertEqual(one(self.db, 'SELECT count(*) FROM fixes'), (4,))

    def test_same_catalog_accepts_either_single_retransmit(self):
        cases = [
            (REQ_A1, 'alpha', 'narrow', 'beta'),
            (REQ_B1, 'beta', 'wide', 'alpha'),
        ]
        for fixture, buoy, released_trip, other in cases:
            with self.subTest(request=fixture[0][2]):
                with closing(connect(':memory:')) as db:
                    acceptance_base(db)
                    self.assertEqual(fleet.options(db, buoy), [])
                    load(db, fixture)  # 接收中心一次只补这一个原件
                    self.assertIn(released_trip, fleet.options(db, buoy))
                    self.assertEqual(fleet.options(db, other), [])

    def test_retransmit_a1_releases_narrow_and_keeps_beta_pending(self):
        acceptance_base(self.db)
        load(self.db, REQ_A1)
        self.assertEqual(fleet.options(self.db, 'alpha'), ['narrow', 'wide'])
        self.assertEqual(fleet.options(self.db, 'beta'), [])
        self.assertEqual(one(self.db, '''SELECT minute,x,y,error FROM fixes
                                         WHERE buoy='alpha' AND source='beacon' '''), (20, 10, 0, 1))
        self.event('book-a-narrow', 'reserve', 'alpha', 'narrow')
        self.assertEqual(one(self.db, 'SELECT trip,lifted FROM recoveries WHERE buoy=?', 'alpha'),
                         ('narrow', None))
        with self.assertRaisesRegex(ValueError, 'trip not feasible'):
            self.event('book-b-still', 'reserve', 'beta', 'wide')

    def test_retransmit_b1_releases_beta_wide_and_keeps_alpha_pending(self):
        acceptance_base(self.db)
        load(self.db, REQ_B1)
        self.assertEqual(fleet.options(self.db, 'beta'), ['wide'])
        self.assertEqual(fleet.options(self.db, 'alpha'), [])
        self.assertEqual(one(self.db, '''SELECT minute,x,y,error FROM fixes
                                         WHERE buoy='beta' AND source='beacon' '''), (22, 15, 0, 1))
        self.event('book-b-wide', 'reserve', 'beta', 'wide')
        self.assertEqual(one(self.db, 'SELECT trip FROM recoveries WHERE buoy=?', 'beta'), ('wide',))

    def test_same_original_same_content_counts_once_different_content_conflicts(self):
        acceptance_base(self.db)
        load(self.db, REQ_A1)
        self.event('req-a1-copy', 'packet-retransmit', 'shore-accept', 'a1', '000a000001')
        self.assertEqual(len(position.received_versions(self.db, 'shore-accept', 'a1')), 1)
        self.assertEqual(position.conflict_rows(self.db, batch='shore-accept'), [])
        self.event('req-a1-wrong', 'packet-retransmit', 'shore-accept', 'a1', '000b000001')
        versions = position.received_versions(self.db, 'shore-accept', 'a1')
        self.assertEqual(len(versions), 2)
        conflicts = position.conflict_rows(self.db, batch='shore-accept')
        self.assertEqual([c['code'] for c in conflicts], ['original-reprint'])
        self.assertIn('a1', conflicts[0]['refs'])
        # 第一份真实原件仍然可查、可用；冲突只禁止该批推导。
        self.assertEqual(position.received_payload(self.db, 'shore-accept', 'a1').hex(), '000a000001')
        self.assertIn('narrow', fleet.options(self.db, 'alpha'))

    def test_parity_same_evidence_idempotent_different_content_conflicts(self):
        acceptance_base(self.db)
        same = ['pa-again', 'packet-parity', 'ev-pa', 'shore-accept', ['a1', 'm1', 'm2'], '0008000600']
        self.event(*same)
        self.assertEqual(one(self.db, "SELECT count(*) FROM packet_parities WHERE evidence='ev-pa'"), (1,))
        self.event('pa-diff', 'packet-parity', 'ev-pa', 'shore-accept', ['a1', 'm1', 'm2'], '0008000601')
        conflicts = position.conflict_rows(self.db, batch='shore-accept')
        self.assertEqual([c['code'] for c in conflicts], ['parity-reprint'])
        self.assertIn('ev-pa', conflicts[0]['refs'])

    def test_parity_packet_cannot_cross_batches(self):
        acceptance_base(self.db)
        self.event('other-batch', 'packet-batch', 'other', 5)
        self.event('other-catalog', 'packet-catalog', 'other', [['z1', 'alpha', 40, 'beacon']])
        with self.assertRaisesRegex(ValueError, 'crosses batch'):
            self.event('cross', 'packet-parity', 'ev-cross', 'other', ['z1', 'a1'], '0000000000')
        self.assertIsNone(one(self.db, "SELECT 1 FROM packet_parities WHERE evidence='ev-cross'"))

    def test_raw_and_derived_payloads_are_separately_queryable(self):
        lock_base(self.db)
        # lA 没有任何原始负载，但被联立唯一恢复。
        self.assertIsNone(position.received_payload(self.db, 'lock-demo', 'lA'))
        self.assertEqual(position.derived_payload(self.db, 'lock-demo', 'lA'),
                         position.encode_payload(10, 0, 1))
        with closing(connect(':memory:')) as db:
            acceptance_base(db)
            self.assertEqual(position.received_payload(db, 'shore-accept', 'r1').hex(), '0000000001')
            self.assertIsNone(position.derived_payload(db, 'shore-accept', 'r1'))

    def test_derived_witnesses_trace_only_to_received_evidence(self):
        lock_base(self.db)
        status = position.batch_status(self.db, 'lock-demo')
        witnesses = status['derived']['lA']['witnesses']
        self.assertEqual(witnesses, ['lk1', 'lk2'])
        for ref in witnesses:
            parity = one(self.db, 'SELECT 1 FROM packet_parities WHERE evidence=?', ref)
            original = one(self.db, 'SELECT 1 FROM packet_originals WHERE batch=? AND orig=?',
                           'lock-demo', ref)
            self.assertTrue(parity or original, f'untraceable witness {ref}')

    def test_invalid_raw_payload_is_kept_but_not_admitted_as_fix(self):
        self.event('inv-gamma', 'buoy', 'gamma', 0, 0, 'precision')
        self.event('inv-batch', 'packet-batch', 'bad-raw', 5)
        self.event('inv-catalog', 'packet-catalog', 'bad-raw', [['z', 'gamma', 40, 'beacon']])
        self.event('inv-data', 'packet-original', 'bad-raw', 'z', '0000000009')  # error=9 不过字段检查
        self.assertIsNotNone(position.received_payload(self.db, 'bad-raw', 'z'))
        self.assertIsNone(one(self.db, "SELECT 1 FROM fixes WHERE buoy='gamma' AND minute=40"))
        self.assertEqual(one(self.db, "SELECT state FROM fix_support WHERE batch='bad-raw'"), ('rejected',))

    def test_unique_but_invalid_derived_payload_is_not_admitted(self):
        self.event('id-gamma', 'buoy', 'gamma', 0, 0, 'precision')
        self.event('id-batch', 'packet-batch', 'bad-derived', 5)
        self.event('id-catalog', 'packet-catalog', 'bad-derived', [
            ['t1', 'gamma', 40, 'beacon'], ['q1', 'gamma', 41, 'shore'], ['q2', 'gamma', 42, 'ranger']])
        bad = '0000000009'
        q1 = position.encode_payload(4, 4, 1).hex()
        q2 = position.encode_payload(5, 5, 1).hex()

        def xor_hex(*hs):
            out = bytearray(position.PAYLOAD_LEN)
            for h in hs:
                for i, v in enumerate(bytes.fromhex(h)):
                    out[i] ^= v
            return out.hex()

        self.event('id-p1', 'packet-parity', 'id-k1', 'bad-derived', ['t1', 'q1', 'q2'], xor_hex(bad, q1, q2))
        self.event('id-p2', 'packet-parity', 'id-k2', 'bad-derived', ['q1', 'q2'], xor_hex(q1, q2))
        self.assertIsNotNone(position.derived_payload(self.db, 'bad-derived', 't1'))
        self.assertIsNone(one(self.db, "SELECT 1 FROM fixes WHERE buoy='gamma' AND source='beacon' AND minute=40"))
        self.assertEqual(one(self.db, "SELECT state FROM fix_support WHERE orig='t1'"), ('rejected',))


class JointRecoveryFleetTest(LedgerTest):
    def test_jointly_determined_payload_enters_forensics(self):
        lock_base(self.db)
        status = position.batch_status(self.db, 'lock-demo')
        self.assertEqual(status['state'], 'open')
        self.assertEqual(list(status['derived']), ['lA'])
        self.assertEqual(status['pending'], ['d1', 'd2'])
        self.assertEqual(position.envelope(self.db, 'alpha', 25), ('trusted', (11, 0, 3)))
        self.assertEqual(fleet.options(self.db, 'alpha'), ['narrow', 'wide'])

    def test_later_contradiction_removes_derived_observation_and_options(self):
        lock_base(self.db)
        self.assertEqual(fleet.options(self.db, 'alpha'), ['narrow', 'wide'])
        load(self.db, LOCK_CONFLICT)
        status = position.batch_status(self.db, 'lock-demo')
        self.assertEqual(status['state'], 'conflict')
        self.assertEqual([c['code'] for c in status['conflicts']], ['infeasible-parity'])
        self.assertEqual(status['conflicts'][0]['refs'], ['lk1', 'lk3'])
        self.assertIsNone(one(self.db, "SELECT 1 FROM fixes WHERE buoy='alpha' AND source='beacon'"))
        self.assertEqual(position.envelope(self.db, 'alpha', 25)[0], 'unconfirmed')
        self.assertEqual(fleet.options(self.db, 'alpha'), [])

    def test_conflict_forces_lift_refusal_with_traceable_refs(self):
        lock_base(self.db)
        self.event('rsv', 'reserve', 'alpha', 'narrow')
        load(self.db, LOCK_CONFLICT)
        with self.assertRaises(ValueError) as ctx:
            self.event('lift-after-conflict', 'lift', 'alpha', 27)
        message = str(ctx.exception)
        self.assertIn('position unqualified', message)
        self.assertIn('lk1', message)
        self.assertIn('lk3', message)
        self.assertEqual(one(self.db, 'SELECT trip,lifted FROM recoveries WHERE buoy=?', 'alpha'),
                         ('narrow', None))

    def test_retransmitted_original_contradicting_derivation_conflicts(self):
        lock_base(self.db)
        self.assertEqual(position.derived_payload(self.db, 'lock-demo', 'lA').hex(), '000a000001')
        # 事后补到的 lA 原件却是另一份内容，与既有联立推导矛盾。
        self.event('lA-wrong', 'packet-retransmit', 'lock-demo', 'lA', '000b000001')
        status = position.batch_status(self.db, 'lock-demo')
        self.assertEqual(status['state'], 'conflict')
        self.assertEqual([c['code'] for c in status['conflicts']], ['infeasible-parity'])
        self.assertIn('lA', status['conflicts'][0]['refs'])
        self.assertTrue({'lk1', 'lk2'}.intersection(status['conflicts'][0]['refs']))
        self.assertEqual(status['derived']['lA']['state'], 'withdrawn')
        # 新到的真实原件本身仍原样可查。
        self.assertEqual(position.received_payload(self.db, 'lock-demo', 'lA').hex(), '000b000001')

    def test_completed_lift_survives_later_conflict_and_stays_queryable(self):
        lock_base(self.db)
        self.event('rsv', 'reserve', 'alpha', 'narrow')
        self.event('lift-ok', 'lift', 'alpha', 27)
        self.assertEqual(one(self.db, 'SELECT lifted FROM recoveries WHERE buoy=?', 'alpha'), (27,))
        load(self.db, LOCK_CONFLICT)
        # 已经真实完成的起吊不被回滚，仍可查询。
        self.assertEqual(one(self.db, 'SELECT trip,lifted FROM recoveries WHERE buoy=?', 'alpha'),
                         ('narrow', 27))
        with self.assertRaisesRegex(ValueError, 'position unqualified'):
            self.event('lift-again', 'lift', 'alpha', 30)

    def test_packet_conclusions_and_derived_fixes_replay_from_journal(self):
        lock_base(self.db)
        self.event('rsv', 'reserve', 'alpha', 'narrow')
        load(self.db, LOCK_CONFLICT)
        before = snapshot(self.db)
        events = [json.loads(payload) for payload, in self.db.execute('SELECT payload FROM journal ORDER BY rowid')]
        with closing(connect(':memory:')) as rebuilt:
            load(rebuilt, events)
            self.assertEqual(snapshot(rebuilt), before)
            self.assertEqual(position.batch_status(rebuilt, 'lock-demo')['state'], 'conflict')
            self.assertIsNone(one(rebuilt, "SELECT 1 FROM fixes WHERE buoy='alpha' AND source='beacon'"))


class SparseParityRegressionTest(LedgerTest):
    """36 个未收到负载 + 8 条独立拼校约束的联立回归。

    u00 只能由 k0(x0,x1,x2) 与 k1(x1,x2) 两式联立得到；
    u04 只能由 k3(x4,x5,x6) 与 k4(x5,x6) 两式联立得到。没有任何一条校验式初始只剩一个未知数，
    因此剥壳法一无所获，候选字节串枚举更不可行，必须做整批 GF(2) 消元。
    k2、k5-k7 是另外数条独立约束，各自保留多个待收原件。
    """

    GROUPS = {
        'k0': [0, 1, 2], 'k1': [1, 2],
        'k2': [3, 4, 5], 'k3': [4, 5, 6], 'k4': [5, 6],
        'k5': [7, 8, 9], 'k6': [8, 9, 10], 'k7': [9, 10, 11],
    }

    def build_regression(self):
        for i in range(36):
            self.db.execute('INSERT INTO buoys VALUES(?,?,?,?)',
                            (f'u{i:02d}', i - 18, (i * 7) % 13 - 6, 'precision'))
        position.open_batch(self.db, 'reg36')
        position.add_catalog(self.db, 'reg36', [
            (f'o{i:02d}', f'u{i:02d}', 20, 'beacon') for i in range(36)])
        truth = [position.encode_payload(i - 18, (i * 7) % 13 - 6, 1 + i % 8) for i in range(36)]
        for evidence, ids in self.GROUPS.items():
            checksum = bytearray(position.PAYLOAD_LEN)
            for i in ids:
                for j, value in enumerate(truth[i]):
                    checksum[j] ^= value
            position.add_parity(self.db, evidence, 'reg36', [f'o{i:02d}' for i in ids], bytes(checksum))
        return truth

    def test_thirty_six_missing_payloads_jointly_recover_unique_ones(self):
        truth = self.build_regression()
        # 36 个定长负载全部未收到。
        self.assertEqual(one(self.db, 'SELECT count(*) FROM packet_catalog WHERE batch=?', 'reg36'), (36,))
        self.assertEqual(one(self.db, 'SELECT count(*) FROM packet_originals WHERE batch=?', 'reg36'), (0,))
        self.assertEqual({r[0] for r in self.db.execute(
            'SELECT DISTINCT length FROM packet_batches WHERE batch=?', ('reg36',))}, {5})

        # 8 条约束线性独立。
        matrix = []
        for ids in self.GROUPS.values():
            bits = 0
            for i in ids:
                bits |= 1 << i
            matrix.append(bits)
        self.assertEqual(gf2_rank(matrix, 36), len(self.GROUPS))

        # 回归结构不能被“单未知数剥壳”解决：初始没有单未知数式，剥壳集合为空。
        member_sets = [[f'o{i:02d}' for i in ids] for ids in self.GROUPS.values()]
        self.assertTrue(all(len(group) >= 2 for group in member_sets))
        self.assertEqual(peel_singletons(member_sets), set())

        status = position.batch_status(self.db, 'reg36')
        self.assertEqual(sorted(status['derived']), ['o00', 'o04'])
        self.assertEqual(len(status['pending']), 34)
        self.assertEqual(position.derived_payload(self.db, 'reg36', 'o00'), truth[0])
        self.assertEqual(position.derived_payload(self.db, 'reg36', 'o04'), truth[4])
        # 唯一且字段合法的两条进入现有 fixes 取证，其余 34 条仍待收。
        self.assertEqual(one(self.db, 'SELECT count(*) FROM fixes'), (2,))
        self.assertEqual(one(self.db, "SELECT x,y,error FROM fixes WHERE buoy='u00'"),
                         position.decode_payload(truth[0]))
        self.assertEqual(one(self.db, "SELECT x,y,error FROM fixes WHERE buoy='u04'"),
                         position.decode_payload(truth[4]))
        # 推导负载的回查证人只能是实际收到的拼校包。
        self.assertEqual(status['derived']['o00']['witnesses'], ['k0', 'k1'])
        self.assertEqual(status['derived']['o04']['witnesses'], ['k3', 'k4'])

    def test_committed_fixture_loads_through_journal_and_replays(self):
        rows = json.loads((FIXTURES / 'recovery-regression.json').read_text(encoding='utf-8'))
        load(self.db, rows)
        truth = [position.encode_payload(i - 18, (i * 7) % 13 - 6, 1 + i % 8) for i in range(36)]
        self.assertEqual(one(self.db, 'SELECT count(*) FROM packet_catalog WHERE batch=?', 'reg36'), (36,))
        self.assertEqual(one(self.db, 'SELECT count(*) FROM packet_originals WHERE batch=?', 'reg36'), (0,))
        self.assertEqual(position.derived_payload(self.db, 'reg36', 'o00'), truth[0])
        self.assertEqual(position.derived_payload(self.db, 'reg36', 'o04'), truth[4])
        self.assertEqual(one(self.db, 'SELECT count(*) FROM fixes'), (2,))
        # 整条 46 事件夹具可由 journal 重放并逐表重验。
        journal = [json.loads(p) for p, in self.db.execute('SELECT payload FROM journal ORDER BY rowid')]
        with closing(connect(':memory:')) as rebuilt:
            load(rebuilt, journal)
            self.assertEqual(snapshot(rebuilt), snapshot(self.db))

    def test_contradiction_in_regression_batch_withdraws_every_derivation(self):
        truth = self.build_regression()
        self.assertEqual(one(self.db, 'SELECT count(*) FROM fixes'), (2,))
        bad = bytearray(truth[0])
        bad[0] ^= 0xFF
        position.add_parity(self.db, 'k8', 'reg36', ['o00', 'o01', 'o02'], bytes(bad))
        status = position.batch_status(self.db, 'reg36')
        self.assertEqual(status['state'], 'conflict')
        self.assertEqual(status['conflicts'][0]['refs'], ['k0', 'k8'])
        self.assertEqual({o: d['state'] for o, d in status['derived'].items()},
                         {'o00': 'withdrawn', 'o04': 'withdrawn'})
        # 整批推导禁止再作位置证据；没有原始负载，fixes 全部撤回。
        self.assertEqual(one(self.db, 'SELECT count(*) FROM fixes'), (0,))
        self.assertEqual(fleet.options(self.db, 'u00'), [])

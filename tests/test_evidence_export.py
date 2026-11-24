import json
import unittest
from contextlib import closing

from buoy_recovery import load, snapshot
from evidence_export import capture, verify
from recovery_support import FIXTURES
from store import connect


def fixture(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


class EvidenceExportTest(unittest.TestCase):
    def test_pack_replays_both_original_checkpoints_without_writing_source(self):
        with closing(connect(':memory:')) as db:
            load(db, fixture('demo'))
            before = snapshot(db)
            packed = capture(db, 'alpha', ['a6', 'water'])
            self.assertEqual(snapshot(db), before)
        checkpoints = verify(packed)
        self.assertEqual(checkpoints['water']['continuity'], 'hold-position')
        self.assertEqual(checkpoints['a6']['continuity'], 'continuous')
        self.assertEqual(checkpoints['a6']['recovery'], ['wide', 27, 53])

    def test_mismatching_claim_fails_offline_verification(self):
        with closing(connect(':memory:')) as db:
            load(db, fixture('demo'))
            packed = capture(db, 'alpha', ['water'])
        packed['checkpoints']['water']['continuity'] = 'continuous'
        with self.assertRaisesRegex(ValueError, 'checkpoint outcome mismatch'):
            verify(packed)

    def test_later_facts_do_not_change_exported_cut(self):
        with closing(connect(':memory:')) as db:
            records = fixture('demo')
            load(db, records)
            packed = capture(db, 'alpha', ['a6'])
            load(db, fixture('late-fix'))
            self.assertEqual(capture(db, 'alpha', ['a6']), packed)
            self.assertEqual(packed['events'], records)

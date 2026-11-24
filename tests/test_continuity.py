import json
import sqlite3

import continuity
import fleet
import position
from buoy_recovery import load
from recovery_support import FIXTURES, LedgerTest
from store import one


class ContinuityTest(LedgerTest):
    def test_clock_pair_is_scoped_to_sensor_and_tenure(self):
        self.through('swap-s')
        for buoy, sensor in [('beta', 'sen-new'), ('alpha', 'bat-new')]:
            with self.subTest(buoy=buoy,sensor=sensor), self.assertRaisesRegex(ValueError, 'requires attached sensor'):
                self.event('wrong', 'pair', buoy, sensor, 60, 62)
        for sensor, ref in [('sen-new', 56), ('sen-old', 56)]:
            with self.subTest(sensor=sensor), self.assertRaisesRegex(ValueError, 'outside sensor tenure'):
                self.event('boundary', 'pair', 'alpha', sensor, ref, ref)
        self.row('p1')
        self.assertEqual(one(self.db, "SELECT ref,local FROM pairs WHERE sensor='sen-new'"), (60, 62))

    def test_clock_uses_latest_in_window_and_rejects_same_time_conflict(self):
        self.through('a6', omit=('p1',))
        self.assertEqual(self.qualification(), 'hold-clock')
        self.row('p1')
        self.event('old', 'pair', 'alpha', 'sen-new', 59, 120)
        self.assertEqual(self.qualification(), 'continuous')
        with self.assertRaises(sqlite3.IntegrityError):
            self.event('same-ref', 'pair', 'alpha', 'sen-new', 60, 70)
        self.assertEqual(one(self.db, "SELECT local FROM pairs WHERE sensor='sen-new' AND ref=60"), (62,))
        self.event('latest', 'pair', 'alpha', 'sen-new', 61, 65)
        self.assertEqual(self.qualification(), 'hold-clock')

    def test_clock_rejects_agreeing_but_excessive_absolute_offsets(self):
        self.through('a6', omit=('p0', 'p1'))
        self.event('old-fast', 'pair', 'alpha', 'sen-old', 25, 31)
        self.event('new-fast', 'pair', 'alpha', 'sen-new', 60, 66)
        self.assertEqual(self.qualification(), 'hold-clock')

    def test_clock_reference_gap_is_capped_at_45_minutes(self):
        self.through('a6', omit=('p0',))
        self.event('too-far', 'pair', 'alpha', 'sen-old', 14, 15)
        self.assertEqual(self.qualification(), 'hold-clock')
        self.event('edge', 'pair', 'alpha', 'sen-old', 15, 16)
        self.assertEqual(self.qualification(), 'continuous')

    def test_configured_spatial_limit_changes_qualification_only(self):
        self.through('t3')
        self.event('small-zone', 'limits', 'alpha', 1, 2, 5)
        self.assertEqual(fleet.options(self.db, 'alpha'), ['narrow', 'wide'])
        self.through('a6')
        self.assertEqual(self.qualification(), 'hold-position')

    def test_configured_absolute_clock_limit_changes_qualification(self):
        self.through('p1')
        self.event('strict-offset', 'limits', 'alpha', 1, 8, 1)
        self.through('a6')
        self.assertEqual(self.qualification(), 'hold-clock')

    def test_deployment_requires_both_swaps_and_follows_their_times(self):
        self.through('swap-b')
        with self.assertRaisesRegex(ValueError, 'requires both swaps'):
            self.row('water')
        self.row('swap-s')
        with self.assertRaisesRegex(ValueError, 'requires both swaps'):
            self.event('same-time', 'redeploy', 'alpha', 56)
        self.row('water')
        self.assertEqual(one(self.db, 'SELECT buoy,minute FROM deployments'), ('alpha', 67))

    def test_return_requires_two_post_deployment_fixes_in_window(self):
        self.through('water')
        self.assertEqual(position.envelope(self.db, 'alpha', 67), ('trusted', (1, 0, 2)))
        self.assertEqual(self.qualification(), 'hold-position')
        self.row('a5')
        self.event('after-window', 'fix', 'alpha', 'shore', 78, 30, 0, 1)
        self.assertEqual(self.qualification(), 'hold-position')
        self.event('exact-cutoff', 'fix', 'alpha', 'shore', 77, 1, 0, 1)
        self.assertEqual(self.qualification(), 'continuous')

    def test_late_spatial_conflict_does_not_erase_deployment_or_landing(self):
        self.through('a6')
        self.assertEqual(self.qualification(), 'continuous')
        load(self.db, json.loads((FIXTURES / 'late-fix.json').read_text(encoding='utf-8')))
        self.assertEqual(self.qualification(), 'hold-position')
        self.assertEqual(one(self.db, 'SELECT minute FROM deployments'), (67,))
        self.assertEqual(one(self.db, 'SELECT landed FROM recoveries'), (53,))

    def test_limits_are_buoy_scoped_versioned_and_locked_on_deployment(self):
        self.through('t3')
        self.event('alpha-v2', 'limits', 'alpha', 2, 3, 2)
        self.event('alpha-v1', 'limits', 'alpha', 1, 1, 0)
        self.event('beta-v2', 'limits', 'beta', 2, 1, 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.event('alpha-same', 'limits', 'alpha', 2, 8, 5)
        self.assertEqual(continuity.limits(self.db, 'alpha'), (3, 2))
        self.assertEqual(continuity.limits(self.db, 'beta'), (1, 0))
        self.assertEqual(fleet.options(self.db, 'beta'), ['wide'])
        self.through('a6')
        self.assertEqual(self.qualification(), 'continuous')
        with self.assertRaisesRegex(ValueError, 'limits locked'):
            self.event('alpha-v3', 'limits', 'alpha', 3, 1, 0)
        self.event('beta-v3', 'limits', 'beta', 3, 2, 0)
        self.assertEqual(continuity.limits(self.db, 'beta'), (2, 0))
        self.assertEqual(continuity.limits(self.db, 'alpha'), (3, 2))

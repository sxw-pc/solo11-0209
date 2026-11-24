import json
import unittest
from pathlib import Path

import continuity
from buoy_recovery import load
from store import connect

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
EVENTS = json.loads((FIXTURES / 'demo.json').read_text(encoding='utf-8'))
INDEX = {row[0]: i for i, row in enumerate(EVENTS)}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.addCleanup(self.db.close)

    def through(self, event, omit=()):
        load(self.db, [row for row in EVENTS[:INDEX[event] + 1] if row[0] not in omit])

    def event(self, *args):
        load(self.db, [list(args)])

    def row(self, event):
        load(self.db, [EVENTS[INDEX[event]]])

    def qualification(self):
        return continuity.status(self.db, 'alpha')

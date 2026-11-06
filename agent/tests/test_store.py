"""Unit tests for agent/store.py — run from agent/:  python3 -m unittest"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from store import Outbox, load_json, save_json


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_roundtrip_and_defaults(self):
        p = self.dir / "session.json"
        self.assertEqual(load_json(p, {"x": 1}), {"x": 1})
        save_json(p, {"date": "2026-11-09", "session": None})
        self.assertEqual(load_json(p, None), {"date": "2026-11-09", "session": None})
        self.assertFalse((self.dir / "session.json.tmp").exists())

    def test_corrupt_file_falls_back_and_is_kept_aside(self):
        p = self.dir / "cache.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(load_json(p, None))
        self.assertTrue((self.dir / "cache.json.corrupt").exists())

    def test_outbox_durable_dedup_and_remove(self):
        p = self.dir / "outbox.json"
        ob = Outbox(p)
        ob.add([{"eventId": "a", "type": "rearm"}, {"eventId": "b", "type": "rearm"}], datetime(2026, 11, 9, 8, 0))
        ob.add([{"eventId": "a", "type": "rearm"}])                # duplicate ignored
        self.assertEqual(len(ob), 2)
        again = Outbox(p)                                          # survives a restart
        self.assertEqual([e["eventId"] for e in again.batch()], ["a", "b"])
        self.assertEqual(again.oldest_queued_at(), datetime(2026, 11, 9, 8, 0))
        again.remove(["a"])
        self.assertEqual([e["eventId"] for e in Outbox(p).batch()], ["b"])


if __name__ == "__main__":
    unittest.main()

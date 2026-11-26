import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend import atomic_json


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "manifest.json"
        atomic_json.write_json(self.path, {"old": True})

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_reader_releases_windows_lock_and_checkpoint_is_saved(self):
        reader = self.path.open("r", encoding="utf-8")
        release = threading.Timer(.2, reader.close)
        release.start()
        try:
            atomic_json.write_json(self.path, {"saved": "新清单"})
        finally:
            release.join()
            reader.close()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"saved": "新清单"})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_permanent_lock_keeps_old_and_complete_recovery_checkpoint(self):
        with patch.object(atomic_json.os, "replace", side_effect=PermissionError("locked")) as replace, patch.object(atomic_json.time, "sleep"):
            with self.assertRaisesRegex(PermissionError, "最新临时清单已保留"):
                atomic_json.write_json(self.path, {"new": True})
        self.assertEqual(replace.call_count, len(atomic_json.REPLACE_RETRY_DELAYS) + 1)
        self.assertEqual(json.loads(self.path.read_text()), {"old": True})
        recovery, = self.path.parent.glob("*.tmp")
        self.assertEqual(json.loads(recovery.read_text()), {"new": True})

    def test_serialization_failure_keeps_old_and_cleans_partial_temp(self):
        with self.assertRaises(TypeError):
            atomic_json.write_json(self.path, {"bad": object()})
        self.assertEqual(json.loads(self.path.read_text()), {"old": True})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

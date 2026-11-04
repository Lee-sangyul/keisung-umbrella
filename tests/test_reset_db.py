"""Exercise WAL checkpoint disappearance, offline snapshots and safe failures."""
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import reset_db


class SafeResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.db = self.root / "umbrellas.db"

    def make_db(self, wal=False):
        c = sqlite3.connect(self.db)
        if wal:
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE student (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO student VALUES (1, 'existing')")
        c.commit()
        c.close()

    def validate_snapshot(self, folder):
        snapshot = folder / "umbrellas.db"
        self.assertTrue(snapshot.is_file())
        c = sqlite3.connect(snapshot)
        try:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("SELECT name FROM student").fetchone()[0], "existing")
        finally:
            c.close()

    def test_closed_normal_database_snapshot_and_archive(self):
        self.make_db()
        target=self.root / "backups" / "one"
        reset_db.backup_and_archive(self.db,target)
        self.assertFalse(self.db.exists())
        self.assertTrue((target / "umbrellas.db.original").exists())
        self.validate_snapshot(target)

    def test_wal_created_then_disappears_during_sqlite_access(self):
        self.make_db(wal=True)
        target=self.root / "backups" / "two"
        reset_db.backup_and_archive(self.db,target)
        self.assertFalse(self.db.exists())
        self.validate_snapshot(target)

    def test_nonempty_sidecar_refuses_reset_and_keeps_original(self):
        self.make_db()
        sidecar=self.root / "umbrellas.db-wal"
        target=self.root / "backups" / "three"
        real_connect = sqlite3.connect

        def patched_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            if args[0] != self.db:
                return connection

            class Proxy:
                def __getattr__(self, key):
                    return getattr(connection, key)

                def close(self):
                    connection.close()
                    # Simulate a sidecar appearing immediately after SQLite closes.
                    sidecar.write_bytes(b"new activity")

            return Proxy()

        with patch.object(reset_db.sqlite3, "connect", side_effect=patched_connect):
            with self.assertRaisesRegex(RuntimeError,"WAL/SHM"):
                reset_db.backup_and_archive(self.db,target)
        self.assertTrue(self.db.exists())
        self.assertTrue(sidecar.exists())
        self.validate_snapshot(target)

    def test_atomic_move_failure_preserves_original_and_snapshot(self):
        self.make_db()
        target=self.root / "backups" / "four"
        with patch.object(reset_db.os,"replace",side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                reset_db.backup_and_archive(self.db,target)
        self.assertTrue(self.db.exists())
        self.validate_snapshot(target)

    def test_confirmation_required(self):
        self.make_db()
        with patch.object(reset_db, "ROOT", self.root),\
             patch.object(reset_db, "load_dotenv"),\
             patch.dict(reset_db.os.environ, {"DATA_DIR":str(self.root)}),\
             patch("builtins.input",return_value="NO"):
            self.assertEqual(reset_db.main(),1)
        self.assertTrue(self.db.exists())
        self.assertFalse((self.root / "backups").exists())


if __name__=="__main__":
    unittest.main()

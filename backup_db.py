#!/usr/bin/env python3
"""Consistent online backup of the Keisung Umbrella SQLite database.

Produces a single SQLite snapshot, including committed WAL content. Backups
contain private student records and password/PIN hashes: transfer to an
institution-approved encrypted off-host destination and test restores.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def create_backup(db_path: Path, directory: Path | None = None) -> Path:
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    backups = Path(directory).resolve() if directory else db_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backups, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"umbrella-{stamp}-{secrets.token_hex(4)}.db"
    final = backups / filename
    partial = backups / ("." + filename + ".tmp")
    try:
        # Read-only backup source: does not alter the active WAL or original DB.
        with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro",
                                     uri=True, timeout=15)) as source:
            with closing(sqlite3.connect(partial)) as target:
                source.backup(target, pages=200, sleep=0.1)
                target.commit()
        os.chmod(partial, 0o600)
        with closing(sqlite3.connect(f"file:{partial.as_posix()}?mode=ro",
                                     uri=True)) as check:
            row = check.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError("Backup integrity_check failed.")
        os.replace(partial, final)
        return final
    finally:
        if partial.exists():
            partial.unlink()


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    db = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve() / "umbrellas.db"
    try:
        result = create_backup(db)
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"BACKUP FAILED: {error}")
        return 1
    print(f"Backup OK: {result}")
    print("IMPORTANT: Copy backups to encrypted approved off-server storage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

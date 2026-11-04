#!/usr/bin/env python3
"""Reset Keisung Umbrella *test* DB only after a verified SQLite backup.

Stop Uvicorn and all DB clients before use. Never run on production data.
A WAL or SHM sidecar may be removed automatically when SQLite closes.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def backup_and_archive(db_path: Path, backup_dir: Path) -> None:
    """Backup using SQLite's consistent snapshot, then archive original.

    On verification/checkpoint failure, leave the source DB untouched.
    """
    if not db_path.is_file():
        raise RuntimeError("원본 DB 파일이 없습니다.")
    backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot = backup_dir / "umbrellas.db"
    original = backup_dir / "umbrellas.db.original"

    # Opening and closing this connection may delete obsolete WAL/-shm files.
    # Never cache a list of sidecars for later movement.
    conn = sqlite3.connect(db_path, timeout=0.2, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=200")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and checkpoint[0] != 0:
            raise RuntimeError("WAL이 사용 중입니다. 모든 서버와 DB 연결을 종료하세요.")
        target = sqlite3.connect(snapshot)
        try:
            conn.backup(target)
        finally:
            target.close()
    finally:
        conn.close()

    with sqlite3.connect(snapshot) as validation:
        if validation.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("백업 DB 무결성 검사에 실패했습니다.")

    # Missing WAL/SHM is normal; persistent sidecars can indicate a still
    # active database. Refuse to reset rather than separate a WAL from its DB.
    remaining = [
        db_path.with_name(db_path.name + suffix)
        for suffix in ("-wal", "-shm")
        if db_path.with_name(db_path.name + suffix).exists()
    ]
    if remaining:
        raise RuntimeError("WAL/SHM 파일이 남아 있습니다. 서버를 종료하세요: " +
                           ", ".join(p.name for p in remaining))
    if not db_path.is_file():
        raise RuntimeError("원본 DB가 사라졌습니다. 백업 파일을 확인하세요.")

    # Keep both the SQLite-consistent snapshot and the original DB file.
    # os.replace is atomic on a single filesystem, avoiding partial copies.
    os.replace(db_path, original)
    print("✅ DB 무결성 검사 통과: " + str(snapshot))
    print("✅ 이전 원본 보관: " + str(original))


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
    db_path = data_dir / "umbrellas.db"
    if not db_path.is_file():
        print(f"❌ DB 파일을 찾지 못했습니다: {db_path}")
        return 1
    print("\n☂️ 계성 우산 DB 초기화 (테스트 전용)")
    print(f"대상: {db_path}")
    print("⚠️ Uvicorn 및 DB 사용하는 모든 프로그램을 먼저 종료하세요.")
    print("⚠️ DB 초기화는 기존 우산·학생·대여·패널티와 PIN에 영향을 줍니다.")
    print("⚠️ 기존 QR 우산 번호와 새 DB의 번호가 충돌할 수 있습니다.\n")
    try:
        confirmation = input("테스트 데이터 초기화를 진행하려면 RESET 입력: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n취소되었습니다. DB는 변경되지 않았습니다.")
        return 1
    if confirmation != "RESET":
        print("취소되었습니다. DB는 변경되지 않았습니다.")
        return 1
    backup_dir = data_dir / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    try:
        backup_and_archive(db_path, backup_dir)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"❌ 초기화 중단: {exc}")
        print(f"원본 DB 경로를 확인하세요: {db_path}")
        print(f"백업 폴더(있는 경우): {backup_dir}")
        return 1
    print("\nUvicorn을 다시 시작하면 새 테스트 DB가 생성됩니다.")
    print("🔒 백업에 개인정보·PIN 해시가 포함되므로 공개 저장소에 올리지 마세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

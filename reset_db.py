#!/usr/bin/env python3
"""Safely reset the Keisung Umbrella *test* SQLite database.

Usage:
    python reset_db.py

Stops at a confirmation prompt and moves the existing SQLite database and WAL
sidecars into a timestamped private backup directory. The application recreates
its schema and bootstrap admin on the next server start.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import os


ROOT = Path(__file__).resolve().parent


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)

    # Match the actual server.py DB_PATH resolution.
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
    db_path = data_dir / "umbrellas.db"
    if not db_path.is_file():
        print(f"❌ DB 파일을 찾지 못했어: {db_path}")
        print("DB가 이미 초기화되어 있거나 DATA_DIR 설정이 다른지 확인해 줘.")
        return 1

    files = [
        data_dir / ("umbrellas.db" + suffix)
        for suffix in ("", "-wal", "-shm")
    ]
    present = [path for path in files if path.exists()]

    print("\n☂️ 계성 우산 DB 초기화 (테스트 전용)")
    print(f"대상: {db_path}")
    print("⚠️ 실행 전 Uvicorn 및 DB를 사용하는 모든 프로그램을 종료해야 해.")
    print("⚠️ 우산/대여/패널티/정책/관리자 세션 데이터가 새 DB에서 초기화돼.")
    print("⚠️ 초기화 후 UMB-0001부터 다시 시작돼 기존 QR 스티커가 잘못 연결될 수 있어.")
    print("기존 DB는 삭제하지 않고 백업 폴더로 옮길 거야.\n")

    try:
        confirmation = input("테스트 데이터 초기화를 진행하려면 RESET 입력: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n취소했어. DB는 변경하지 않았어.")
        return 1

    if confirmation != "RESET":
        print("취소했어. DB는 변경하지 않았어.")
        return 1

    # Best-effort lock detection; still require the operator to stop all servers.
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=0.2)
        try:
            connection.execute("PRAGMA busy_timeout=200")
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        print(f"❌ DB가 사용 중이거나 접근할 수 없어: {exc}")
        print("Uvicorn 및 다른 DB 접속을 종료하고 다시 실행해 줘.")
        return 1

    backup_dir = data_dir / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    moved: list[tuple[Path, Path]] = []
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)
        for source in present:
            dest = backup_dir / source.name
            shutil.move(str(source), str(dest))
            moved.append((source, dest))
    except (OSError, shutil.Error) as exc:
        print(f"❌ 백업 중 오류: {exc}")
        print("이동했던 파일을 원래 위치로 되돌리는 중이야.")
        for original, archived in reversed(moved):
            try:
                shutil.move(str(archived), str(original))
            except (OSError, shutil.Error) as rollback_exc:
                print(f"⚠️ 자동 복구 실패: {archived} → {original}: {rollback_exc}")
        return 1

    print("\n✅ 이전 DB를 안전한 백업 폴더로 이동했어.")
    print(f"백업 위치: {backup_dir}")
    print("이제 Uvicorn을 다시 실행하면 새로운 DB가 생성돼.")
    print("참고: 새 DB의 기본 월 대여 한도는 10회야.")
    print("🔒 백업 파일에는 학생 정보가 포함될 수 있으니 외부에 공유하지 마.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

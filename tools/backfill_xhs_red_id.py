# -*- coding: utf-8 -*-
"""用一个内存 CDP 会话为历史 SQLite 小红书记录补全公开 red_id。"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.db_config import sqlite_db_config
from database import db_session
from media_platform.xhs.dm_bot import XiaohongshuDmBot


_INTERNAL_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def _pending_user_ids(db_path: Path, limit: int = 0) -> list[str]:
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        rows = conn.execute(
            """
            SELECT creator_hash FROM xhs_note_comment
             WHERE COALESCE(red_id, '') = ''
            UNION
            SELECT creator_hash FROM xhs_note
             WHERE COALESCE(red_id, '') = ''
            """
        ).fetchall()
    result = sorted({str(row[0] or "").strip() for row in rows})
    result = [user_id for user_id in result if _INTERNAL_ID_RE.fullmatch(user_id)]
    return result[:limit] if limit > 0 else result


def _save_mapping(db_path: Path, user_id: str, red_id: str) -> None:
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        for table_name in ("xhs_note", "xhs_note_comment"):
            conn.execute(
                f"""UPDATE {table_name}
                       SET red_id = ?
                     WHERE creator_hash = ? AND COALESCE(red_id, '') = ''""",
                (red_id, user_id),
            )
        conn.commit()


async def backfill(limit: int = 0, dry_run: bool = False) -> dict[str, int]:
    await db_session.create_tables("sqlite")
    db_path = Path(sqlite_db_config["db_path"]).resolve()
    user_ids = _pending_user_ids(db_path, limit=limit)
    print(f"[xhs red_id] SQLite={db_path}, pending_unique_users={len(user_ids)}")
    if dry_run or not user_ids:
        return {"pending": len(user_ids), "updated": 0, "failed": 0}

    bot = XiaohongshuDmBot()
    updated = 0
    failed = 0
    try:
        await bot.setup()
        for index, user_id in enumerate(user_ids, 1):
            try:
                red_id, nickname = await bot.read_public_red_id(user_id)
                _save_mapping(db_path, user_id, red_id)
                updated += 1
                print(f"[{index}/{len(user_ids)}] OK {nickname}: {user_id} -> {red_id}")
            except Exception as exc:
                failed += 1
                print(f"[{index}/{len(user_ids)}] SKIP {user_id}: {type(exc).__name__}: {exc}")
            await asyncio.sleep(0.5)
    finally:
        await bot.close()
    return {"pending": len(user_ids), "updated": updated, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 SQLite 中历史小红书公开账号 red_id")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个用户；0 表示全部")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不连接 Chrome、不更新数据库")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    result = asyncio.run(backfill(limit=max(0, args.limit), dry_run=args.dry_run))
    print(f"[xhs red_id] done: {result}")


if __name__ == "__main__":
    main()

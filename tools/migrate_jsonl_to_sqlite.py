# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/migrate_jsonl_to_sqlite.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。

"""
一次性迁移脚本：把历史 jsonl 数据（data/<平台>/jsonl/*.jsonl）导入 SQLite。

- 按平台主键 upsert（内容表按 aweme_id/video_id/note_id/content_id，评论表按 comment_id），
  同一条记录跨日期文件重复出现时自动去重，重复执行幂等；
- jsonl 中的 douyin_id 会导入抖音作品/评论表；其他模型外字段按模型列过滤；
- 源 jsonl 文件保留不删除。

用法:
    uv run python tools/migrate_jsonl_to_sqlite.py                 # 默认 data/ -> database/sqlite_tables.db
    uv run python tools/migrate_jsonl_to_sqlite.py --data-dir X --db-path Y
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Add project root to sys.path（uv run python tools/xxx.py 时脚本目录在 sys.path[0]，项目根不在）
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from config.db_config import sqlite_db_config  # noqa: E402

# 数据目录名 -> (内容模型, 评论模型, 内容主键列, 评论主键列)，与 database/models.py 一致
PLATFORM_MAP = None  # 延迟导入，避免无谓加载 sqlalchemy


def _platform_map() -> Dict[str, Tuple[Any, Any, str, str]]:
    global PLATFORM_MAP
    if PLATFORM_MAP is None:
        from database import models

        PLATFORM_MAP = {
            "douyin": (models.DouyinAweme, models.DouyinAwemeComment, "aweme_id", "comment_id"),
            "bili": (models.BilibiliVideo, models.BilibiliVideoComment, "video_id", "comment_id"),
            "xhs": (models.XhsNote, models.XhsNoteComment, "note_id", "comment_id"),
            "kuaishou": (models.KuaishouVideo, models.KuaishouVideoComment, "video_id", "comment_id"),
            "weibo": (models.WeiboNote, models.WeiboNoteComment, "note_id", "comment_id"),
            "tieba": (models.TiebaNote, models.TiebaComment, "note_id", "comment_id"),
            "zhihu": (models.ZhihuContent, models.ZhihuComment, "content_id", "comment_id"),
        }
    return PLATFORM_MAP


async def _upsert(session, model, key_attr: str, record: Dict[str, Any]) -> bool:
    """按主键 upsert 单条记录（镜像各平台 DbStoreImplement 的语义）。"""
    from sqlalchemy import select

    from tools.utils import utils

    key_value = record.get(key_attr)
    if key_value is None:
        return False
    result = await session.execute(
        select(model).where(getattr(model, key_attr) == str(key_value))
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        record["add_ts"] = utils.get_current_timestamp()
        session.add(model(**record))
    else:
        for k, v in record.items():
            if hasattr(existing, k):
                setattr(existing, k, v)
    return True


async def run_migration(data_dir: Path, db_path: Path) -> Dict[str, int]:
    """遍历 data/<平台>/jsonl/*.jsonl 导入 SQLite，返回 {contents, comments, skipped} 计数。

    db_path 通过修改 sqlite_db_config["db_path"] 生效（get_async_engine 惰性按此建引擎）；
    先清引擎缓存——引擎按 db_type 缓存不按路径，复用旧路径引擎会写错库。
    """
    from database.db_session import create_tables, get_session, _engines

    sqlite_db_config["db_path"] = str(db_path)
    _engines.clear()
    await create_tables("sqlite")

    stats = {"contents": 0, "comments": 0, "skipped": 0}
    if not data_dir.is_dir():
        return stats

    for plat_dir in sorted(data_dir.iterdir()):
        if not plat_dir.is_dir():
            continue
        platform = plat_dir.name.lower()
        mapping = _platform_map().get(platform)
        jsonl_dir = plat_dir / "jsonl"
        if mapping is None or not jsonl_dir.is_dir():
            continue

        content_model, comment_model, content_key, _ = mapping
        for fp in sorted(jsonl_dir.glob("*.jsonl")):
            if "_comments_" in fp.name:
                model, key_attr, stat_key = comment_model, "comment_id", "comments"
            elif "_contents_" in fp.name:
                model, key_attr, stat_key = content_model, content_key, "contents"
            else:
                continue

            async with get_session(db_type="sqlite") as session:
                for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        stats["skipped"] += 1
                        continue
                    if not isinstance(raw, dict):
                        stats["skipped"] += 1
                        continue
                    # 过滤为模型声明的列，保留抖音 JSONL 中的公开 douyin_id。
                    record = {k: v for k, v in raw.items() if hasattr(model, k)}
                    if await _upsert(session, model, key_attr, record):
                        stats[stat_key] += 1
                    else:
                        stats["skipped"] += 1

    print(f"[migrate] 迁移完成: 内容 {stats['contents']} 条, 评论 {stats['comments']} 条, 跳过 {stats['skipped']} 行")
    return stats


MARKER_NAME = ".migrated_jsonl"


def has_jsonl_data(data_dir: Path) -> bool:
    """data 目录下是否存在 jsonl 数据文件。"""
    if not data_dir.is_dir():
        return False
    return any(p.is_file() and p.suffix == ".jsonl" for p in data_dir.rglob("*.jsonl"))


async def run_if_needed() -> bool:
    """API 启动调用：环境变量 AUTO_MIGRATE_JSONL=1 且存在 jsonl 数据且未迁移过（marker 不存在）时执行。

    首次迁移完成后写入 marker 文件，之后启动不再重复迁移；需重迁可删除 marker 或直接跑 CLI。
    """
    if os.environ.get("AUTO_MIGRATE_JSONL", "") != "1":
        return False
    data_dir = project_root / "data"
    db_path = Path(sqlite_db_config["db_path"])
    marker = db_path.parent / MARKER_NAME
    if marker.exists() or not has_jsonl_data(data_dir):
        return False
    stats = await run_migration(data_dir, db_path)
    marker.write_text(datetime.now().isoformat(), encoding="utf-8")
    print(f"[migrate] 历史 jsonl 数据已迁移，marker 已写入 {marker}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="把历史 jsonl 数据迁移到 SQLite（upsert 去重，幂等）")
    parser.add_argument("--data-dir", type=Path, default=project_root / "data",
                        help="jsonl 数据目录（默认: 项目根/data）")
    parser.add_argument("--db-path", type=Path, default=Path(sqlite_db_config["db_path"]),
                        help="SQLite 数据库文件（默认: 项目根/database/sqlite_tables.db）")
    args = parser.parse_args()

    asyncio.run(run_migration(args.data_dir, args.db_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

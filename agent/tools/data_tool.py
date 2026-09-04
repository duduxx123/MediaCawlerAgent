# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/tools/data_tool.py
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
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""
2 个数据读取工具：读取已抓取的内容数据 / 列出数据表统计。
只读本地 SQLite 数据库（database/sqlite_tables.db），不发起任何网络请求。
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ..services.crawler_runner import (
    SQLITE_TABLE_MAP,
    extract_compact_record,
    normalize_platform,
)

MAX_SCAN_LINES = 20_000  # 单表最大扫描行数，防超大表卡死
MAX_LIST_FILES = 30


def _db_path() -> str:
    """SQLite 数据库路径（与 config/db_config.py 同源；测试可 monkeypatch sqlite_db_config）。"""
    from config.db_config import sqlite_db_config

    return sqlite_db_config["db_path"]


class ReadCrawledDataArgs(BaseModel):
    """读取已抓取数据的参数"""

    platform: str = Field(description="目标平台，可选值: 抖音、小红书、快手、B站、微博、贴吧、知乎，也接受英文名或平台缩写")
    crawler_type: str = Field(default="search", description="抓取模式: search(关键词搜索) / detail(详情) / creator(创作者)")
    limit: int = Field(default=10, ge=1, le=50, description="最多返回条数")
    keyword_filter: str = Field(default="", description="按标题/描述过滤的关键词，空字符串表示不过滤")


class ListCrawledFilesArgs(BaseModel):
    """列出数据文件的参数"""

    platform: str = Field(default="", description="目标平台（抖音、小红书、快手、B站、微博、贴吧、知乎），空字符串表示列出全部平台")


def _read_latest_contents_db(platform_cli_key: str, limit: int) -> List[Dict[str, Any]]:
    """读取平台内容表最新记录（按 add_ts 倒序）；库/表不存在返回空列表。"""
    tables = SQLITE_TABLE_MAP.get(platform_cli_key)
    if tables is None:
        return []
    contents_table = tables[0]
    try:
        with sqlite3.connect(_db_path()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM {contents_table} ORDER BY add_ts DESC LIMIT {limit}"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


@tool(args_schema=ReadCrawledDataArgs)
async def read_crawled_data(
    platform: str,
    crawler_type: str = "search",
    limit: int = 10,
    keyword_filter: str = "",
) -> str:
    """读取已抓取保存的内容数据（本地 SQLite 数据库），返回紧凑摘要供分析。
不会发起新的抓取，仅读取数据库中的已有数据。数据库模式不再区分抓取模式（search/detail/creator 同表存储）。"""
    try:
        platform_cli_key = normalize_platform(platform)
    except ValueError:
        return json.dumps(
            {"ok": False, "message": "平台参数无效，可选值: 抖音、小红书、快手、B站、微博、贴吧、知乎，也接受英文名或平台缩写"},
            ensure_ascii=False,
        )

    raw_rows = _read_latest_contents_db(platform_cli_key, MAX_SCAN_LINES)
    if not raw_rows:
        return json.dumps(
            {
                "ok": False,
                "message": "本地数据库中没有该平台的内容数据。可先调用 crawl_by_keywords 抓取内容后再读取。",
            },
            ensure_ascii=False,
        )

    records: List[Dict[str, Any]] = []
    filter_lower = keyword_filter.strip().lower()
    # 按 add_ts 倒序即最新记录在前，配合 limit 返回最近抓取的内容
    for row in raw_rows:
        if filter_lower:
            haystack = (
                f"{row.get('title', '')} {row.get('desc', '')} "
                f"{row.get('nickname', '')} {row.get('user_nickname', '')}"
            ).lower()
            if filter_lower not in haystack:
                continue
        records.append(extract_compact_record(row))
        if len(records) >= limit:
            break

    table = SQLITE_TABLE_MAP[platform_cli_key][0]
    if not records:
        return json.dumps(
            {"ok": True, "table": table, "total": 0,
             "message": "数据库中没有匹配的记录" + (f"（过滤词: {keyword_filter}）" if keyword_filter else "")},
            ensure_ascii=False,
        )
    return json.dumps(
        {"ok": True, "table": table, "total": len(records), "records": records},
        ensure_ascii=False,
    )


@tool(args_schema=ListCrawledFilesArgs)
async def list_crawled_files(platform: str = "") -> str:
    """列出本地 SQLite 数据库中各平台的数据表（表名、记录数、最近写入时间），按记录数倒序。
platform 为空则列出全部平台。不会发起新的抓取。"""
    try:
        if platform:
            cli_keys = [normalize_platform(platform)]
        else:
            cli_keys = list(SQLITE_TABLE_MAP.keys())
    except ValueError:
        return json.dumps(
            {"ok": False, "message": "平台参数无效，可选值: 抖音、小红书、快手、B站、微博、贴吧、知乎，也接受英文名或平台缩写"},
            ensure_ascii=False,
        )

    files: List[Dict[str, Any]] = []
    db_path = _db_path()
    for cli_key in cli_keys:
        tables = SQLITE_TABLE_MAP.get(cli_key)
        if tables is None:
            continue
        for table in tables:
            try:
                with sqlite3.connect(str(db_path)) as conn:
                    cur = conn.execute(f"SELECT COUNT(*), MAX(add_ts) FROM {table}")
                    count, max_ts = cur.fetchone()
            except sqlite3.Error:
                continue  # 表尚未创建：跳过
            if not count:
                continue  # 空表不列出，减少 LLM 噪音
            files.append({
                "path": f"sqlite:{table}",
                "size": None,
                "records": int(count or 0),
                "modified": datetime.fromtimestamp(max_ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if max_ts else "",
            })

    files.sort(key=lambda f: (f["records"] or 0), reverse=True)
    files = files[:MAX_LIST_FILES]
    if not files:
        return json.dumps(
            {"ok": True, "total": 0, "files": [], "message": "本地数据库暂无数据，可先调用 crawl_by_keywords 抓取。"},
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, "total": len(files), "files": files}, ensure_ascii=False)

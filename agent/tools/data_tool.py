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
2 个数据读取工具：读取已抓取的内容数据 / 列出数据文件。
只读本地 data 目录，不发起任何网络请求。
"""

import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ..services.crawler_runner import (
    DATA_DIR,
    PLATFORM_DATA_DIR,
    _count_records,
    extract_compact_record,
    normalize_platform,
)

MAX_SCAN_LINES = 20_000  # 单文件最大扫描行数，防超大文件卡死
MAX_LIST_FILES = 30


class ReadCrawledDataArgs(BaseModel):
    """读取已抓取数据的参数"""

    platform: str = Field(description="目标平台，可选值: douyin(抖音) / xhs(小红书) / bilibili(B站)，也接受缩写 dy/bili")
    crawler_type: str = Field(default="search", description="抓取模式: search(关键词搜索) / detail(详情) / creator(创作者)")
    limit: int = Field(default=10, ge=1, le=50, description="最多返回条数")
    keyword_filter: str = Field(default="", description="按标题/描述过滤的关键词，空字符串表示不过滤")


class ListCrawledFilesArgs(BaseModel):
    """列出数据文件的参数"""

    platform: str = Field(default="", description="目标平台（douyin/xhs/bilibili，也接受 dy/bili），空字符串表示列出全部平台")


def _latest_contents_file(platform: str, crawler_type: str) -> Optional[Path]:
    """找到指定平台/模式最新的 contents jsonl 文件。"""
    try:
        dir_name = PLATFORM_DATA_DIR[normalize_platform(platform)]
    except ValueError:
        return None
    base_dir = DATA_DIR / dir_name / "jsonl"
    if not base_dir.is_dir():
        return None
    candidates = list(base_dir.glob(f"{crawler_type}_contents_*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@tool(args_schema=ReadCrawledDataArgs)
async def read_crawled_data(
    platform: str,
    crawler_type: str = "search",
    limit: int = 10,
    keyword_filter: str = "",
) -> str:
    """读取已抓取保存的内容数据（jsonl 格式），返回紧凑摘要供分析。
不会发起新的抓取，仅读取本地 data 目录中已有的数据文件。"""
    file_path = _latest_contents_file(platform, crawler_type)
    if file_path is None:
        return json.dumps(
            {
                "ok": False,
                "message": "未找到已抓取的数据文件。可先调用 crawl_by_keywords 抓取内容后再读取。",
            },
            ensure_ascii=False,
        )

    records: List[Dict[str, Any]] = []
    filter_lower = keyword_filter.strip().lower()
    try:
        # 数据文件按日期命名、同一天多次抓取会 append，故从文件尾部读最新记录（倒序）
        buf: deque = deque(maxlen=MAX_SCAN_LINES)
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                buf.append(line)
        for line in reversed(buf):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if filter_lower:
                haystack = f"{record.get('title', '')} {record.get('desc', '')} {record.get('nickname', '')}".lower()
                if filter_lower not in haystack:
                    continue
            records.append(extract_compact_record(record))
            if len(records) >= limit:
                break
    except OSError as e:
        return json.dumps({"ok": False, "message": f"读取数据文件失败: {e}"}, ensure_ascii=False)

    if not records:
        return json.dumps(
            {"ok": True, "file": str(file_path.relative_to(DATA_DIR)), "total": 0,
             "message": "文件中没有匹配的记录" + (f"（过滤词: {keyword_filter}）" if keyword_filter else "")},
            ensure_ascii=False,
        )
    return json.dumps(
        {"ok": True, "file": str(file_path.relative_to(DATA_DIR)), "total": len(records), "records": records},
        ensure_ascii=False,
    )


@tool(args_schema=ListCrawledFilesArgs)
async def list_crawled_files(platform: str = "") -> str:
    """列出 data 目录下已抓取的数据文件（路径、大小、记录数、修改时间），按修改时间倒序。
platform 为空则列出全部平台的数据文件。不会发起新的抓取。"""
    files: List[Dict[str, Any]] = []
    try:
        if platform:
            dir_names = [PLATFORM_DATA_DIR[normalize_platform(platform)]]
        else:
            dir_names = sorted(set(PLATFORM_DATA_DIR.values()))
    except ValueError:
        return json.dumps(
            {"ok": False, "message": "平台参数无效，可选值: douyin(抖音)/xhs(小红书)/bilibili(B站)，也接受缩写 dy/bili"},
            ensure_ascii=False,
        )

    for dir_name in dir_names:
        base_dir = DATA_DIR / dir_name
        if not base_dir.is_dir():
            continue
        for path in base_dir.rglob("*"):
            if not path.is_file() or path.suffix not in (".jsonl", ".json", ".csv"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({
                "path": str(path.relative_to(DATA_DIR)),
                "size": stat.st_size,
                "records": _count_records(path),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })

    files.sort(key=lambda f: f["modified"], reverse=True)
    files = files[:MAX_LIST_FILES]
    if not files:
        return json.dumps(
            {"ok": True, "total": 0, "files": [], "message": "data 目录下暂无数据文件，可先调用 crawl_by_keywords 抓取。"},
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, "total": len(files), "files": files}, ensure_ascii=False)

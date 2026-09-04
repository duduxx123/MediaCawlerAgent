# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/dual_write_store.py
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
文件存储 + SQLite 镜像双写。

主存储（jsonl/json/csv 文件）保持爬虫原有写入路径不变，镜像存储（各平台 XxxSqliteStoreImplement）
同步写入 database/sqlite_tables.db，供 /leads 展示页查询/删除评论与 agent 数据工具读取。
镜像写入失败只记日志，绝不影响主写入（jsonl 文件是事实源）。
"""
from typing import Dict, Type

import config
from base.base_crawler import AbstractStore
from tools import utils


# 这些 SAVE_DATA_OPTION 值本身已有结构化存储，不再做文件镜像
_NON_MIRROR_OPTIONS = ("sqlite", "db", "mysql", "postgres", "mongodb", "excel")

# 参与镜像的文件保存模式（未知选项交给工厂原有逻辑报错，不做隐式包装）
_FILE_OPTIONS = ("jsonl", "json", "csv")


class DualWriteStore(AbstractStore):
    """主存储（文件）+ SQLite 镜像。镜像异常只记日志，绝不影响主写入。"""

    def __init__(self, primary: AbstractStore, mirror: AbstractStore):
        self._primary = primary
        self._mirror = mirror

    async def _mirrored(self, method_name: str, item: Dict):
        # contextvar 让镜像 store 内部的 get_session() 拿到 sqlite 引擎
        # （此时 config.SAVE_DATA_OPTION 仍是 jsonl 等文件模式）
        from database.db_session import _session_db_type_override

        token = _session_db_type_override.set("sqlite")
        try:
            await getattr(self._mirror, method_name)(item)
        except Exception as e:
            utils.logger.error(f"[DualWriteStore] sqlite 镜像 {method_name} 失败（不影响主存储）: {e}")
        finally:
            _session_db_type_override.reset(token)

    async def store_content(self, content_item: Dict):
        await self._primary.store_content(content_item)
        await self._mirrored("store_content", content_item)

    async def store_comment(self, comment_item: Dict):
        await self._primary.store_comment(comment_item)
        await self._mirrored("store_comment", comment_item)

    async def store_creator(self, creator_item: Dict):
        await self._primary.store_creator(creator_item)
        await self._mirrored("store_creator", creator_item)


def maybe_dual_write(primary: AbstractStore, sqlite_store_class: Type[AbstractStore]) -> AbstractStore:
    """7 个平台工厂共享的包装入口。

    镜像开启且当前为文件保存模式时返回 DualWriteStore（主存储 + SQLite 镜像），
    否则原样返回 primary；镜像构造失败回退仅文件写入。
    """
    if not config.ENABLE_SQLITE_MIRROR:
        return primary
    if config.SAVE_DATA_OPTION in _NON_MIRROR_OPTIONS:
        return primary
    if config.SAVE_DATA_OPTION not in _FILE_OPTIONS:
        return primary
    try:
        return DualWriteStore(primary, sqlite_store_class())
    except Exception as e:
        utils.logger.error(f"[maybe_dual_write] 构造 SQLite 镜像失败，回退仅文件写入: {e}")
        return primary

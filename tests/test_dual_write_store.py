# -*- coding: utf-8 -*-
"""
镜像双写测试：7 平台工厂包装行为、jsonl+SQLite 双写落库、镜像失败不影响主写入、
非文件模式/镜像关闭时不包装。
"""

import asyncio
import json
from pathlib import Path
from typing import Dict

import pytest

import config
from config import db_config
from database import db_session

from store.dual_write_store import DualWriteStore, maybe_dual_write

# (工厂, jsonl 实现类, sqlite 实现类)
ALL_FACTORIES = None


def _factories():
    global ALL_FACTORIES
    if ALL_FACTORIES is None:
        from store.xhs import XhsStoreFactory
        from store.xhs._store_impl import XhsJsonlStoreImplement, XhsSqliteStoreImplement
        from store.douyin import DouyinStoreFactory
        from store.douyin._store_impl import DouyinJsonlStoreImplement, DouyinSqliteStoreImplement
        from store.bilibili import BiliStoreFactory
        from store.bilibili._store_impl import BiliJsonlStoreImplement, BiliSqliteStoreImplement
        from store.kuaishou import KuaishouStoreFactory
        from store.kuaishou._store_impl import KuaishouJsonlStoreImplement, KuaishouSqliteStoreImplement
        from store.weibo import WeibostoreFactory
        from store.weibo._store_impl import WeiboJsonlStoreImplement, WeiboSqliteStoreImplement
        from store.tieba import TieBaStoreFactory
        from store.tieba._store_impl import TieBaJsonlStoreImplement, TieBaSqliteStoreImplement
        from store.zhihu import ZhihuStoreFactory
        from store.zhihu._store_impl import ZhihuJsonlStoreImplement, ZhihuSqliteStoreImplement

        ALL_FACTORIES = [
            (XhsStoreFactory, XhsJsonlStoreImplement, XhsSqliteStoreImplement),
            (DouyinStoreFactory, DouyinJsonlStoreImplement, DouyinSqliteStoreImplement),
            (BiliStoreFactory, BiliJsonlStoreImplement, BiliSqliteStoreImplement),
            (KuaishouStoreFactory, KuaishouJsonlStoreImplement, KuaishouSqliteStoreImplement),
            (WeibostoreFactory, WeiboJsonlStoreImplement, WeiboSqliteStoreImplement),
            (TieBaStoreFactory, TieBaJsonlStoreImplement, TieBaSqliteStoreImplement),
            (ZhihuStoreFactory, ZhihuJsonlStoreImplement, ZhihuSqliteStoreImplement),
        ]
    return ALL_FACTORIES


@pytest.fixture
def mirror_env(tmp_path, monkeypatch):
    """开启镜像 + jsonl 模式 + 独立数据目录与 sqlite 库 + 建表。"""
    monkeypatch.setattr(config, "ENABLE_SQLITE_MIRROR", True)
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path / "data"))
    db_path = tmp_path / "test_mirror.db"
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(db_path))
    db_session._engines.clear()
    asyncio.run(db_session.create_tables("sqlite"))
    yield tmp_path, db_path
    db_session._engines.clear()


@pytest.mark.parametrize("factory,jsonl_cls,sqlite_cls", _factories())
def test_factory_returns_dual_write(mirror_env, factory, jsonl_cls, sqlite_cls):
    store = factory.create_store()
    assert isinstance(store, DualWriteStore)
    assert isinstance(store._primary, jsonl_cls)
    assert isinstance(store._mirror, sqlite_cls)


def test_dual_write_content_and_comment(mirror_env, sample_xhs_note, sample_xhs_comment):
    tmp_path, db_path = mirror_env
    from store.xhs import XhsStoreFactory
    from database.db_session import get_session

    store = XhsStoreFactory.create_store()
    asyncio.run(store.store_content(sample_xhs_note))
    asyncio.run(store.store_comment(sample_xhs_comment))

    # jsonl 文件侧：data/xhs/jsonl/ 下出现 contents/comments 文件且含记录
    # （文件名前缀来自 crawler_type_var，测试环境默认空串，故按中缀匹配）
    jsonl_dir = tmp_path / "data" / "xhs" / "jsonl"
    contents_files = list(jsonl_dir.glob("*contents*.jsonl"))
    comments_files = list(jsonl_dir.glob("*comments*.jsonl"))
    assert len(contents_files) == 1
    assert len(comments_files) == 1
    assert sample_xhs_note["note_id"] in contents_files[0].read_text(encoding="utf-8")
    assert sample_xhs_comment["comment_id"] in comments_files[0].read_text(encoding="utf-8")

    # SQLite 侧：xhs_note / xhs_note_comment 各 1 行
    async def _verify():
        async with get_session(db_type="sqlite") as session:
            from sqlalchemy import select
            from database import models
            notes = (await session.execute(select(models.XhsNote))).scalars().all()
            comments = (await session.execute(select(models.XhsNoteComment))).scalars().all()
            assert len(notes) == 1
            assert notes[0].note_id == "test_note_123"
            assert notes[0].red_id == "public_note_123"
            assert len(comments) == 1
            assert comments[0].comment_id == "comment_123"
            assert comments[0].red_id == "public_comment_456"

    asyncio.run(_verify())


class _FakePrimary:
    def __init__(self):
        self.received: Dict[str, list] = {"content": [], "comment": [], "creator": []}

    async def store_content(self, item):
        self.received["content"].append(item)

    async def store_comment(self, item):
        self.received["comment"].append(item)

    async def store_creator(self, item):
        self.received["creator"].append(item)


class _FailingMirror:
    async def store_content(self, item):
        raise RuntimeError("镜像坏了")

    async def store_comment(self, item):
        raise RuntimeError("镜像坏了")

    async def store_creator(self, item):
        raise RuntimeError("镜像坏了")


def test_mirror_failure_does_not_affect_primary():
    primary = _FakePrimary()
    store = DualWriteStore(primary, _FailingMirror())

    # 不抛异常，主存储收到数据
    asyncio.run(store.store_content({"note_id": "n1"}))
    asyncio.run(store.store_comment({"comment_id": "c1"}))
    asyncio.run(store.store_creator({"user_id": "u1"}))
    assert primary.received == {
        "content": [{"note_id": "n1"}],
        "comment": [{"comment_id": "c1"}],
        "creator": [{"user_id": "u1"}],
    }


def test_no_wrap_when_save_option_is_sqlite(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_SQLITE_MIRROR", True)
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "sqlite")
    from store.xhs import XhsStoreFactory
    from store.xhs._store_impl import XhsSqliteStoreImplement

    assert isinstance(XhsStoreFactory.create_store(), XhsSqliteStoreImplement)


def test_no_wrap_when_mirror_off(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_SQLITE_MIRROR", False)
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")
    from store.xhs import XhsStoreFactory
    from store.xhs._store_impl import XhsJsonlStoreImplement

    assert isinstance(XhsStoreFactory.create_store(), XhsJsonlStoreImplement)


def test_maybe_dual_write_falls_back_when_mirror_construction_fails(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_SQLITE_MIRROR", True)
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "jsonl")

    class _BrokenSqlite:
        def __init__(self):
            raise RuntimeError("构造失败")

    primary = _FakePrimary()
    result = maybe_dual_write(primary, _BrokenSqlite)
    assert result is primary  # 回退仅文件写入

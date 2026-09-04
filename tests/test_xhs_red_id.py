# -*- coding: utf-8 -*-
"""小红书公开账号 red_id：提取、缓存、存储和 SQLite 增量迁移。"""

import asyncio
import sqlite3

import pytest

import config
from media_platform.xhs.core import XiaoHongShuCrawler


USER_ID = "69d30f6a0000000032037b74"


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get_creator_info(self, user_id, xsec_token="", xsec_source=""):
        self.calls.append((user_id, xsec_token, xsec_source))
        return {"basicInfo": {"userId": user_id, "redId": "5460254944"}}


@pytest.fixture
def red_id_config(monkeypatch):
    monkeypatch.setattr(config, "XHS_SAVE_ORIGINAL_USER_INFO", True)
    monkeypatch.setattr(config, "XHS_FETCH_PUBLIC_RED_ID", True)
    monkeypatch.setattr(config, "XHS_PUBLIC_RED_ID_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(config, "XHS_PUBLIC_RED_ID_FETCH_INTERVAL", 0)
    monkeypatch.setattr(config, "XHS_PUBLIC_RED_ID_MAX_USERS_PER_RUN", 100)


@pytest.mark.asyncio
async def test_direct_red_id_needs_no_profile_request(red_id_config):
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = FakeClient()
    info = {"user_id": USER_ID, "red_id": "direct-id"}
    await crawler._enrich_user_info(info)
    assert info["red_id"] == "direct-id"
    assert crawler.xhs_client.calls == []


@pytest.mark.asyncio
async def test_profile_red_id_is_cached_by_internal_user_id(red_id_config):
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = FakeClient()
    first = {"user_id": USER_ID}
    second = {"user_id": USER_ID}
    await asyncio.gather(crawler._enrich_user_info(first), crawler._enrich_user_info(second))
    assert first["red_id"] == second["red_id"] == "5460254944"
    assert len(crawler.xhs_client.calls) == 1


@pytest.mark.asyncio
async def test_profile_failure_does_not_break_comment_storage(red_id_config, monkeypatch):
    crawler = XiaoHongShuCrawler()

    class BrokenClient:
        async def get_creator_info(self, **kwargs):
            raise RuntimeError("rate limited")

    crawler.xhs_client = BrokenClient()
    captured = []

    async def fake_store(note_id, comments):
        captured.extend(comments)

    monkeypatch.setattr("media_platform.xhs.core.xhs_store.batch_update_xhs_note_comments", fake_store)
    comments = [{"id": "c1", "user_info": {"user_id": USER_ID}}]
    await crawler._store_comments_with_red_id("n1", comments)
    assert captured == comments
    assert "red_id" not in comments[0]["user_info"]


@pytest.mark.asyncio
async def test_privacy_mode_does_not_fetch_public_id(red_id_config, monkeypatch):
    monkeypatch.setattr(config, "XHS_SAVE_ORIGINAL_USER_INFO", False)
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = FakeClient()
    info = {"user_id": USER_ID}
    await crawler._enrich_user_info(info)
    assert "red_id" not in info
    assert crawler.xhs_client.calls == []


def test_store_extracts_red_id_for_note_and_comment(red_id_config, monkeypatch):
    import store.xhs as xhs_store

    captured = {"note": None, "comment": None}

    class FakeStore:
        async def store_content(self, item):
            captured["note"] = item

        async def store_comment(self, item):
            captured["comment"] = item

    monkeypatch.setattr(xhs_store.XhsStoreFactory, "create_store", staticmethod(lambda: FakeStore()))
    asyncio.run(xhs_store.update_xhs_note({
        "note_id": "n1", "type": "normal", "desc": "d", "user": {
            "user_id": USER_ID, "nickname": "喃喃", "red_id": "5460254944",
        }, "interact_info": {}, "image_list": [], "tag_list": [],
    }))
    asyncio.run(xhs_store.update_xhs_note_comment("n1", {
        "id": "c1", "content": "hi", "user_info": {
            "user_id": USER_ID, "nickname": "喃喃", "redId": "5460254944",
        },
    }))
    assert captured["note"]["creator_hash"] == USER_ID
    assert captured["note"]["red_id"] == "5460254944"
    assert captured["comment"]["creator_hash"] == USER_ID
    assert captured["comment"]["red_id"] == "5460254944"


def test_existing_sqlite_gets_red_id_columns_and_indexes(tmp_path, monkeypatch):
    from config import db_config
    from database import db_session

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE xhs_note (id INTEGER PRIMARY KEY, creator_hash TEXT)")
        conn.execute("CREATE TABLE xhs_note_comment (id INTEGER PRIMARY KEY, creator_hash TEXT)")
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(db_path))
    db_session._engines.clear()
    asyncio.run(db_session.create_tables("sqlite"))
    with sqlite3.connect(str(db_path)) as conn:
        for table in ("xhs_note", "xhs_note_comment"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            indexes = {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
            assert "red_id" in columns
            assert f"ix_{table}_red_id" in indexes
    db_session._engines.clear()

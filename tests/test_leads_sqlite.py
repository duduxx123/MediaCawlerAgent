# -*- coding: utf-8 -*-
"""
SQLite 存储下的 leads 接口测试：读接口（响应形状）、评论批量删除（级联/404/400/409 守卫）、
jsonl→SQLite 迁移、crawler_runner 默认 save_option。

隔离方式：monkeypatch sqlite_db_config["db_path"] 指向 tmp_path（db_session 与 db_config
共享同一 dict 对象），并清空引擎缓存；TestClient 以 with 进入触发 lifespan 建表。
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# conftest.py 已把项目根加入 sys.path
import config
from api.main import app

EXPECTED_LEAD_KEYS = {
    "platform", "platform_label", "keyword", "video_id", "video_url", "video_title",
    "comment_id", "commenter_name", "commenter_id", "commenter_public_id",
    "commenter_internal_id", "commenter_sec_uid", "comment",
    "like_count", "reply_count", "comment_time", "fetch_time", "pictures",
}


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    """独立 sqlite 库 + 建表 + 清引擎缓存。

    建表在 fixture 内完成（TestClient 的 lifespan 只在 with 块内触发，
    而 seed 数据需要在进入 with 之前执行）。
    """
    from config import db_config
    from database import db_session

    db_path = tmp_path / "test_leads.db"
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(db_path))
    monkeypatch.setattr(config, "SAVE_DATA_OPTION", "sqlite")
    db_session._engines.clear()
    asyncio.run(db_session.create_tables("sqlite"))
    yield db_path
    db_session._engines.clear()


def _seed_douyin():
    """插入 douyin 内容 1 条 + 评论 4 条（含父/子/孙链）供断言。"""
    from database import models
    from database.db_session import get_session

    async def _run():
        async with get_session() as session:
            session.add(models.DouyinAweme(
                aweme_id="aw1", title="测试视频", desc="副标题",
                aweme_url="https://example.com/aw1", nickname="作者A",
                source_keyword="编程副业", liked_count="10",
                create_time=2000, last_modify_ts=1000,
            ))
            session.add_all([
                models.DouyinAwemeComment(comment_id="c1", aweme_id="aw1", content="主评论",
                                          parent_comment_id="0", like_count="1",
                                          create_time=4000, last_modify_ts=3000),
                models.DouyinAwemeComment(comment_id="c2", aweme_id="aw1", content="回复1",
                                          parent_comment_id="c1", like_count="0"),
                models.DouyinAwemeComment(comment_id="c3", aweme_id="aw1", content="回复2",
                                          parent_comment_id="c2", like_count="0"),
                models.DouyinAwemeComment(comment_id="c4", aweme_id="aw1", content="无关评论",
                                          parent_comment_id="0", like_count="0"),
            ])

    asyncio.run(_run())


def _db_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_comments_endpoint_reads_sqlite(sqlite_env):
    _seed_douyin()
    with TestClient(app) as client:
        resp = client.get("/api/leads/comments")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    lead = next(l for l in data["leads"] if l["comment_id"] == "c1")
    # 响应形状与历史 jsonl 版本一致（16 键），join 值来自内容表
    assert set(lead.keys()) == EXPECTED_LEAD_KEYS
    assert lead["keyword"] == "编程副业"
    assert lead["video_title"] == "测试视频"
    assert lead["video_url"] == "https://example.com/aw1"
    assert lead["commenter_name"] == ""
    assert lead["commenter_id"] == ""  # 教学版匿名化：不落库，返回空串保持字段存在


def test_comments_endpoint_maps_xhs_public_and_internal_ids(sqlite_env):
    """公开小红书号用于展示，24 位内部 ID 单独保留给私信定位。"""
    from database import models
    from database.db_session import get_session

    async def _run():
        async with get_session() as session:
            session.add(models.XhsNote(
                note_id="xhs-note-1", title="小红书笔记", source_keyword="Python",
            ))
            session.add(models.XhsNoteComment(
                comment_id="xhs-comment-1", note_id="xhs-note-1", content="不错",
                creator_hash="69d30f6a0000000032037b74", red_id="5460254944", nickname="用户A",
            ))

    asyncio.run(_run())
    with TestClient(app) as client:
        resp = client.get("/api/leads/comments")
    assert resp.status_code == 200
    lead = next(item for item in resp.json()["leads"] if item["comment_id"] == "xhs-comment-1")
    assert lead["commenter_id"] == "5460254944"
    assert lead["commenter_public_id"] == "5460254944"
    assert lead["commenter_internal_id"] == "69d30f6a0000000032037b74"
    assert lead["commenter_sec_uid"] == ""


def test_contents_endpoint_reads_sqlite(sqlite_env):
    _seed_douyin()
    with TestClient(app) as client:
        resp = client.get("/api/leads/contents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    content = data["contents"][0]
    assert content["id"] == "aw1"
    assert content["title"] == "测试视频"
    assert content["platform"] == "douyin"
    assert content["platform_label"] == "抖音"


def test_delete_comments_with_cascade(sqlite_env):
    _seed_douyin()
    with TestClient(app) as client:
        resp = client.post("/api/leads/comments/delete", json={
            "items": [{"platform": "douyin", "comment_id": "c1"}],
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == 1
    assert data["cascaded"] == 2  # c2（子）+ c3（孙）级联删除
    assert data["not_found"] == []
    # 库中只剩无关评论 c4
    assert _db_count(sqlite_env, "douyin_aweme_comment") == 1
    with sqlite3.connect(str(sqlite_env)) as conn:
        remaining = [r[0] for r in conn.execute("SELECT comment_id FROM douyin_aweme_comment")]
    assert remaining == ["c4"]


def test_delete_comments_multi_platform_and_not_found(sqlite_env):
    _seed_douyin()
    with TestClient(app) as client:
        resp = client.post("/api/leads/comments/delete", json={
            "items": [
                {"platform": "douyin", "comment_id": "c4"},
                {"platform": "douyin", "comment_id": "not-exist"},
            ],
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == 1
    assert data["cascaded"] == 0
    assert data["not_found"] == ["not-exist"]


def test_delete_comments_not_found_404(sqlite_env):
    _seed_douyin()
    before = _db_count(sqlite_env, "douyin_aweme_comment")
    with TestClient(app) as client:
        resp = client.post("/api/leads/comments/delete", json={
            "items": [{"platform": "douyin", "comment_id": "ghost"}],
        })
    assert resp.status_code == 404
    assert _db_count(sqlite_env, "douyin_aweme_comment") == before


def test_delete_comments_invalid_400(sqlite_env):
    with TestClient(app) as client:
        # 未知平台
        resp = client.post("/api/leads/comments/delete", json={
            "items": [{"platform": "../douyin", "comment_id": "c1"}],
        })
        assert resp.status_code == 400
        # 顶层哨兵值 0
        resp = client.post("/api/leads/comments/delete", json={
            "items": [{"platform": "douyin", "comment_id": "0"}],
        })
        assert resp.status_code == 400
        # 空列表（Pydantic 校验）
        resp = client.post("/api/leads/comments/delete", json={"items": []})
        assert resp.status_code == 422


def test_delete_comments_guard_409(sqlite_env, monkeypatch):
    # 注意：api/services/__init__.py 暴露的是单例实例，直接导入模块内的同名实例
    from api.services.crawler_manager import crawler_manager

    _seed_douyin()
    monkeypatch.setattr(crawler_manager, "status", "running")
    with TestClient(app) as client:
        resp = client.post("/api/leads/comments/delete", json={
            "items": [{"platform": "douyin", "comment_id": "c1"}],
        })
    assert resp.status_code == 409
    monkeypatch.setattr(crawler_manager, "status", "idle")

    # agent 进程内爬取中（is_crawling 为 True）同样拒绝
    monkeypatch.setattr("api.routers.leads.is_crawling", lambda: True)
    with TestClient(app) as client:
        resp = client.post("/api/leads/comments/delete", json={
            "items": [{"platform": "douyin", "comment_id": "c1"}],
        })
    assert resp.status_code == 409


def test_migrate_jsonl_to_sqlite(tmp_path, monkeypatch):
    from config import db_config
    from database import db_session
    from tools.migrate_jsonl_to_sqlite import run_migration

    # 构造历史 jsonl：两个日期文件含同一条 comment_id（去重）+ 模型外字段（过滤）
    jsonl_dir = tmp_path / "data" / "douyin" / "jsonl"
    jsonl_dir.mkdir(parents=True)
    content = {
        "aweme_id": "aw_mig", "title": "迁移视频", "desc": "d", "aweme_url": "https://e.com/aw_mig",
        "nickname": "作者M", "source_keyword": "迁移", "douyin_id": "will-be-dropped",
    }
    comment = {"comment_id": "cm1", "aweme_id": "aw_mig", "content": "迁移评论",
               "parent_comment_id": "0", "nickname": "用户M", "douyin_id": "will-be-dropped"}
    (jsonl_dir / "search_contents_2026-08-01.jsonl").write_text(
        json.dumps(content, ensure_ascii=False) + "\n", encoding="utf-8")
    (jsonl_dir / "search_comments_2026-08-01.jsonl").write_text(
        json.dumps(comment, ensure_ascii=False) + "\n", encoding="utf-8")
    (jsonl_dir / "search_comments_2026-08-02.jsonl").write_text(
        json.dumps(comment, ensure_ascii=False) + "\n", encoding="utf-8")  # 重复日期的同一条

    db_path = tmp_path / "db" / "mig.db"
    db_path.parent.mkdir()
    monkeypatch.setitem(db_config.sqlite_db_config, "db_path", str(db_path))
    db_session._engines.clear()
    stats = asyncio.run(run_migration(tmp_path / "data", db_path))
    assert stats["contents"] == 1
    assert stats["comments"] == 2  # 两条评论行都导入
    assert _db_count(db_path, "douyin_aweme") == 1
    assert _db_count(db_path, "douyin_aweme_comment") == 1  # 同 comment_id 去重为 1 行

    # 重跑幂等：行数不变
    stats2 = asyncio.run(run_migration(tmp_path / "data", db_path))
    assert _db_count(db_path, "douyin_aweme_comment") == 1
    assert stats2["comments"] == 2
    db_session._engines.clear()


def test_build_command_default_save_option_jsonl():
    from agent.services.crawler_runner import build_command

    cmd = build_command(platform="douyin", crawler_type="search", keywords="测试")
    assert cmd is not None
    assert "--save_data_option" in cmd
    assert cmd[cmd.index("--save_data_option") + 1] == "jsonl"

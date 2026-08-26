# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_comment_tools.py
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

"""评论获客工具离线单测（不连 CDP、不发网络请求、不真实发评论）。"""

import json

import pytest

from agent.tools import ALL_TOOLS
from agent.tools import comment_tools
from agent.tools.comment_tools import fetch_comment_users, post_comment, reply_comment, send_dm_user

VIDEO_URL = "https://www.douyin.com/video/7525538910311632128"
SAMPLE_COMMENTS = [
    {
        "cid": "c1",
        "text": "哇我也是陕科大的，前段时间也有用过大佬的这个开源项目",
        "sec_uid": "sec_uid_1",
        "uid": "uid_1",
        "nickname": "奶服",
        "digg_count": 2,
        "reply_comment_total": 0,
        "create_time": 1700000000,
    },
    {
        "cid": "c2",
        "text": "多少钱？怎么买？",
        "sec_uid": "sec_uid_2",
        "uid": "uid_2",
        "nickname": "意向客户",
        "digg_count": 0,
        "reply_comment_total": 1,
        "create_time": 1700000001,
    },
]


class FakePage:
    def __init__(self, closed: bool = False) -> None:
        self._closed = closed
        self.dead = False  # is_closed=False 但连接已断（evaluate 抛错）

    def is_closed(self) -> bool:
        return self._closed

    async def evaluate(self, *args, **kwargs):
        if self._closed or self.dead:
            raise RuntimeError("Target closed")
        return True


class FakeBot:
    """替身 DouyinCommentBot：记录调用、可注入故障，不触网。"""

    def __init__(self):
        self.page = FakePage()
        self.reply_calls = []
        self.post_calls = []
        self.dm_calls = []
        self.fail_fetch = False
        self.fail_write = False
        self.fail_connection = False
        self.write_confirmed = True  # bot 自检结果：True=评论区确认出现

    async def setup(self):
        pass

    async def close(self):
        pass

    async def fetch_comments_by_api(self, video_url, max_count):
        if self.fail_fetch:
            raise RuntimeError("CDP 连接失败")
        return SAMPLE_COMMENTS

    async def open_video_page(self, video_url):
        if self.fail_connection:
            raise RuntimeError("Target closed")
        if self.fail_write:
            raise RuntimeError("页面打开失败")

    async def post_comment(self, text):
        if self.fail_write:
            raise RuntimeError("提交失败")
        self.post_calls.append(text)
        return self.write_confirmed

    async def reply_to_comment(self, sec_uid, text, nth=0):
        if self.fail_write:
            raise RuntimeError("提交失败")
        self.reply_calls.append((sec_uid, text, nth))
        return self.write_confirmed

    async def open_user_profile(self, sec_uid):
        if self.fail_write:
            raise RuntimeError("页面打开失败")
        self.dm_calls.append(("open_profile", sec_uid))
        return "张三"

    async def fill_dm_input(self, text):
        if self.fail_write:
            raise RuntimeError("填框失败")
        self.dm_calls.append(("fill", text))

    async def submit_dm(self):
        if self.fail_write:
            raise RuntimeError("提交失败")
        self.dm_calls.append(("submit", ""))

    async def _self_check_dm_sent(self, text):
        self.dm_calls.append(("self_check", text))


@pytest.fixture
def fake_bot(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(comment_tools, "_bot", bot)
    return bot


# ---------- fetch_comment_users ----------

class TestFetchCommentUsers:

    @pytest.mark.asyncio
    async def test_success(self, fake_bot):
        result = json.loads(await fetch_comment_users.ainvoke({"video_url": VIDEO_URL, "max_comments": 30}))
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["comments"][0]["sec_uid"] == "sec_uid_1"
        assert result["comments"][0]["nickname"] == "奶服"
        assert result["comments"][1]["text"] == "多少钱？怎么买？"
        assert result["comments"][1]["reply_comment_total"] == 1

    @pytest.mark.asyncio
    async def test_error_returns_ok_false(self, fake_bot):
        fake_bot.fail_fetch = True
        result = json.loads(await fetch_comment_users.ainvoke({"video_url": VIDEO_URL}))
        assert result["ok"] is False
        assert "CDP 连接失败" in result["message"]
        assert result.get("hint")

    @pytest.mark.asyncio
    async def test_error_resets_bot_for_reconnect(self, fake_bot):
        fake_bot.fail_fetch = True
        await fetch_comment_users.ainvoke({"video_url": VIDEO_URL})
        assert comment_tools._bot is None  # 出错后丢弃单例，下次调用自动重建重连


# ---------- post_comment ----------

class TestPostComment:

    @pytest.mark.asyncio
    async def test_success(self, fake_bot):
        result = json.loads(await post_comment.ainvoke({"video_url": VIDEO_URL, "content": "很棒"}))
        assert result["ok"] is True
        assert result["self_checked"] is True
        assert "自检通过" in result["message"]
        assert fake_bot.post_calls == ["很棒"]

    @pytest.mark.asyncio
    async def test_unconfirmed_self_check_still_ok(self, fake_bot):
        fake_bot.write_confirmed = False
        result = json.loads(await post_comment.ainvoke({"video_url": VIDEO_URL, "content": "很棒"}))
        assert result["ok"] is True  # 已提交（不可逆），不能报失败
        assert result["self_checked"] is False
        assert "未确认" in result["message"]

    @pytest.mark.asyncio
    async def test_error(self, fake_bot):
        fake_bot.fail_write = True
        result = json.loads(await post_comment.ainvoke({"video_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is False
        assert "页面打开失败" in result["message"]
        assert comment_tools._bot is fake_bot  # 页面级错误：连接保留，避免用户重点「允许」

    @pytest.mark.asyncio
    async def test_error_resets_bot_on_connection_error(self, fake_bot):
        fake_bot.fail_connection = True
        result = json.loads(await post_comment.ainvoke({"video_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is False
        assert comment_tools._bot is None  # 连接级错误：丢弃单例，下次自动重建重连


# ---------- reply_comment ----------

class TestReplyComment:

    @pytest.mark.asyncio
    async def test_success_with_comment_index(self, fake_bot):
        result = json.loads(await reply_comment.ainvoke({
            "video_url": VIDEO_URL,
            "sec_uid": "sec_uid_1",
            "content": "学弟你好！",
            "comment_index": 1,
        }))
        assert result["ok"] is True
        assert result["self_checked"] is True
        assert result["comment_index"] == 1
        sec_uid, text, nth = fake_bot.reply_calls[0]
        assert sec_uid == "sec_uid_1"
        assert text == "学弟你好！"
        assert nth == 1  # 列表第 1 条 = 该用户第 1 条评论

    @pytest.mark.asyncio
    async def test_no_index_targets_first_comment(self, fake_bot):
        await reply_comment.ainvoke({"video_url": VIDEO_URL, "sec_uid": "sec_uid_2", "content": "回复"})
        assert fake_bot.reply_calls[0][2] == 0  # nth=0：该用户第一条可见评论

    @pytest.mark.asyncio
    async def test_index_mismatch_after_hot_sort(self, fake_bot):
        # 序号 1 当前指向奶服(sec_uid_1)，但请求回复 sec_uid_2 → 热排序漂移，应报错且不写
        result = json.loads(await reply_comment.ainvoke({
            "video_url": VIDEO_URL, "sec_uid": "sec_uid_2", "content": "回复", "comment_index": 1,
        }))
        assert result["ok"] is False
        assert "已不是目标用户" in result["message"]
        assert fake_bot.reply_calls == []

    @pytest.mark.asyncio
    async def test_index_out_of_range(self, fake_bot):
        result = json.loads(await reply_comment.ainvoke({
            "video_url": VIDEO_URL, "sec_uid": "sec_uid_1", "content": "回复", "comment_index": 5,
        }))
        assert result["ok"] is False
        assert "超出" in result["message"]

    @pytest.mark.asyncio
    async def test_unconfirmed_self_check_still_ok(self, fake_bot):
        fake_bot.write_confirmed = False
        result = json.loads(await reply_comment.ainvoke({
            "video_url": VIDEO_URL, "sec_uid": "sec_uid_1", "content": "学弟你好！",
        }))
        assert result["ok"] is True
        assert result["self_checked"] is False
        assert "未确认" in result["message"]

    @pytest.mark.asyncio
    async def test_error(self, fake_bot):
        fake_bot.fail_write = True
        result = json.loads(await reply_comment.ainvoke({"video_url": VIDEO_URL, "sec_uid": "x", "content": "y"}))
        assert result["ok"] is False
        assert "找不到目标评论" in result.get("hint", "")
        assert comment_tools._bot is fake_bot  # 页面级错误：连接保留


# ---------- send_dm_user ----------

class TestSendDmUser:

    @pytest.mark.asyncio
    async def test_success(self, fake_bot):
        result = json.loads(await send_dm_user.ainvoke({"sec_uid": "sec_uid_2", "content": "您好，看到您感兴趣"}))
        assert result["ok"] is True
        assert "张三" in result["message"]
        assert fake_bot.dm_calls == [
            ("open_profile", "sec_uid_2"),
            ("fill", "您好，看到您感兴趣"),
            ("submit", ""),
            ("self_check", "您好，看到您感兴趣"),
        ]

    @pytest.mark.asyncio
    async def test_error_keeps_bot_for_non_connection_error(self, fake_bot):
        fake_bot.fail_write = True
        result = json.loads(await send_dm_user.ainvoke({"sec_uid": "x", "content": "y"}))
        assert result["ok"] is False
        assert "页面打开失败" in result["message"]
        assert comment_tools._bot is fake_bot  # 页面级错误：连接保留复用


# ---------- bot 单例复用/重建（CDP 连接生命周期） ----------

class TestBotReuse:

    @pytest.mark.asyncio
    async def test_reuse_healthy_bot(self, fake_bot, monkeypatch):
        # 健康 bot 复用：不应重建（重建会触发新的 CDP 连接 + 允许弹窗）
        def boom():
            raise AssertionError("健康 bot 不应重建")

        monkeypatch.setattr(comment_tools, "DouyinCommentBot", boom)
        result = json.loads(await fetch_comment_users.ainvoke({"video_url": VIDEO_URL}))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_rebuild_when_page_closed(self, fake_bot, monkeypatch):
        # 用户关掉了 bot 开的标签页：下次调用应自动重建而不是在死页面上失败
        fake_bot.page._closed = True
        created = []

        class FreshBot(FakeBot):
            pass

        monkeypatch.setattr(
            comment_tools, "DouyinCommentBot",
            lambda: created.append(FreshBot()) or created[-1],
        )
        result = json.loads(await fetch_comment_users.ainvoke({"video_url": VIDEO_URL}))
        assert result["ok"] is True
        assert len(created) == 1
        assert comment_tools._bot is created[0]

    @pytest.mark.asyncio
    async def test_rebuild_when_connection_dead(self, fake_bot, monkeypatch):
        # 页面未关但连接已断（Chrome 重启/连接被回收）：evaluate 健康检查发现并重建
        fake_bot.page.dead = True
        created = []

        class FreshBot(FakeBot):
            pass

        monkeypatch.setattr(
            comment_tools, "DouyinCommentBot",
            lambda: created.append(FreshBot()) or created[-1],
        )
        result = json.loads(await fetch_comment_users.ainvoke({"video_url": VIDEO_URL}))
        assert result["ok"] is True
        assert len(created) == 1
        assert comment_tools._bot is created[0]


# ---------- 注册 ----------

class TestRegistration:

    def test_tools_registered(self):
        names = {t.name for t in ALL_TOOLS}
        assert {"fetch_comment_users", "post_comment", "reply_comment"} <= names

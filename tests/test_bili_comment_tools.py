# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_bili_comment_tools.py
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

"""B站发评论工具离线单测（不连 CDP、不发网络请求、不真实发评论）。"""

import json

import pytest

from agent.tools import ALL_TOOLS
from agent.tools import bili_comment_tools
from agent.tools.bili_comment_tools import post_bilibili_comment
from media_platform.bilibili.comment_bot import (
    BilibiliCommentError,
    NotLoggedInError,
    parse_comment_target_url,
)

VIDEO_URL = "https://www.bilibili.com/video/BV1dwuKzmE26/?spm_id_from=333.1387"
DYNAMIC_URL = "https://www.bilibili.com/opus/123456789"

SUCCESS_RESULT = {
    "ok": True,
    "message": "评论已发布（自检可见）",
    "rpid": 1890123456789,
    "rpid_str": "1890123456789",
    "type": 1,
    "oid": 123,
    "oid_str": "123",
    "target_label": "BV1dwuKzmE26",
    "verified": True,
    "need_captcha": False,
}


class FakeBot:
    """替身 BilibiliCommentBot：记录调用、可注入故障，不触网。

    注意 fail_connection 只影响 post_comment（模拟连接级错误发生在业务调用中）；
    健康探针由 alive 单独控制——若探针也报连接错，_get_bot 会走重建分支，测试里
    必须同时 monkeypatch 构造器，否则会创建真 bot 连真实 Chrome。
    """

    def __init__(self):
        self.post_calls = []
        self.alive = True
        self.fail_connection = False
        self.fail_not_logged_in = False
        self.result = SUCCESS_RESULT
        self.error = None

    async def setup(self):
        pass

    async def close(self):
        pass

    async def check_alive(self):
        return self.alive

    async def post_comment(self, target, content, root=None, parent=None):
        self.post_calls.append((target, content, root, parent))
        if self.fail_connection:
            raise RuntimeError("Target closed")
        if self.fail_not_logged_in:
            raise NotLoggedInError("未检测到 B站登录态（cookie 需含 bili_jct/SESSDATA）")
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def fake_bot(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(bili_comment_tools, "_bot", bot)
    return bot


# ---------- 成功路径 ----------

class TestSuccess:

    @pytest.mark.asyncio
    async def test_video_bv_url_success(self, fake_bot):
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "视频很棒"}))
        assert result["ok"] is True
        assert result["rpid"] == 1890123456789
        assert result["rpid_str"] == "1890123456789"
        assert result["verified"] is True
        assert fake_bot.post_calls == [(VIDEO_URL, "视频很棒", None, None)]

    @pytest.mark.asyncio
    async def test_dynamic_url_success(self, fake_bot):
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": DYNAMIC_URL, "content": "动态不错"}))
        assert result["ok"] is True
        assert fake_bot.post_calls == [(DYNAMIC_URL, "动态不错", None, None)]

    @pytest.mark.asyncio
    async def test_reply_root_parent_passthrough(self, fake_bot):
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "回复一下", "root": 111, "parent": 222}))
        assert result["ok"] is True
        assert fake_bot.post_calls == [(VIDEO_URL, "回复一下", 111, 222)]


# ---------- 参数守卫 ----------

class TestParamGuard:

    @pytest.mark.asyncio
    async def test_parent_without_root_rejected(self, fake_bot):
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "x", "parent": 222}))
        assert result["ok"] is False
        assert "必须同时传 root" in result["message"]
        assert fake_bot.post_calls == []  # 未发网络


# ---------- 业务错误（保留 bot） ----------

class TestBusinessErrors:

    @pytest.mark.asyncio
    async def test_captcha_keeps_bot(self, fake_bot):
        fake_bot.error = BilibiliCommentError(
            "发布未成功（success_action=-352）：风控校验失败", need_captcha=True)
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is False
        assert "风控" in result["hint"]
        assert bili_comment_tools._bot is fake_bot  # 连接是好的，不重置

    @pytest.mark.asyncio
    async def test_biz_error_keeps_bot(self, fake_bot):
        fake_bot.error = BilibiliCommentError("发布失败：评论区已关闭", need_captcha=False)
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is False
        assert "评论区已关闭" in result["message"]
        assert bili_comment_tools._bot is fake_bot

    @pytest.mark.asyncio
    async def test_not_logged_in_keeps_bot(self, fake_bot):
        fake_bot.fail_not_logged_in = True
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is False
        assert "登录" in result["hint"]
        assert bili_comment_tools._bot is fake_bot


# ---------- 连接错误（重置 bot） ----------

class TestConnectionError:

    @pytest.mark.asyncio
    async def test_connection_error_resets_bot(self, fake_bot):
        fake_bot.fail_connection = True
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is False
        assert bili_comment_tools._bot is None  # 下次调用自动重建


# ---------- bot 单例复用/重建（CDP 连接生命周期） ----------

class TestBotReuse:

    @pytest.mark.asyncio
    async def test_reuse_healthy_bot(self, fake_bot, monkeypatch):
        # 健康 bot 复用：不应重建（重建会触发新的 CDP 连接 + 允许弹窗）
        def boom():
            raise AssertionError("健康 bot 不应重建")

        monkeypatch.setattr(bili_comment_tools, "BilibiliCommentBot", boom)
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_rebuild_when_connection_dead(self, fake_bot, monkeypatch):
        # CDP 连接已断（Chrome 重启/连接被回收）：check_alive 发现并重建
        fake_bot.alive = False
        created = []

        class FreshBot(FakeBot):
            pass

        monkeypatch.setattr(
            bili_comment_tools, "BilibiliCommentBot",
            lambda: created.append(FreshBot()) or created[-1],
        )
        result = json.loads(await post_bilibili_comment.ainvoke(
            {"target_url": VIDEO_URL, "content": "x"}))
        assert result["ok"] is True
        assert len(created) == 1
        assert bili_comment_tools._bot is created[0]


# ---------- 目标解析纯函数 ----------

class TestParseTargetUrl:

    def test_video_bv_url(self):
        t = parse_comment_target_url("https://www.bilibili.com/video/BV1dwuKzmE26/?spm=1")
        assert (t.kind, t.bvid) == ("video", "BV1dwuKzmE26")

    def test_video_av_url(self):
        t = parse_comment_target_url("https://www.bilibili.com/video/av123456")
        assert (t.kind, t.aid) == ("video", 123456)

    def test_bare_bv(self):
        t = parse_comment_target_url("BV1d54y1g7db")
        assert (t.kind, t.bvid) == ("video", "BV1d54y1g7db")

    def test_bare_av(self):
        t = parse_comment_target_url("av123")
        assert (t.kind, t.aid) == ("video", 123)

    def test_bare_digits_as_av(self):
        t = parse_comment_target_url("123")
        assert (t.kind, t.aid) == ("video", 123)

    def test_opus_url(self):
        t = parse_comment_target_url("https://www.bilibili.com/opus/987654321")
        assert (t.kind, t.dynamic_id) == ("dynamic", "987654321")

    def test_dynamic_url(self):
        t = parse_comment_target_url("https://www.bilibili.com/dynamic/456789")
        assert (t.kind, t.dynamic_id) == ("dynamic", "456789")

    def test_t_bilibili_short_url(self):
        t = parse_comment_target_url("https://t.bilibili.com/888999?share_medium=android")
        assert (t.kind, t.dynamic_id) == ("dynamic", "888999")

    def test_b23_short_link_rejected(self):
        with pytest.raises(ValueError):
            parse_comment_target_url("https://b23.tv/abc123")

    def test_unrecognized_rejected(self):
        with pytest.raises(ValueError):
            parse_comment_target_url("https://example.com/foo")


# ---------- 注册 ----------

class TestRegistration:

    def test_tools_registered(self):
        names = {t.name for t in ALL_TOOLS}
        assert "post_bilibili_comment" in names


# ---------- 内存登录态（首次 CDP 获取后仅在进程内复用） ----------

CACHE_COOKIE_STR = "SESSDATA=abc;bili_jct=xyz;buvid3=123"
CACHE_COOKIE_DICT = {"SESSDATA": "abc", "bili_jct": "xyz", "buvid3": "123"}


class TestMemoryOnlyCookie:

    @staticmethod
    def _make_bot():
        from media_platform.bilibili.comment_bot import BilibiliCommentBot

        return BilibiliCommentBot()

    @pytest.mark.asyncio
    async def test_setup_reads_browser_without_writing_cookie_file(self, tmp_path, monkeypatch):
        # setup 总是从浏览器读取 cookie，登录态不写入项目目录或其他缓存文件
        bot = self._make_bot()

        async def fake_connect():
            bot._browser_context = object()

        async def fake_read():
            return CACHE_COOKIE_STR, CACHE_COOKIE_DICT

        monkeypatch.setattr(bot, "_connect_browser", fake_connect)
        monkeypatch.setattr(bot, "_read_cookies_from_browser", fake_read)

        await bot.setup()
        assert bot.client is not None
        assert bot._csrf == "xyz"
        assert bot._browser_context is None  # Cookie 读取完成后立即断开 CDP
        assert not list(tmp_path.rglob("bili_comment_cookie.json"))

    @pytest.mark.asyncio
    async def test_disconnect_browser_preserves_in_memory_cookie_state(self):
        bot = self._make_bot()
        bot._browser_context = object()
        bot._build_client_from_cookies(CACHE_COOKIE_STR, CACHE_COOKIE_DICT)
        await bot._disconnect_browser()
        assert bot.client is not None
        assert bot._csrf == "xyz"
        assert bot._browser_context is None

    @pytest.mark.asyncio
    async def test_close_clears_in_memory_cookie_state(self):
        bot = self._make_bot()
        bot._browser_context = object()
        bot._csrf = "xyz"
        bot._build_client_from_cookies(CACHE_COOKIE_STR, CACHE_COOKIE_DICT)
        await bot.close()
        assert bot.client is None
        assert bot._csrf == ""
        assert bot._browser_context is None

    @pytest.mark.asyncio
    async def test_post_comment_retries_once_on_auth_error(self, monkeypatch):
        # 内存 cookie 失效（-101）：清空内存态 → 重连浏览器刷新 → 重试一次成功
        bot = self._make_bot()

        connects = []
        calls = []

        class FakeClient:
            async def get_video_info(self, bvid=None):
                return {"View": {"aid": 123, "title": "标题"}}

            async def post_comment_reply(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return {"code": -101, "message": "账号未登录"}
                return {"code": 0, "data": {"success_action": 0, "rpid": 999}}

            async def get_comment_list(self, type, oid, sort=0, pn=1, ps=20):
                return {"replies": [{"rpid": 999}]}

        bot.client = FakeClient()
        bot._csrf = "xyz"

        async def fake_connect():
            connects.append(True)
            bot._browser_context = object()

        async def fake_read():
            return CACHE_COOKIE_STR, CACHE_COOKIE_DICT

        def fake_build(cookie_str, cookie_dict):
            # 刷新后仍用替身 client，避免测试发真实网络请求
            bot.client = FakeClient()
            bot._csrf = "xyz"

        monkeypatch.setattr(bot, "_connect_browser", fake_connect)
        monkeypatch.setattr(bot, "_read_cookies_from_browser", fake_read)
        monkeypatch.setattr(bot, "_build_client_from_cookies", fake_build)

        result = await bot.post_comment("BV1dwuKzmE26", "你好")
        assert result["ok"] is True
        assert result["rpid"] == 999
        assert len(connects) == 1  # 失效后重连了一次浏览器
        assert len(calls) == 2  # 重试了一次
        assert bot._browser_context is None  # 刷新 Cookie 后立即断开 CDP

    @pytest.mark.asyncio
    async def test_post_comment_reuses_in_memory_client_without_browser_connect(self, monkeypatch):
        # 当前进程已有内存登录态时发评论全程不重新连接浏览器
        bot = self._make_bot()
        bot._build_client_from_cookies(CACHE_COOKIE_STR, CACHE_COOKIE_DICT)

        connects = []

        class FakeClient:
            async def get_video_info(self, bvid=None):
                return {"View": {"aid": 123, "title": "标题"}}

            async def post_comment_reply(self, **kwargs):
                return {"code": 0, "data": {"success_action": 0, "rpid": 777}}

            async def get_comment_list(self, type, oid, sort=0, pn=1, ps=20):
                return {"replies": []}

        bot.client = FakeClient()

        async def fake_connect():
            connects.append(True)
            bot._browser_context = object()

        monkeypatch.setattr(bot, "_connect_browser", fake_connect)

        result = await bot.post_comment("BV1dwuKzmE26", "你好")
        assert result["ok"] is True
        assert result["rpid"] == 777
        assert connects == []  # 全程没连浏览器


# ---------- client 层：表单形状（不触网） ----------

class TestClientPostCommentReply:

    @pytest.mark.asyncio
    async def test_form_shape_and_headers(self, monkeypatch):
        from media_platform.bilibili import client as bili_client

        client = object.__new__(bili_client.BilibiliClient)
        client._host = "https://api.bilibili.com"
        client.proxy = None
        client.timeout = 60
        client.headers = {"User-Agent": "ua", "Cookie": "ck", "Content-Type": "application/json"}
        captured = {}

        class FakeResponse:
            def json(self):
                return {"code": 0, "data": {"success_action": 0, "rpid": 1}}

        class FakeHttpClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def request(self, method, url, **kwargs):
                captured["method"] = method
                captured["url"] = url
                captured["kwargs"] = kwargs
                return FakeResponse()

        monkeypatch.setattr(bili_client, "make_async_client", lambda **kw: FakeHttpClient())

        resp = await client.post_comment_reply(type=1, oid=123, message="hello", csrf="csrftoken")
        assert resp["data"]["success_action"] == 0
        assert captured["method"] == "POST"
        assert captured["url"] == "https://api.bilibili.com/x/v2/reply/add"
        form = captured["kwargs"]["data"]
        assert form == {"type": 1, "oid": 123, "message": "hello", "plat": 1, "csrf": "csrftoken"}
        assert "root" not in form and "parent" not in form  # 顶层评论：字段整体省略
        assert captured["kwargs"]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    @pytest.mark.asyncio
    async def test_reply_fields_present_when_provided(self, monkeypatch):
        from media_platform.bilibili import client as bili_client

        client = object.__new__(bili_client.BilibiliClient)
        client._host = "https://api.bilibili.com"
        client.proxy = None
        client.timeout = 60
        client.headers = {"Cookie": "ck"}
        captured = {}

        class FakeResponse:
            def json(self):
                return {"code": 0, "data": {"success_action": 0, "rpid": 2}}

        class FakeHttpClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def request(self, method, url, **kwargs):
                captured["kwargs"] = kwargs
                return FakeResponse()

        monkeypatch.setattr(bili_client, "make_async_client", lambda **kw: FakeHttpClient())

        await client.post_comment_reply(
            type=1, oid=123, message="hi", csrf="c", root=11, parent=22)
        form = captured["kwargs"]["data"]
        assert form["root"] == 11 and form["parent"] == 22

    @pytest.mark.asyncio
    async def test_biz_error_code_returned_raw(self, monkeypatch):
        # code!=0（如 -101 未登录）应原样返回，不抛 DataFetchError（bot 层需要分类）
        from media_platform.bilibili import client as bili_client

        client = object.__new__(bili_client.BilibiliClient)
        client._host = "https://api.bilibili.com"
        client.proxy = None
        client.timeout = 60
        client.headers = {"Cookie": "ck"}

        class FakeResponse:
            def json(self):
                return {"code": -101, "message": "账号未登录"}

        class FakeHttpClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def request(self, method, url, **kwargs):
                return FakeResponse()

        monkeypatch.setattr(bili_client, "make_async_client", lambda **kw: FakeHttpClient())

        resp = await client.post_comment_reply(type=1, oid=123, message="hi", csrf="c")
        assert resp["code"] == -101
        assert resp["message"] == "账号未登录"
